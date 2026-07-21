"""MinerU HTTP API 客户端。

兼容常见的 ``mineru-api`` ``POST /file_parse`` 接口。客户端只关心最终
Markdown，既支持 JSON 响应，也支持直接返回 Markdown 或 ZIP 文件。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import httpx

from app.config import settings


class MinerUError(Exception):
    """MinerU 调用或响应解析失败。"""


_MARKDOWN_KEYS = ("md_content", "markdown_content", "markdown")


def _extract_markdown(value: Any) -> str | None:
    """从不同版本/包装层的 MinerU JSON 响应中递归查找 Markdown。"""
    if isinstance(value, dict):
        for key in _MARKDOWN_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        for nested in value.values():
            candidate = _extract_markdown(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _extract_markdown(nested)
            if candidate:
                return candidate
    return None


def _extract_zip_markdown(content: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            markdown_files = sorted(
                (name for name in archive.namelist() if name.lower().endswith(".md")),
                key=lambda name: (name.count("/"), len(name)),
            )
            if not markdown_files:
                return None
            return archive.read(markdown_files[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return None


def parse_pdf(file_bytes: bytes, filename: str) -> str:
    """调用 MinerU，把 PDF 转成 Markdown。"""
    if not settings.mineru_base_url.strip():
        raise MinerUError("MINERU_BASE_URL is empty")

    endpoint = settings.mineru_endpoint.strip() or "/file_parse"
    url = f"{settings.mineru_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    headers: dict[str, str] = {}
    if settings.mineru_api_key:
        headers["Authorization"] = f"Bearer {settings.mineru_api_key}"

    safe_filename = Path(filename).name or "document.pdf"
    form = {
        "backend": settings.mineru_backend,
        "parse_method": settings.mineru_parse_method,
        "lang_list": settings.mineru_language,
        "formula_enable": "true",
        "table_enable": "true",
        "return_md": "true",
        "return_middle_json": "false",
        "return_model_output": "false",
        "return_content_list": "false",
        "return_images": "false",
    }

    try:
        with httpx.Client(timeout=settings.mineru_timeout_seconds) as client:
            response = client.post(
                url,
                headers=headers,
                data=form,
                files={"files": (safe_filename, file_bytes, "application/pdf")},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MinerUError(f"MinerU request failed: {exc.__class__.__name__}") from exc

    content_type = response.headers.get("content-type", "").lower()
    if "zip" in content_type or response.content.startswith(b"PK"):
        markdown = _extract_zip_markdown(response.content)
    elif "json" in content_type:
        try:
            markdown = _extract_markdown(response.json())
        except ValueError as exc:
            raise MinerUError("MinerU returned invalid JSON") from exc
    else:
        markdown = response.text if response.text.strip() else None

    if not markdown or not markdown.strip():
        raise MinerUError("MinerU response contains no Markdown")
    return markdown.strip()
