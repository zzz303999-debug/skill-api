"""托书抽取 Skill。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.config import settings
from app.core.skill_base import SkillBase
from app.errors import BadRequestError, ConvertError, ParseError
from app.llm import chat_json, image_to_data_url
from app.logging_conf import get_logger

from .convert_service import (
    convert_image_to_parse_result,
    convert_to_markdown,
    is_image,
    render_pdf_pages,
)
from .deterministic_mapper import map_template, merge_deterministic_values
from .normalizer import normalize_llm_output
from .postprocessor import finalize_extraction
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


def _normalize_with_review_issue_repair(
    data: dict,
    meta: dict,
    *,
    messages: list[dict],
    output_schema: dict,
) -> tuple[dict, dict]:
    """Retry once when the model emits an incomplete structured review issue."""
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
                    "请完整重新输出整个 JSON 对象，不要删除任何复核项。"
                    "review_issues 的每一项都必须是对象，且 code、field、message "
                    "必须是非空字符串；source_values 必须是字符串数组；blocking 必须是布尔值。"
                ),
            },
        ]
        repaired_data, repaired_meta = chat_json(
            repair_messages,
            temperature=0.0,
            json_schema=output_schema,
        )
        return normalize_llm_output(repaired_data), repaired_meta


class TuoshuSkill(SkillBase):
    name = "tuoshu"
    version = "0.1.0"
    description = "海运托书结构化抽取（做箱通知、运输委托书、派车托书等）"
    accepts = [".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".pdf",
               ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".webp"]
    output_model = TuoshuOutput

    def run(self, *, file_bytes: bytes, filename: str, options: dict | None = None) -> dict:
        ext = Path(filename).suffix.lower()
        if ext not in self.accepts:
            raise BadRequestError(f"unsupported extension: {ext}", details={"accepts": self.accepts})

        doc_format = ext.lstrip(".")
        extracted_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        source_text: str | None = None
        conversion_meta: dict = {}
        parser_review_issues: list[dict] = []
        route_text = filename

        # 图片优先原图直传 MinerU；硬失败或低置信时才携原图走 vision。
        if is_image(ext):
            parse_result = convert_image_to_parse_result(file_bytes, filename)
            doc_format = parse_result.input_format
            conversion_meta = parse_result.meta()
            parser_review_issues = parse_result.review_issues()
            source_text = parse_result.markdown or None
            route_text = f"{filename}\n{source_text or ''}"
            if parse_result.vision_images:
                data_urls = [
                    image_to_data_url(image, mime=f"image/{doc_format}")
                    for image in parse_result.vision_images
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
                user_content = build_user_message_text(
                    source_text or "",
                    filename=filename,
                    doc_format=doc_format,
                    extracted_at=extracted_at,
                )
        else:
            markdown = convert_to_markdown(file_bytes, filename)
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
                    raise ConvertError(
                        "document has no extractable text; convert it to PDF/image or install LibreOffice"
                    )
                page_images = render_pdf_pages(
                    file_bytes,
                    max_pages=settings.vision_max_pdf_pages,
                    scale=settings.vision_pdf_render_scale,
                )
                data_urls = [image_to_data_url(image, mime="image/png") for image in page_images]
                if conversion_meta:
                    conversion_meta["parser"] = "vision"
                user_content = build_user_message_vision(
                    data_urls,
                    filename=filename,
                    doc_format=doc_format,
                    extracted_at=extracted_at,
                )
            elif parse_result is not None and parse_result.vision_images:
                source_text = str(markdown) or None
                route_text = f"{filename}\n{source_text or ''}"
                data_urls = [
                    image_to_data_url(image, mime="image/png")
                    for image in parse_result.vision_images
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

        log.info("tuoshu_llm_start", extra={"file": filename, "doc_format": doc_format})
        # 用 TuoshuOutput 的 JSON Schema 约束模型输出，确保字段名严格一致
        output_schema = _clean_json_schema(TuoshuOutput.model_json_schema())
        data, meta = chat_json(messages, temperature=0.0,
                               json_schema=output_schema)

        if not isinstance(data, dict):
            raise ParseError("LLM output must be a JSON object")

        # 字段名归一化（兜底：网关不支持 json_schema 时仍能矫正中文 key）
        data, meta = _normalize_with_review_issue_repair(
            data,
            meta,
            messages=messages,
            output_schema=output_schema,
        )
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
        data = finalize_extraction(data, source_text=source_text)

        # 确保 source 字段齐全（若模型漏填）
        src = data.get("source")
        if not isinstance(src, dict):
            src = {}
            data["source"] = src
        src.setdefault("file", filename)
        src.setdefault("doc_format", doc_format)
        src["template_hint"] = mapper_result.template_id or route.template_hint
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
        return {"result": result_dict, "meta": safe_meta}
