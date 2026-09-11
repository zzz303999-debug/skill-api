"""日志支撑工具：请求上下文（request_id 串联）与日志查询的时间过滤。

- request_id 上下文：api 访问日志中间件在请求入口 set（call_next 前），
  core/http_client 落盘第三方日志时经 third_party_log_store.record 自动读取，
  使 ``/api/logs`` 审计条与 ``/api/third-party-logs`` 下游调用条可由同一
  request_id 串联（一次请求全链路排查）；非请求上下文（如脚本）为 None。
  asyncio 语义：call_next 下游任务创建时复制当前 context，set 后进入即为
  请求级隔离，跨请求不串号。
- 时间过滤：两个日志查询（access / third-party）共用同一口径——边界 naive
  按 UTC 解释（日志 ts 统一 UTC），条目 ts 解析失败视为不匹配（坏数据不
  混入过滤结果）。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(value: str) -> Token[str | None]:
    """设置当前请求上下文的 request_id，返回重置令牌。"""
    return _request_id_var.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    """按令牌恢复上下文（请求结束时调用，避免上下文残留）。"""
    _request_id_var.reset(token)


def get_request_id() -> str | None:
    """读取当前请求上下文的 request_id（无请求上下文时为 None）。"""
    return _request_id_var.get()


def normalize_ts_bound(value: datetime | None) -> datetime | None:
    """时间边界规范化：naive 按 UTC 解释（与日志 ts 存储口径一致）。"""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def entry_in_time_range(
    ts: Any,
    ts_from: datetime | None,
    ts_to: datetime | None,
) -> bool:
    """条目 ts（ISO8601 字符串）是否落在 [ts_from, ts_to]（闭区间）。

    无边界直接放行；ts 缺失/非法视为不匹配（过滤场景下坏数据不混入）。
    边界在函数内做一次幂等归一（naive 按 UTC）——查询入口已调用
    normalize_ts_bound，此处为直传调用方的双保险（防 naive 边界与 aware
    条目比较抛 TypeError）。
    """
    if ts_from is None and ts_to is None:
        return True
    ts_from = normalize_ts_bound(ts_from)
    ts_to = normalize_ts_bound(ts_to)
    try:
        parsed = datetime.fromisoformat(str(ts))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if ts_from is not None and parsed < ts_from:
        return False
    if ts_to is not None and parsed > ts_to:
        return False
    return True
