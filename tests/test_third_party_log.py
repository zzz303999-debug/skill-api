"""第三方接口调用日志测试：record/query/过滤/落盘/回填/路由。"""

from __future__ import annotations

import json

import app.third_party_log as third_party_log


def _record(
    message="third_party_response",
    level="INFO",
    endpoint="AddWork",
    url="https://s3.jxt56.com/Car/WorkOut/AddWork",
    **extra,
) -> None:
    third_party_log.record(message, level, endpoint=endpoint, url=url, **extra)


def test_record_and_query_newest_first():
    _record(status_code=200, duration_ms=5)
    _record("third_party_request", endpoint="addBill", url="https://x/addBill")
    result = third_party_log.query()
    assert result["total"] == 2
    # 最新在前
    assert result["items"][0]["endpoint"] == "addBill"
    assert result["items"][1]["endpoint"] == "AddWork"
    assert result["items"][0]["message"] == "third_party_request"
    assert result["items"][0]["level"] == "INFO"
    assert "ts" in result["items"][0]


def test_query_filters():
    _record(status_code=200, duration_ms=3)
    _record(status_code=204, duration_ms=4)
    _record("third_party_request_error", "WARNING", endpoint="addBill", error_type="TimeoutException")

    assert third_party_log.query(message="request_error")["total"] == 1
    assert third_party_log.query(endpoint="addbill")["total"] == 1  # 大小写不敏感子串
    assert third_party_log.query(endpoint="addwork")["total"] == 2
    assert third_party_log.query(status=204)["total"] == 1
    assert third_party_log.query(status_min=200)["total"] == 2  # 200/204 两档
    assert third_party_log.query(status_max=200)["total"] == 1
    assert third_party_log.query(level="warning")["total"] == 1


def test_query_pagination():
    for i in range(5):
        _record(status_code=200, duration_ms=i)
    page1 = third_party_log.query(limit=2, offset=0)
    page2 = third_party_log.query(limit=2, offset=2)
    assert page1["total"] == 5 and len(page1["items"]) == 2
    assert page2["items"][0]["duration_ms"] == 2  # 倒序：offset=2 起


def test_record_writes_daily_file():
    _record(status_code=200, duration_ms=1)
    files = list(third_party_log._log_dir().glob("third-party-*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["endpoint"] == "AddWork"
    assert entry["message"] == "third_party_response"
    assert entry["status_code"] == 200


def test_reload_backfills_from_file(monkeypatch):
    """服务重启后：内存清空 + _loaded=False → query 从按天文件尾部回填。"""
    _record(status_code=200, duration_ms=7)
    # 模拟重启：缓冲清空、未加载
    monkeypatch.setattr(third_party_log, "_loaded", False)
    from collections import deque

    monkeypatch.setattr(
        third_party_log,
        "_entries",
        deque(maxlen=third_party_log._MAX_MEMORY_ENTRIES),
    )
    result = third_party_log.query()
    assert result["total"] == 1
    assert result["items"][0]["duration_ms"] == 7


def test_cleanup_expired_removes_old_daily_files():
    """超过 storage_keep_hours（向上取整天）的按天文件被清理。"""
    import math
    from datetime import date, timedelta

    from app.config import settings

    log_dir = third_party_log._log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    keep_days = max(1, math.ceil(max(settings.storage_keep_hours, 1) / 24))
    old = log_dir / (
        "third-party-" + (date.today() - timedelta(days=keep_days + 5)).isoformat() + ".jsonl"
    )
    fresh = log_dir / ("third-party-" + date.today().isoformat() + ".jsonl")
    old.write_text('{"ts": "old"}\n', encoding="utf-8")
    fresh.write_text('{"ts": "fresh"}\n', encoding="utf-8")
    third_party_log._cleanup_expired()
    assert not old.exists()
    assert fresh.exists()


def test_record_failure_does_not_raise(monkeypatch):
    """落盘失败只记 warning 不冒泡（日志不阻断业务）。"""

    class BoomDir:
        """落盘目录替身：mkdir/open 全部抛 OSError，glob 返回空。"""

        def __truediv__(self, _name):
            return self

        @property
        def parent(self):
            return self

        def glob(self, _pattern):
            return []

        def mkdir(self, *_a, **_k):
            raise OSError("disk full")

        def open(self, *_a, **_k):
            raise OSError("disk full")

    monkeypatch.setattr(third_party_log, "_log_dir", lambda: BoomDir())
    _record(status_code=200)  # 不应抛异常


def test_routes(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(
        "app.config.settings.api_key", "", raising=False
    )  # conftest 已关闭鉴权，此处防回归
    _record(status_code=200, duration_ms=9)
    with TestClient(app) as client:
        resp = client.get("/api/third-party-logs?endpoint=AddWork")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["items"][0]["endpoint"] == "AddWork"

        page = client.get("/third-party-logs")
        assert page.status_code == 200
        assert "第三方接口调用日志" in page.text
