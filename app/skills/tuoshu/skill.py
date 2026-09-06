"""托书抽取 Skill。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.core.config import settings
from app.core.doc_convert import (
    convert_image_to_parse_result_async,
    convert_to_markdown_async,
    detect_document_format,
    is_image,
    render_pdf_pages,
)
from app.core.errors import BadRequestError, ConvertError, ParseError
from app.core.logging_conf import get_logger
from app.core.skill_base import SkillBase
from app.llm import achat_json, image_to_data_url
from app.mineru import client as mineru

from .mapping.deterministic_mapper import map_template, merge_deterministic_values
from .mapping.finalize import finalize_extraction
from .normalize import normalize_llm_output
from .prompt import (
    build_few_shot_messages,
    build_system_prompt,
    build_user_message_text,
    build_user_message_vision,
    detect_prompt_route,
)
from .schema import TuoshuOutput

log = get_logger(__name__)

_INVALID_REVIEW_ISSUE_MESSAGE = (
    "LLM review_issues item does not match the required structure"
)


def _clean_json_schema(schema: dict) -> dict:
    """去除 Pydantic 生成的 $defs / anyOf，转成模型友好的简化 schema。

    - 展平 $defs 引用
    - null 字段用 type: ["string", "null"] 替代 anyOf
    - 移除 title / default / $schema 等冗余字段
    """
    import copy
    schema = copy.deepcopy(schema)

    defs = schema.pop("$defs", {})

    def _resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref and ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):]
                resolved = defs.get(name, {})
                return _resolve(resolved)
            result = {}
            for k, v in node.items():
                if k in ("title", "default", "$schema"):
                    continue
                if k == "anyOf":
                    options = [_resolve(opt) for opt in v]
                    non_null = [opt for opt in options if opt.get("type") != "null"]
                    has_null = len(non_null) != len(options)
                    if has_null and len(non_null) == 1:
                        nullable = non_null[0]
                        nullable_type = nullable.get("type")
                        if isinstance(nullable_type, str):
                            nullable["type"] = [nullable_type, "null"]
                            result.update(nullable)
                        elif isinstance(nullable_type, list):
                            nullable["type"] = [*nullable_type, "null"]
                            result.update(nullable)
                        else:
                            result["anyOf"] = [nullable, {"type": "null"}]
                    else:
                        result["anyOf"] = options
                elif k in ("properties",):
                    result[k] = {pk: _resolve(pv) for pk, pv in v.items()}
                elif k == "items":
                    result[k] = _resolve(v)
                else:
                    result[k] = v
            return result
        return node

    return _resolve(schema)


def _extract_review_issues_list(raw: dict | list) -> list | None:
    """从修复响应中提取 review_issues 数组（兼容裸数组或 {"review_issues": [...]}）。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        issues = raw.get("review_issues")
        if isinstance(issues, list):
            return issues
        # 部分模型可能用中文 key
        issues = raw.get("复核问题")
        if isinstance(issues, list):
            return issues
    return None


async def _normalize_with_review_issue_repair(
    data: dict,
    meta: dict,
    *,
    messages: list[dict],
    output_schema: dict,
) -> tuple[dict, dict]:
    """_normalize_with_review_issue_repair（2026-09 异步化改造后为生产唯一入口）（repair 重试走 achat_json），
    其余逻辑逐行一致。"""
    try:
        return normalize_llm_output(data), meta
    except ParseError as exc:
        if exc.message != _INVALID_REVIEW_ISSUE_MESSAGE:
            raise

        details = exc.details
        log.warning(
            "tuoshu_review_issue_repair_retry",
            extra={
                "review_issue_index": details.get("index"),
                "reason": details.get("reason"),
            },
        )
        repair_messages = [
            *messages,
            {
                "role": "assistant",
                "content": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            },
            {
                "role": "user",
                "content": (
                    "上一份 JSON 的 review_issues 结构无效："
                    f"index={details.get('index')}, reason={details.get('reason')}。"
                    "不要重复输出整个 JSON 对象，只重新输出 review_issues 数组本身。"
                    "数组的每一项都必须是对象，且 code、field、message 必须是非空字符串；"
                    "source_values 必须是字符串数组（没有候选值时用 []）；"
                    "blocking 必须是布尔值。其余字段保持原样。"
                ),
            },
        ]
        repaired_raw, repaired_meta = await achat_json(
            repair_messages,
            temperature=0.0,
            # 目标是裸数组，不套用完整对象 schema，避免约束模型输出整个 JSON
            json_schema=None,
        )
        repaired_issues = _extract_review_issues_list(repaired_raw)
        if repaired_issues is None:
            raise ParseError(
                _INVALID_REVIEW_ISSUE_MESSAGE,
                details={"index": "n/a", "reason": "repair response is not a review_issues array"},
            ) from None
        data["review_issues"] = repaired_issues
        return normalize_llm_output(data), repaired_meta


@dataclass(frozen=True)
class _NeedsScanOcr:
    """扫描 PDF 无视觉分支的中间信号（收尾计划改造项 C）。

    _prepare_doc_stage（CPU 组装段）遇「SCAN_OR_IMAGE_HINT + .pdf +
    llm_vision_enabled=False」时返回本哨兵；_prepare_stage_async 编排层
    await parse_document_async 后交给 _prepare_scan_stage 完成组装
    （空文本检查与 ConvertError 语义在编排层，逐字对齐原同步内联分支）。"""

    file_bytes: bytes
    filename: str


class TuoshuSkill(SkillBase):
    name = "tuoshu"
    version = "0.1.0"
    description = "海运托书结构化抽取（做箱通知、运输委托书、派车托书等）"
    accepts = [".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".pdf",
               ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".webp"]
    output_model = TuoshuOutput
    include_content = True

    def _prepare_head(self, file_bytes: bytes, filename: str) -> dict:
        """轻 CPU 头段：ext 校验与上下文初始化（_prepare_stage_async 首段，to_thread）。"""
        ext = Path(filename).suffix.lower()
        if ext not in self.accepts:
            raise BadRequestError(f"unsupported extension: {ext}", details={"accepts": self.accepts})

        return {
            "ext": ext,
            "doc_format": ext.lstrip("."),
            "detected_format": detect_document_format(file_bytes),
            "extracted_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "source_text": None,
            "conversion_meta": {},
            "parser_review_issues": [],
            "route_text": filename,
            "filename": filename,
            "file_bytes": file_bytes,
        }

    def _prepare_image_stage(self, ctx: dict, filename: str, parse_result) -> dict:
        """图片分支组装段（纯 CPU）：parse_result 由编排层 await 注入（收尾计划改造项 D）。"""
        doc_format = ctx["doc_format"]
        extracted_at = ctx["extracted_at"]
        source_text = ctx["source_text"]
        conversion_meta = ctx["conversion_meta"]
        parser_review_issues = ctx["parser_review_issues"]
        route_text = ctx["route_text"]
        doc_format = parse_result.input_format
        conversion_meta = parse_result.meta()
        parser_review_issues = parse_result.review_issues()
        source_text = parse_result.markdown or None
        route_text = f"{filename}\n{source_text or ''}"
        skip_vision = (
            not settings.llm_vision_enabled
            or (
                settings.image_vision_skip_when_confident
                and parse_result.parser == "mineru"
                and not parse_result.parser_fallback
            )
        )
        if parse_result.vision_images and not skip_vision:
            images = list(parse_result.vision_inputs)
            total_image_bytes = sum(len(image) for image, _ in images)
            if total_image_bytes > settings.vision_max_image_bytes:
                if not source_text:
                    # 无 OCR 文本可降级时，绝不能把空文档喂给 LLM——模型会输出
                    # 整份捏造数据。直接拒绝并提示压缩/拆分图片。
                    raise ConvertError(
                        "image exceeds the vision upload limit and no OCR text is "
                        "available; compress or split the image and retry",
                        code="vision_image_too_large",
                        details={
                            "file": Path(filename).name,
                            "bytes": total_image_bytes,
                            "max_bytes": settings.vision_max_image_bytes,
                        },
                    )
                # 原图 base64 直传会超网关请求体限制（base64 膨胀约 1/3）；
                # 有 OCR 文本时降级为纯文本抽取并标记必须人工复核
                parser_review_issues.append(
                    {
                        "code": "vision_image_too_large",
                        "field": "source",
                        "message": (
                            f"图片总大小 {total_image_bytes} 字节超过 vision 直传上限 "
                            f"{settings.vision_max_image_bytes} 字节，已跳过 LLM vision "
                            "交叉核验，必须人工复核"
                        ),
                        "source_values": [],
                        "blocking": True,
                    }
                )
                user_content = build_user_message_text(
                    source_text or "",
                    filename=filename,
                    doc_format=doc_format,
                    extracted_at=extracted_at,
                )
            else:
                data_urls = [
                    image_to_data_url(image, mime=mime)
                    for image, mime in images
                ]
                user_content = build_user_message_vision(
                    data_urls,
                    filename=filename,
                    doc_format=doc_format,
                    extracted_at=extracted_at,
                    parsed_text=source_text,
                    # MinerU already supplies the structure.  The image is a
                    # low-cost independent check against OCR hallucinations,
                    # not a second full-document extraction pass.
                    image_detail=(
                        "low"
                        if parse_result.parser == "mineru" and not parse_result.parser_fallback
                        else "high"
                    ),
                )
        else:
            if skip_vision:
                parser_review_issues.append(
                    {
                        "code": "vision_cross_check_skipped",
                        "field": "source",
                        "message": "MinerU 高置信，已跳过 LLM vision 交叉核验以提速，建议人工抽检关键编号与数值",
                        "source_values": [],
                        "blocking": False,
                    }
                )
            if not source_text:
                # 无 OCR 文本可降级且模型无视觉时，绝不能把空文档喂给 LLM——
                # 模型会输出整份捏造数据。直接拒绝并提示检查 MinerU 服务。
                raise ConvertError(
                    "image has no OCR text and the current LLM model has no "
                    "vision capability; check the MinerU service or use a "
                    "vision-capable model",
                    code="vision_disabled_no_ocr",
                    details={"file": Path(filename).name},
                )
            user_content = build_user_message_text(
                source_text,
                filename=filename,
                doc_format=doc_format,
                extracted_at=extracted_at,
            )
        ctx.update(
            source_text=source_text, route_text=route_text,
            conversion_meta=conversion_meta, parser_review_issues=parser_review_issues,
            doc_format=doc_format, user_content=user_content,
        )
        return self._finalize_prepare(ctx)

    def _prepare_doc_stage(self, ctx: dict, file_bytes: bytes, filename: str, markdown):
        """非图片分支组装段（纯 CPU）：markdown 由编排层 await 注入；
        扫描 PDF 无视觉分支返回 _NeedsScanOcr 哨兵（改造项 C）。"""
        ext = ctx["ext"]
        doc_format = ctx["doc_format"]
        extracted_at = ctx["extracted_at"]
        conversion_meta = ctx["conversion_meta"]
        parser_review_issues = ctx["parser_review_issues"]
        source_text = None
        route_text = ctx["route_text"]
        user_content = None
        parse_result = getattr(markdown, "parse_result", None)
        if parse_result is not None:
            conversion_meta = parse_result.meta()
            parser_review_issues = parse_result.review_issues()
        else:
            parser = getattr(markdown, "parser", None)
            if parser:
                conversion_meta = {
                    "parser": parser,
                    "parser_fallback": bool(getattr(markdown, "parser_fallback", False)),
                }
        if markdown.startswith("SCAN_OR_IMAGE_HINT:"):
            if ext != ".pdf":
                # hint 行尾 `# 原因` 携带转换器具体失败原因，透传给调用方
                hint_detail = ""
                if "#" in markdown:
                    hint_detail = markdown.split("#", 1)[1].strip()
                log.warning(
                    "tuoshu_convert_scan_hint",
                    extra={"file": filename, "ext": ext, "hint": hint_detail},
                )
                raise ConvertError(
                    "document has no extractable text; convert it to PDF/image or install LibreOffice",
                    details={"file": Path(filename).name, "reason": hint_detail},
                )
            if not settings.llm_vision_enabled:
                # 扫描 PDF：模型无视觉，交 MinerU OCR——网络调用提升到编排层
                # await（收尾计划改造项 C：CPU 组装段返回哨兵信号）
                return _NeedsScanOcr(file_bytes=file_bytes, filename=filename)
            page_images = render_pdf_pages(
                file_bytes,
                max_pages=settings.vision_max_pdf_pages,
                scale=settings.vision_pdf_render_scale,
            )
            total_image_bytes = sum(len(image) for image in page_images)
            if total_image_bytes > settings.vision_max_image_bytes:
                # 渲染出的 PNG 总字节同样受 vision 直传上限约束，
                # 超限时报错提示拆分，避免网关拒绝与内存峰值。
                raise ConvertError(
                    "rendered scan pages exceed the vision upload limit; "
                    "split the PDF into smaller parts and retry",
                    code="vision_image_too_large",
                    details={
                        "file": Path(filename).name,
                        "bytes": total_image_bytes,
                        "max_bytes": settings.vision_max_image_bytes,
                    },
                )
            data_urls = [image_to_data_url(image, mime="image/png") for image in page_images]
            parser_review_issues.extend(
                {
                    "code": "vision_only_unverified",
                    "field": f"source.pages[{page_index}]",
                    "message": "扫描 PDF 页面仅由 vision 识别，没有独立 OCR 文本可交叉核验，必须人工复核",
                    "source_values": [],
                    "blocking": True,
                }
                for page_index in range(len(page_images))
            )
            conversion_meta = {
                "parser": "vision",
                "parser_fallback": True,
                "input_format": "pdf",
                "page_routes": [
                    {
                        "page": page_index + 1,
                        "parser": "vision",
                        "confidence": "low",
                        "issues": ["vision_only_unverified"],
                    }
                    for page_index in range(len(page_images))
                ],
            }
            user_content = build_user_message_vision(
                data_urls,
                filename=filename,
                doc_format=doc_format,
                extracted_at=extracted_at,
            )
        elif (
            parse_result is not None
            and parse_result.vision_images
            and settings.llm_vision_enabled
        ):
            source_text = str(markdown) or None
            route_text = f"{filename}\n{source_text or ''}"
            images = list(parse_result.vision_inputs)
            total_image_bytes = sum(len(image) for image, _ in images)
            if total_image_bytes > settings.vision_max_image_bytes:
                if not source_text:
                    raise ConvertError(
                        "parsed images exceed the vision upload limit and no OCR "
                        "text is available; split the document and retry",
                        code="vision_image_too_large",
                        details={
                            "file": Path(filename).name,
                            "bytes": total_image_bytes,
                            "max_bytes": settings.vision_max_image_bytes,
                        },
                    )
                parser_review_issues.append(
                    {
                        "code": "vision_image_too_large",
                        "field": "source",
                        "message": (
                            f"解析出的图片总大小 {total_image_bytes} 字节超过 vision "
                            f"直传上限 {settings.vision_max_image_bytes} 字节，"
                            "已跳过 LLM vision 交叉核验，必须人工复核"
                        ),
                        "source_values": [],
                        "blocking": True,
                    }
                )
                user_content = build_user_message_text(
                    source_text or "",
                    filename=filename,
                    doc_format=doc_format,
                    extracted_at=extracted_at,
                )
            else:
                data_urls = [
                    image_to_data_url(image, mime=mime)
                    for image, mime in images
                ]
                user_content = build_user_message_vision(
                    data_urls,
                    filename=filename,
                    doc_format=doc_format,
                    extracted_at=extracted_at,
                    parsed_text=source_text,
                )
        else:
            source_text = str(markdown)
            route_text = f"{filename}\n{markdown}"
            user_text = build_user_message_text(
                markdown, filename=filename, doc_format=doc_format, extracted_at=extracted_at
            )
            user_content = user_text
        ctx.update(
            source_text=source_text, route_text=route_text,
            conversion_meta=conversion_meta, parser_review_issues=parser_review_issues,
            user_content=user_content,
        )
        return self._finalize_prepare(ctx)

    def _prepare_scan_stage(self, ctx: dict, filename: str, scanned_markdown: str) -> dict:
        """扫描 PDF OCR 组装段（纯 CPU，改造项 C）：scanned_markdown 由编排层
        await parse_document_async 后注入（空文本检查已在编排层完成）。"""
        doc_format = ctx["doc_format"]
        extracted_at = ctx["extracted_at"]
        parser_review_issues = ctx["parser_review_issues"]
        # MinerU OCR 成功：走纯文本抽取；扫描件无独立文本层可交叉核验
        parser_review_issues.append(
            {
                "code": "scanned_pdf_ocr_unverified",
                "field": "source",
                "message": "扫描 PDF 由 MinerU OCR 解析，无独立文本层可交叉核验，必须人工复核",
                "source_values": [],
                "blocking": True,
            }
        )
        conversion_meta = {
            "parser": "mineru",
            "parser_fallback": True,
            "input_format": "pdf",
        }
        user_content = build_user_message_text(
            scanned_markdown,
            filename=filename,
            doc_format=doc_format,
            extracted_at=extracted_at,
        )
        ctx.update(
            parser_review_issues=parser_review_issues,
            conversion_meta=conversion_meta,
            user_content=user_content,
        )
        return self._finalize_prepare(ctx)

    def _finalize_prepare(self, ctx: dict) -> dict:
        """prompt 构造尾段（所有组装分支共享，纯 CPU）。"""
        source_text = ctx["source_text"]
        route_text = ctx["route_text"]
        conversion_meta = ctx["conversion_meta"]
        parser_review_issues = ctx["parser_review_issues"]
        doc_format = ctx["doc_format"]
        detected_format = ctx["detected_format"]
        extracted_at = ctx["extracted_at"]
        filename = ctx["filename"]
        file_bytes = ctx["file_bytes"]
        user_content = ctx["user_content"]
        mapper_result = map_template(source_text)
        route = detect_prompt_route(route_text)
        system = build_system_prompt(route)
        few_shot = build_few_shot_messages(route)
        deterministic_messages: list[dict] = []
        if mapper_result.matched:
            deterministic_messages.append(
                {
                    "role": "system",
                    "content": (
                        "以下 JSON 字段已经由高置信模板确定性映射锁定。"
                        "只补充其中未覆盖的字段，不得改写锁定值；若原文看似冲突，"
                        "保留锁定值并写入 blocking review_issues：\n"
                        + json.dumps(mapper_result.values, ensure_ascii=False, separators=(",", ":"))
                    ),
                }
            )
            conversion_meta.update(
                {
                    "deterministic_template": mapper_result.template_id,
                    "template_fingerprint": mapper_result.fingerprint,
                }
            )
        messages = [
            {"role": "system", "content": system},
            *few_shot,
            *deterministic_messages,
            {"role": "user", "content": user_content},
        ]

        few_shot_chars = sum(
            len(message["content"])
            for message in few_shot
            if isinstance(message.get("content"), str)
        )
        log.info(
            "tuoshu_prompt_built",
            extra={
                "file": filename,
                "route_doc_type": route.doc_type,
                "route_template_hint": route.template_hint,
                "system_chars": len(system),
                "few_shot_chars": few_shot_chars,
                "document_chars": len(source_text) if source_text is not None else None,
            },
        )
        return {
            "messages": messages,
            "conversion_meta": conversion_meta,
            "source_text": source_text,
            "route": route,
            "mapper_result": mapper_result,
            "parser_review_issues": parser_review_issues,
            "doc_format": doc_format,
            "detected_format": detected_format,
            "extracted_at": extracted_at,
            "filename": filename,
            "file_bytes": file_bytes,
        }

    async def _prepare_stage_async(self, *, file_bytes: bytes, filename: str) -> dict:
        """两段式准备编排（收尾计划改造项 C+D）：网络段 await，组装段 to_thread。

        - 轻头段/各组装段均为纯 CPU，经 to_thread 执行；
        - 图片 → await convert_image_to_parse_result_async（共享 MinerU 连接池）；
        - 非图片 → await convert_to_markdown_async；扫描 PDF 无视觉分支捕获
          _NeedsScanOcr 哨兵后 await parse_document_async（错误语义与原同步
          内联分支逐字一致）。
        """
        import asyncio

        ctx = await asyncio.to_thread(self._prepare_head, file_bytes, filename)
        ext = ctx["ext"]
        if is_image(ext):
            parse_result = await convert_image_to_parse_result_async(file_bytes, filename)
            return await asyncio.to_thread(
                self._prepare_image_stage, ctx, filename, parse_result
            )
        markdown = await convert_to_markdown_async(file_bytes, filename)
        result = await asyncio.to_thread(
            self._prepare_doc_stage, ctx, file_bytes, filename, markdown
        )
        if isinstance(result, _NeedsScanOcr):
            try:
                scanned = await mineru.parse_document_async(
                    result.file_bytes, result.filename, mime_type="application/pdf"
                )
            except Exception as exc:
                raise ConvertError(
                    "scan PDF has no extractable text and MinerU OCR failed; "
                    "check the MinerU service or use a text-based PDF",
                    code="vision_disabled_no_ocr",
                    details={
                        "file": Path(result.filename).name,
                        "mineru_error": f"{exc.__class__.__name__}: {exc}",
                    },
                ) from exc
            if not scanned.markdown.strip():
                raise ConvertError(
                    "scan PDF has no extractable text and MinerU OCR returned "
                    "empty; check the MinerU service or use a text-based PDF",
                    code="vision_disabled_no_ocr",
                    details={"file": Path(result.filename).name},
                )
            return await asyncio.to_thread(
                self._prepare_scan_stage, ctx, filename, scanned.markdown
            )
        return result

    async def run(self, *, file_bytes: bytes, filename: str, options: dict | None = None) -> dict:
        """异步契约主流程：两段式准备（网络段 await，CPU 段 to_thread）→
        LLM（achat_json 真异步）→ 后处理段（CPU）；语义与拆分前 run() 一致。"""
        ctx = await self._prepare_stage_async(file_bytes=file_bytes, filename=filename)
        log.info(
            "tuoshu_llm_start",
            extra={"file": filename, "doc_format": ctx["doc_format"]},
        )
        output_schema = _clean_json_schema(TuoshuOutput.model_json_schema())
        data, meta = await achat_json(
            ctx["messages"], temperature=0.0, json_schema=output_schema
        )
        if not isinstance(data, dict):
            raise ParseError("LLM output must be a JSON object")
        data, meta = await _normalize_with_review_issue_repair(
            data,
            meta,
            messages=ctx["messages"],
            output_schema=output_schema,
        )
        return self._finalize_stage(ctx, data, meta)

    def _finalize_stage(self, ctx: dict, data: dict, meta: dict) -> dict:
        """后处理段（纯 CPU）：路由 doc_type 覆盖/确定性合并/终稿化/校验/组装。"""
        filename = ctx["filename"]
        source_text = ctx["source_text"]
        conversion_meta = ctx["conversion_meta"]
        parser_review_issues = ctx["parser_review_issues"]
        route = ctx["route"]
        mapper_result = ctx["mapper_result"]
        doc_format = ctx["doc_format"]
        detected_format = ctx["detected_format"]
        extracted_at = ctx["extracted_at"]
        file_bytes = ctx["file_bytes"]
        if route.doc_type != "UNKNOWN":
            llm_doc_type = data.get("doc_type")
            if llm_doc_type != route.doc_type:
                log.warning(
                    "tuoshu_doc_type_route_override",
                    extra={
                        "file": filename,
                        "llm_doc_type": llm_doc_type,
                        "route_doc_type": route.doc_type,
                    },
                )
            data["doc_type"] = route.doc_type
        data = merge_deterministic_values(data, mapper_result)
        if parser_review_issues:
            data.setdefault("review_issues", []).extend(parser_review_issues)
        filename_year = re.search(r"(?<!\d)(20\d{2})(?!\d)", filename)
        template_hint = mapper_result.template_id or route.template_hint
        data = finalize_extraction(
            data,
            source_text=source_text,
            reference_year=int(filename_year.group(1)) if filename_year else None,
            template_hint=template_hint,
        )

        # 确保 source 字段齐全（若模型漏填）
        src = data.get("source")
        if not isinstance(src, dict):
            src = {}
            data["source"] = src
        src.setdefault("file", filename)
        src.setdefault("doc_format", doc_format)
        src["template_hint"] = template_hint
        src.setdefault("extracted_at", extracted_at)

        try:
            validated = TuoshuOutput.model_validate(data)
        except ValidationError as e:
            raise ParseError(
                "LLM output does not match tuoshu schema",
                details={"errors": e.errors(include_input=False)},
            ) from e
        result_dict = validated.model_dump()
        safe_meta = {key: meta.get(key) for key in ("model", "usage") if key in meta}
        safe_meta.update(conversion_meta)
        converted_content = source_text or ""
        coverage = conversion_meta.get("coverage")
        coverage_complete = not isinstance(coverage, dict) or coverage.get("complete") is not False
        safe_meta.update(
            {
                "source_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "content_sha256": (
                    hashlib.sha256(converted_content.encode("utf-8")).hexdigest()
                    if converted_content
                    else None
                ),
                "source_bytes": len(file_bytes),
                "content_chars": len(converted_content),
                "detected_format": detected_format,
                "conversion_status": (
                    "needs_review"
                    if parser_review_issues or not converted_content or not coverage_complete
                    else "converted"
                ),
            }
        )
        return {"result": result_dict, "content": converted_content, "meta": safe_meta}
