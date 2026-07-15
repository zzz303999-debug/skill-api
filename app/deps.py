"""通用依赖：鉴权等。"""

from __future__ import annotations

from fastapi import Header

from app.config import settings
from app.errors import UnauthorizedError


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if not settings.api_key:
        return
    if x_api_key != settings.api_key:
        raise UnauthorizedError("invalid or missing X-API-Key")
