"""log_support 单元测试：请求上下文（request_id）与时间过滤工具。

端到端串联（中间件→上下文→第三方日志）见 tests/api/test_third_party_log.py；
本文件覆盖工具函数本身的边界行为。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core import log_support


def test_request_id_context_set_get_reset():
    assert log_support.get_request_id() is None
    token = log_support.set_request_id("rid-1")
    assert log_support.get_request_id() == "rid-1"
    log_support.reset_request_id(token)
    assert log_support.get_request_id() is None


def test_normalize_ts_bound():
    naive = datetime(2026, 9, 11, 5, 0, 0)
    normalized = log_support.normalize_ts_bound(naive)
    assert normalized == naive.replace(tzinfo=UTC)
    aware = datetime(2026, 9, 11, 13, 0, 0, tzinfo=UTC)
    assert log_support.normalize_ts_bound(aware) is aware  # 幂等：aware 原样返回
    assert log_support.normalize_ts_bound(None) is None


def test_entry_in_time_range_no_bounds_passes():
    assert log_support.entry_in_time_range("bad-ts", None, None) is True


def test_entry_in_time_range_aware_bounds():
    ts = "2026-09-11T05:00:00.000+00:00"
    before = datetime(2026, 9, 11, 4, 0, tzinfo=UTC)
    after = datetime(2026, 9, 11, 6, 0, tzinfo=UTC)
    assert log_support.entry_in_time_range(ts, before, after) is True
    assert log_support.entry_in_time_range(ts, after, None) is False
    assert log_support.entry_in_time_range(ts, None, before) is False
    # 闭区间：边界恰好等于条目 ts 时命中
    exact = datetime(2026, 9, 11, 5, 0, tzinfo=UTC)
    assert log_support.entry_in_time_range(ts, exact, None) is True
    assert log_support.entry_in_time_range(ts, None, exact) is True


def test_entry_in_time_range_naive_bounds_not_raise():
    """naive 边界直传不抛 TypeError（内部归一，防调用方忘 normalize）。"""
    ts = "2026-09-11T05:00:00.000+00:00"
    naive_before = datetime(2026, 9, 11, 4, 0)  # 无时区 → 按 UTC 解释
    naive_after = datetime(2026, 9, 11, 6, 0)
    assert log_support.entry_in_time_range(ts, naive_before, naive_after) is True
    assert log_support.entry_in_time_range(ts, naive_after, None) is False


def test_entry_in_time_range_naive_entry_ts_treated_as_utc():
    """条目 ts 无时区时按 UTC 解释。"""
    naive_ts = "2026-09-11T05:00:00"
    before = datetime(2026, 9, 11, 4, 0, tzinfo=UTC)
    after = datetime(2026, 9, 11, 6, 0, tzinfo=UTC)
    assert log_support.entry_in_time_range(naive_ts, before, after) is True


def test_entry_in_time_range_bad_ts_not_matched():
    before = datetime(2000, 1, 1, tzinfo=UTC)
    for bad in ("", "not-a-time", None, 12345):
        assert log_support.entry_in_time_range(bad, before, None) is False
