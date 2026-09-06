"""MinerU HTTP API 客户端。

兼容常见的 ``mineru-api`` ``POST /file_parse`` 接口。客户端保留 Markdown、
结构块和低置信依据，支持 JSON、直接 Markdown 或 ZIP 响应。

2026-09 异步化后生产唯一入口 parse_document_async（同步版已随第二波
同步链清理退役）：网络段走共享 AsyncClient 懒加载单例（连接池复用），
前置校验/响应解码/质量评估为模块内共享纯函数。
"""

from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging_conf import get_logger

log = get_logger(__name__)


class MinerUError(Exception):
    """MinerU 调用或响应解析失败。"""


class MinerUContractError(MinerUError):
    """MinerU version/profile contract is incompatible."""


_MARKDOWN_KEYS = ("md_content", "markdown_content", "markdown")
_CONTENT_LIST_KEYS = ("content_list", "contentList")
_IMAGE_BLOCK_TYPES = {"image", "figure", "stamp", "seal", "logo", "watermark"}
MINERU_REQUEST_PROFILE_ID = "tuoshu-pipeline-ch-v1"
MINERU_REQUEST_PROFILE = {
    "backend": "pipeline",
    "parse_method": "auto",
    "lang_list": "ch",
    "formula_enable": "true",
    "table_enable": "true",
    "return_md": "true",
    "return_middle_json": "false",
    "return_model_output": "false",
    "return_content_list": "true",
    "return_images": "false",
}

# 图片单据（托书/做箱通知）没有数学公式；CPU 容器上公式识别耗时占比高，
# 关闭以提速。注意：扫描版 PDF 的 OCR 页会先渲染成 {stem}-page-{n}.png 再
# 传入，同样落入图片分支关闭公式识别（托书场景无公式，行为口径一致）。
_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".webp"}
)


def _request_form(filename: str) -> dict[str, str]:
    """按文件类型构造 MinerU 请求参数（图片/OCR 渲染页关闭公式识别）。"""
    form = dict(MINERU_REQUEST_PROFILE)
    if Path(filename).suffix.lower() in _IMAGE_SUFFIXES:
        form["formula_enable"] = "false"
    return form
_MINERU_VERSION_HEADERS = ("x-mineru-version", "mineru-version")
_KEY_LABELS = (
    "主单号",
    "提单号",
    "船名",
    "航次",
    "件数",
    "箱型",
    "业务编号",
    "我司编号",
    "目的港",
)


@dataclass(frozen=True)
class MinerUParseResult:
    markdown: str
    content_list: tuple[dict[str, Any], ...] = ()
    table_count: int = 0
    key_labels: tuple[str, ...] = ()
    low_confidence_reasons: tuple[str, ...] = ()

    @property
    def low_confidence(self) -> bool:
        return bool(self.low_confidence_reasons)


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


def _extract_content_list(value: Any) -> list[dict[str, Any]] | None:
    """Find MinerU's structured content list without consuming image blocks."""
    if isinstance(value, dict):
        for key in _CONTENT_LIST_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str):
                try:
                    candidate = json.loads(candidate)
                except json.JSONDecodeError:
                    candidate = None
            if isinstance(candidate, list) and any(isinstance(item, dict) for item in candidate):
                return candidate
        for nested in value.values():
            candidate = _extract_content_list(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _extract_content_list(nested)
            if candidate:
                return candidate
    return None


def _content_list_to_markdown(items: list[dict[str, Any]]) -> str | None:
    """Rebuild text/table Markdown while dropping OCR attached to image regions."""
    blocks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or item.get("category") or "").lower()
        if block_type in _IMAGE_BLOCK_TYPES or any(
            token in block_type for token in ("image", "figure", "stamp", "watermark", "logo")
        ):
            continue
        if block_type == "table":
            value = item.get("table_body") or item.get("table_content") or item.get("text")
        else:
            value = item.get("text") or item.get("content")
        if isinstance(value, str) and value.strip():
            value = value.strip()
            raw_level = item.get("text_level", item.get("level"))
            try:
                text_level = int(raw_level)
            except (TypeError, ValueError):
                text_level = 1 if block_type in {"title", "heading", "header"} else 0
            if 1 <= text_level <= 6 and not value.startswith("#"):
                value = f"{'#' * text_level} {value}"
            blocks.append(value)
    return "\n\n".join(blocks) or None


def _strip_markdown_images(markdown: str) -> str:
    """Remove fallback Markdown/HTML image regions and their OCR-like alt text."""
    markdown = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)
    markdown = re.sub(r"<img\b[^>]*>", "", markdown, flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", markdown).strip()


def _extract_zip_markdown(content: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            content_lists = sorted(
                (
                    name
                    for name in archive.namelist()
                    if name.lower().endswith(".json") and "content_list" in name.lower()
                ),
                key=lambda name: (name.count("/"), len(name)),
            )
            for name in content_lists:
                try:
                    payload = json.loads(archive.read(name).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                items = payload if isinstance(payload, list) else _extract_content_list(payload)
                if isinstance(items, list) and (markdown := _content_list_to_markdown(items)):
                    return markdown
            markdown_files = sorted(
                (name for name in archive.namelist() if name.lower().endswith(".md")),
                key=lambda name: (name.count("/"), len(name)),
            )
            if not markdown_files:
                return None
            return _strip_markdown_images(archive.read(markdown_files[0]).decode("utf-8"))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return None


def _check_version(response: httpx.Response, expected_version: str) -> None:
    actual_version = ""
    for name in _MINERU_VERSION_HEADERS:
        if candidate := response.headers.get(name, "").strip():
            actual_version = candidate
            break

    if not actual_version:
        log.warning(
            "mineru_version_header_missing",
            extra={"expected_version": expected_version},
        )
    elif actual_version != expected_version:
        raise MinerUContractError(
            f"MinerU version mismatch: expected {expected_version}, got {actual_version}"
        )


def _decode_response(response: httpx.Response) -> tuple[str, list[dict[str, Any]]]:
    content_list: list[dict[str, Any]] = []
    content_type = response.headers.get("content-type", "").lower()
    if "zip" in content_type or response.content.startswith(b"PK"):
        markdown = _extract_zip_markdown(response.content)
    elif "json" in content_type:
        try:
            payload = response.json()
            extracted = _extract_content_list(payload)
            if extracted:
                content_list = extracted
            markdown = _content_list_to_markdown(content_list) if content_list else None
            if not markdown:
                markdown = _extract_markdown(payload)
        except ValueError as exc:
            raise MinerUError("MinerU returned invalid JSON") from exc
    else:
        markdown = response.text if response.text.strip() else None

    if not markdown or not markdown.strip():
        raise MinerUError("MinerU response contains no Markdown")
    return _strip_markdown_images(markdown), content_list


def _quality_result(markdown: str, content_list: list[dict[str, Any]]) -> MinerUParseResult:
    normalized = re.sub(r"\s+", "", markdown)
    labels = tuple(label for label in _KEY_LABELS if label in normalized)
    table_count = sum(
        1
        for item in content_list
        if str(item.get("type") or item.get("category") or "").lower() == "table"
    )
    if table_count == 0 and re.search(r"^\s*\|.+\|\s*$", markdown, re.MULTILINE):
        table_count = 1

    reasons: list[str] = []
    if len(normalized) < 20:
        reasons.append("insufficient_text_blocks")
    if table_count == 0 and not labels:
        reasons.append("no_table_or_key_labels")
    return MinerUParseResult(
        markdown=markdown,
        content_list=tuple(content_list),
        table_count=table_count,
        key_labels=labels,
        low_confidence_reasons=tuple(reasons),
    )


# ---- 共享 AsyncClient 单例（2026-09 收尾计划 §8 拍板，对齐 core/http_client 模式）----
# 连接池复用（页级 OCR 并行时收益最大）；timeout 在构造时固定（MinerU 全端点同超时）。
# 跨 loop 检测重建：AsyncClient 连接池绑定事件循环，pytest-asyncio 每测试新 loop
# 的场景检测到 loop 变化时重建，生产单 loop 常驻不受影响。
_async_client: httpx.AsyncClient | None = None
_async_client_loop: asyncio.AbstractEventLoop | None = None


def get_async_client() -> httpx.AsyncClient:
    """共享 AsyncClient 懒加载单例（连接池复用；测试可直接 monkeypatch 替换）。"""
    global _async_client, _async_client_loop
    loop = asyncio.get_running_loop()
    if _async_client is not None and _async_client_loop is not loop:
        # 跨事件循环（测试多 loop）：旧实例废弃由 GC 回收
        _async_client = None
    if _async_client is None:
        _async_client = httpx.AsyncClient(timeout=settings.mineru_timeout_seconds)
        _async_client_loop = loop
    return _async_client


async def parse_document_async(
    file_bytes: bytes,
    filename: str,
    *,
    mime_type: str | None = None,
) -> MinerUParseResult:
    """parse_document 的异步版：网络段走共享 AsyncClient 单例，其余逻辑逐行一致
    （前置校验/表单构造/版本契约/解码/质量评估复用同步纯函数）。"""
    if not settings.mineru_base_url.strip():
        raise MinerUError("MINERU_BASE_URL is empty")
    expected_version = settings.mineru_expected_version.strip()
    if not expected_version:
        raise MinerUContractError("MINERU_EXPECTED_VERSION must be pinned")

    endpoint = settings.mineru_endpoint.strip() or "/file_parse"
    url = f"{settings.mineru_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    headers: dict[str, str] = {}
    if settings.mineru_api_key:
        headers["Authorization"] = f"Bearer {settings.mineru_api_key}"

    safe_filename = Path(filename).name or "document.pdf"
    upload_mime = mime_type or mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
    form = _request_form(safe_filename)

    start = time.monotonic()
    try:
        client = get_async_client()
        response = await client.post(
            url,
            headers=headers,
            data=form,
            files={"files": (safe_filename, file_bytes, upload_mime)},
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MinerUError(f"MinerU request failed: {exc.__class__.__name__}") from exc

    _check_version(response, expected_version)
    markdown, content_list = _decode_response(response)
    result = _quality_result(markdown, content_list)
    log.info(
        "mineru_parse_done",
        extra={
            "file": safe_filename,
            "bytes": len(file_bytes),
            "duration_ms": round((time.monotonic() - start) * 1000, 1),
            "table_count": result.table_count,
            "low_confidence": result.low_confidence,
            "low_confidence_reasons": result.low_confidence_reasons,
        },
    )
    return result
