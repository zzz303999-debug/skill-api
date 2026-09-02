"""接口鉴权中间件：配置了 api_key 时校验 Bearer / X-API-Key。"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.api.response_shell import _unified_error_body, _use_unified_response
from app.config import settings
from app.errors import ERROR_CODE_DESCRIPTIONS


async def _auth_middleware(request: Request, call_next: Callable) -> Any:
    """接口鉴权：配置了 api_key 时校验 Bearer / X-API-Key。

    注册在 access_log 与 rate_limit 之间：未授权请求同样留下审计记录，
    且不消耗限流配额。未配置 api_key 时直接放行（仅限可信内网部署）。
    """
    if not settings.api_key:
        return await call_next(request)
    # 豁免路径经 app.main 命名空间解析：测试以 setattr(main_module,
    # "_AUTH_FREE_PATHS", ...) 注入替身（保持拆分前的 patch 点不变，2026-09）
    from app.main import _AUTH_FREE_PATHS

    # 去尾斜杠匹配（2026-08-27 审查修正）：尾斜杠请求（307 重定向前经中间件）
    # 若不归一会被误判为非豁免路径 → 401；与错误外壳归一口径一致
    if request.url.path.rstrip("/") in _AUTH_FREE_PATHS:
        return await call_next(request)
    # 常量时间比较，避免时序侧信道泄露 api_key 信息
    auth = request.headers.get("authorization", "")
    auth_ok = False
    if auth.startswith("Bearer "):
        auth_ok = hmac.compare_digest(auth[7:].encode(), settings.api_key.encode())
    header_key = request.headers.get("x-api-key")
    if not auth_ok and header_key:
        auth_ok = hmac.compare_digest(header_key.encode(), settings.api_key.encode())
    if auth_ok:
        return await call_next(request)
    request.state.error_code = "unauthorized"
    request.state.error_detail = {
        "code": "unauthorized",
        "message": "invalid or missing API key",
        "description": ERROR_CODE_DESCRIPTIONS["unauthorized"],
        "details": None,
    }
    content = (
        _unified_error_body("unauthorized", ERROR_CODE_DESCRIPTIONS["unauthorized"])
        if _use_unified_response(request.url.path)
        else {
            "error": {
                "code": "unauthorized",
                "message": "invalid or missing API key",
                "description": ERROR_CODE_DESCRIPTIONS["unauthorized"],
                "details": None,
            }
        }
    )
    return JSONResponse(
        status_code=401,
        content=content,
        headers={"WWW-Authenticate": "Bearer"},
    )
