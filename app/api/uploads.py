"""上传文件读取与校验（各上传路由共用）。"""

from __future__ import annotations

from fastapi import UploadFile

from app.config import settings
from app.errors import BadRequestError


async def _read_upload(file: UploadFile) -> bytes:
    """读取上传文件字节并校验：超大小上限抛 file_too_large，空文件抛 empty_file。"""
    content = await file.read(settings.api_max_upload_bytes + 1)
    if len(content) > settings.api_max_upload_bytes:
        raise BadRequestError(
            "uploaded file is too large",
            code="file_too_large",
            details={"max_bytes": settings.api_max_upload_bytes},
        )
    if not content:
        raise BadRequestError(
            "uploaded file is empty",
            code="empty_file",
            details={"file": file.filename or "unnamed"},
        )
    return content
