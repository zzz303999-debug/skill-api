"""请求限流中间件（内存滑动窗口，按客户端 IP）与客户端 IP 解析。

本文件同时承载限流算法（SlidingWindowLimiter，2026-09 结构整理并入）：
算法为中间件私有实现，同文件同居消除根级同名歧义。

设计约束：
- 当前部署为单进程 uvicorn（Dockerfile 无 --workers），进程内存计数即精确；
  若未来改为多 worker，需换成 Redis 等共享存储，否则各 worker 独立计数。
- 服务重启后计数清零，可接受（限流用于防滥用，不做强隔离）。
- 被限流的请求仍会经过外层 access_log 中间件，保证审计留痕完整。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.api.response_shell import _unified_error_body, _use_unified_response
from app.core.config import settings
from app.core.errors import ERROR_CODE_DESCRIPTIONS

# key 数量上限：防止空闲 IP 的时间戳记录长期滞留导致内存缓慢膨胀；
# 超限时清掉最旧的一半（简单 LRU 近似）
_MAX_KEYS = 10_000


class SlidingWindowLimiter:
    """按 key（如客户端 IP）的滑动窗口限流器。

    allow() 记录请求时间戳，窗口内超过 max_requests 时拒绝并返回
    Retry-After（最早一次请求还需多久滑出窗口）。
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """返回 (是否允许, 被拒时需等待秒数)。"""
        with self._lock:
            now = time.monotonic()
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= _MAX_KEYS:
                    self._evict_oldest_half()
                bucket = deque()
                self._hits[key] = bucket
            while bucket and bucket[0] <= now - self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                # bucket 可能为空（max_requests=0 等防御场景），此时等待整个窗口
                wait = self.window_seconds - (now - bucket[0]) if bucket else self.window_seconds
                return False, max(wait, 0.0)
            bucket.append(now)
            return True, 0.0

    def _evict_oldest_half(self) -> None:
        """key 数超限时清掉最旧一半，控制内存占用。"""
        if not self._hits:
            return
        ordered = sorted(self._hits, key=lambda k: self._hits[k][-1])
        for key in ordered[: len(ordered) // 2]:
            del self._hits[key]


# ---- 路径分类：heavy（LLM/转换密集型）/ light（日志查询）/ None（不限） ----

_HEAVY_PATH_PREFIXES = ("/orders", "/skills/")
_LIGHT_PATH_PREFIXES = ("/api/logs", "/api/third-party-logs")
_FREE_PATHS = frozenset(
    {
        "/healthz",
        "/skills",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
    }
)


def classify_path(path: str) -> str | None:
    """把请求路径分成限流档位：heavy / light / None（不限制）。"""
    if path in _FREE_PATHS:
        return None
    if path.startswith(_HEAVY_PATH_PREFIXES):
        return "heavy"
    if path.startswith(_LIGHT_PATH_PREFIXES):
        return "light"
    return None


def build_rate_limited_response(retry_after: float) -> JSONResponse:
    """构造 429 响应：错误格式与 SkillAPIError 响应一致 + Retry-After 头。"""
    seconds = math.ceil(retry_after)
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "rate_limited",
                "message": "too many requests, please retry later",
                "description": ERROR_CODE_DESCRIPTIONS["rate_limited"],
                "details": {"retry_after_seconds": seconds},
            }
        },
        headers={"Retry-After": str(seconds)},
    )


# ---- 中间件 ----

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
