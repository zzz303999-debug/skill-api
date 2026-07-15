"""托书抽取 Skill。"""

from __future__ import annotations

import mimetypes
from datetime import datetime
from pathlib import Path

from app.core.skill_base import SkillBase
from app.errors import BadRequestError
from app.llm import chat_json, image_to_data_url
from app.logging_conf import get_logger

from .convert_service import convert_to_markdown, is_image
from .prompt import (
    build_few_shot_messages,
    build_system_prompt,
    build_user_message_text,
    build_user_message_vision,
)
from .schema import TuoshuOutput

log = get_logger(__name__)


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
        extracted_at = datetime.utcnow().replace(microsecond=0).isoformat()

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
            # 扫描版 PDF：底层脚本会输出 SCAN_OR_IMAGE_HINT；此时降级为把 PDF 转图片首页发 vision
            # 简化处理：直接把提示喂给模型，让其返回可解释的结构，后续可接 OCR skill
            user_text = build_user_message_text(
                markdown, filename=filename, doc_format=doc_format, extracted_at=extracted_at
            )
            messages = [{"role": "system", "content": system}, *few_shot,
                        {"role": "user", "content": user_text}]

        log.info("tuoshu_llm_start", extra={"file": filename, "doc_format": doc_format})
        data, meta = chat_json(messages, temperature=0.0)

        # 确保 source 字段齐全（若模型漏填）
        src = data.setdefault("source", {})
        src.setdefault("file", filename)
        src.setdefault("doc_format", doc_format)
        src.setdefault("extracted_at", extracted_at)

        # Pydantic 校验（extra='allow'，容错但保留强类型 OpenAPI schema）
        validated = TuoshuOutput.model_validate(data)
        return {"result": validated.model_dump(), "meta": meta}
