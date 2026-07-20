"""托书抽取 Skill。"""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.config import settings
from app.core.skill_base import SkillBase
from app.errors import BadRequestError, ConvertError, ParseError
from app.llm import chat_json, image_to_data_url
from app.logging_conf import get_logger

from .convert_service import convert_to_markdown, is_image, render_pdf_pages
from .normalizer import normalize_llm_output
from .prompt import (
    build_few_shot_messages,
    build_system_prompt,
    build_user_message_text,
    build_user_message_vision,
    format_to_chat_text,
)
from .schema import TuoshuOutput

log = get_logger(__name__)


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


class TuoshuSkill(SkillBase):
    name = "tuoshu"
    version = "0.1.0"
    description = "海运托书结构化抽取（做箱通知、运输委托书、派车托书等）"
    accepts = [".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".pdf",
               ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"]
    output_model = TuoshuOutput

    def run(self, *, file_bytes: bytes, filename: str, options: dict | None = None) -> dict:
        ext = Path(filename).suffix.lower()
        if ext not in self.accepts:
            raise BadRequestError(f"unsupported extension: {ext}", details={"accepts": self.accepts})

        doc_format = ext.lstrip(".") if not is_image(ext) else "image"
        extracted_at = datetime.now(UTC).replace(microsecond=0).isoformat()

        system = build_system_prompt()
        few_shot = build_few_shot_messages()

        # 图片走 vision，其他走 markdown
        if is_image(ext):
            mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
            data_url = image_to_data_url(file_bytes, mime=mime)
            user_content = build_user_message_vision(
                data_url, filename=filename, doc_format=doc_format, extracted_at=extracted_at
            )
            messages = [{"role": "system", "content": system}, *few_shot,
                        {"role": "user", "content": user_content}]
        else:
            markdown = convert_to_markdown(file_bytes, filename)
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
                user_content = build_user_message_vision(
                    data_urls,
                    filename=filename,
                    doc_format=doc_format,
                    extracted_at=extracted_at,
                )
                messages = [
                    {"role": "system", "content": system},
                    *few_shot,
                    {"role": "user", "content": user_content},
                ]
            else:
                user_text = build_user_message_text(
                    markdown, filename=filename, doc_format=doc_format, extracted_at=extracted_at
                )
                messages = [{"role": "system", "content": system}, *few_shot,
                            {"role": "user", "content": user_text}]

        log.info("tuoshu_llm_start", extra={"file": filename, "doc_format": doc_format})
        # 用 TuoshuOutput 的 JSON Schema 约束模型输出，确保字段名严格一致
        output_schema = _clean_json_schema(TuoshuOutput.model_json_schema())
        data, meta = chat_json(messages, temperature=0.0,
                               json_schema=output_schema)

        if not isinstance(data, dict):
            raise ParseError("LLM output must be a JSON object")

        # 字段名归一化（兜底：网关不支持 json_schema 时仍能矫正中文 key）
        data = normalize_llm_output(data)

        # 确保 source 字段齐全（若模型漏填）
        src = data.get("source")
        if not isinstance(src, dict):
            src = {}
            data["source"] = src
        src.setdefault("file", filename)
        src.setdefault("doc_format", doc_format)
        src.setdefault("extracted_at", extracted_at)

        try:
            validated = TuoshuOutput.model_validate(data)
        except ValidationError as e:
            raise ParseError(
                "LLM output does not match tuoshu schema",
                details={"errors": e.errors(include_input=False)},
            ) from e
        result_dict = validated.model_dump()
        meta["chat_text"] = format_to_chat_text(result_dict)
        return {"result": result_dict, "meta": meta}
