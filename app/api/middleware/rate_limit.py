"""请求限流中间件（内存滑动窗口，按客户端 IP）与客户端 IP 解析。"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.api.response_shell import _unified_error_body, _use_unified_response
from app.config import settings
from app.errors import ERROR_CODE_DESCRIPTIONS
from app.rate_limit import SlidingWindowLimiter, build_rate_limited_response, classify_path

# 请求限流（内存滑动窗口，按客户端 IP；单进程部署下精确）
_heavy_limiter = SlidingWindowLimiter(
    max_requests=settings.rate_limit_heavy_max_requests,
    window_seconds=settings.rate_limit_heavy_window_seconds,
)
_light_limiter = SlidingWindowLimiter(
    max_requests=settings.rate_limit_light_max_requests,
    window_seconds=settings.rate_limit_light_window_seconds,
)
_LIMITERS = {"heavy": _heavy_limiter, "light": _light_limiter}


def _resolve_client_ip(request: Request) -> tuple[str | None, str | None]:
    """解析客户端 IP，返回 (客户端IP, 原始X-Forwarded-For头)。

    云服务前面通常有 nginx/负载均衡，access_log_trust_proxy 开启时：
    - X-Forwarded-For 存在时取**最后一个**地址（nginx `$proxy_add_x_forwarded_for`
      是追加语义，最后一个即离本服务最近的代理看到的真实客户端 IP）；
      客户端自行伪造的前缀地址被忽略，限流与审计 IP 不可被污染；
    - 其次 X-Real-IP；均不存在或未开启信任时回退到直连地址。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if settings.access_log_trust_proxy:
        if forwarded:
            parts = [part.strip() for part in forwarded.split(",") if part.strip()]
            if parts:
                return parts[-1], forwarded
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip(), forwarded
    host = request.client.host if request.client else None
    return host, forwarded


async def _rate_limit_middleware(request: Request, call_next: Callable) -> Any:
    """请求限流：按客户端 IP 对 heavy/light 档接口滑动窗口计数。

    注册在 access_log 中间件之前（执行时处于其内层），被 429 拒绝的请求
    仍会经过外层 access_log，审计留痕完整；限流计数在事件循环线程执行。
    """
    if not settings.rate_limit_enabled:
        return await call_next(request)
    group = classify_path(request.url.path)
    if group is None:
        return await call_next(request)
    client_ip, _ = _resolve_client_ip(request)
    # 免限流白名单经 app.main 命名空间解析：测试以 setattr(main_module,
    # "_rate_limit_whitelist", ...) 注入替身（保持拆分前的 patch 点不变，2026-09）
    from app.main import _rate_limit_whitelist

    if not client_ip or client_ip in _rate_limit_whitelist:
        return await call_next(request)
    allowed, retry_after = _LIMITERS[group].allow(client_ip)
    if not allowed:
        request.state.error_code = "rate_limited"
        request.state.error_detail = {
            "code": "rate_limited",
            "message": "too many requests",
            "description": ERROR_CODE_DESCRIPTIONS["rate_limited"],
            "details": {"retry_after_seconds": math.ceil(retry_after)},
        }
        seconds = math.ceil(retry_after)
        if _use_unified_response(request.url.path):
            return JSONResponse(
                status_code=429,
                content=_unified_error_body(
                    "rate_limited",
                    ERROR_CODE_DESCRIPTIONS["rate_limited"],
                    {"retry_after_seconds": seconds},
                ),
                headers={"Retry-After": str(seconds)},
            )
        return build_rate_limited_response(retry_after)
    return await call_next(request)
