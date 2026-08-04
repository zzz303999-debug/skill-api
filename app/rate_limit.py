"""请求限流（内存滑动窗口）。

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

from fastapi.responses import JSONResponse

from app.errors import ERROR_CODE_DESCRIPTIONS

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
_LIGHT_PATH_PREFIXES = ("/api/logs",)
_FREE_PATHS = frozenset(
    {
        "/healthz",
        "/skills",
        "/logs",
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
