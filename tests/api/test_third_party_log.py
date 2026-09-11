"""第三方接口调用日志测试：record/query/过滤/落盘/回填/路由。"""

from __future__ import annotations

import json

import app.core.third_party_log_store as third_party_log


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

    from app.core.config import settings

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


def test_third_party_logs_page_returns_html():
    """内置第三方日志页面可访问（页面无数据免鉴权；数据接口 /api/third-party-logs 仍需 Key）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    resp = TestClient(app).get("/third-party-logs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "第三方接口调用日志" in resp.text


def test_query_status_range_tolerates_bad_status_code():
    """status_code 异常值（字符串，历史/外部写入）不参与范围过滤也不抛错。"""
    _record(status_code=200, duration_ms=1)
    third_party_log._entries.append(
        {
            "ts": "2026-09-11T00:00:00+00:00",
            "level": "INFO",
            "message": "third_party_response",
            "endpoint": "bad",
            "url": "u",
            "status_code": "200",  # 异常落盘：字符串
        }
    )
    assert third_party_log.query(status_min=100)["total"] == 1
    assert third_party_log.query(status_max=300)["total"] == 1


def test_record_carries_request_id_from_context():
    """请求上下文内的 request_id 自动附加到条目（与 /api/logs 审计条串联）。"""
    from app.core import log_support

    token = log_support.set_request_id("rid-link-1")
    try:
        _record(status_code=200)
    finally:
        log_support.reset_request_id(token)
    _record(status_code=204)  # 上下文外：request_id 为 None

    result = third_party_log.query(request_id="rid-link-1")
    assert result["total"] == 1
    assert result["items"][0]["request_id"] == "rid-link-1"
    # 无上下文条目：字段存在但为 None（不匹配任何 request_id 过滤）
    assert third_party_log.query()["items"][0]["request_id"] is None


def test_request_context_links_audit_and_third_party(monkeypatch):
    """端到端串联：中间件 set 的 request_id 经 asyncio context 传播到请求处理中的
    下游记录（call_next task 边界复制验证）——审计条与第三方条同 ID 可联查。"""
    from fastapi.testclient import TestClient

    from app.core import skill_registry
    from app.main import app

    skill = next(s for s in skill_registry.all_skills() if s.name == "tuoshu")

    async def fake_run(*, file_bytes, filename, options):
        # 模拟请求处理中的下游调用落盘（真实链路为 http_client 内部调用）
        third_party_log.record(
            "third_party_request",
            "INFO",
            endpoint="AddWork",
            url="https://s3.jxt56.com/Car/WorkOut/AddWork",
        )
        return {"result": {"source": {"file": filename, "doc_format": "docx"}}, "meta": {}}

    monkeypatch.setattr(skill, "run", fake_run)
    client = TestClient(app)
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.docx", b"fake content", "application/octet-stream")},
        headers={"x-request-id": "rid-e2e-1"},
    )
    assert resp.status_code == 200
    audit = client.get("/api/logs", params={"request_id": "rid-e2e-1"}).json()
    assert audit["total"] >= 1
    linked = client.get(
        "/api/third-party-logs", params={"request_id": "rid-e2e-1"}
    ).json()
    assert linked["total"] == 1
    assert linked["items"][0]["endpoint"] == "AddWork"


def test_query_ts_range():
    """时间范围过滤：闭区间；naive 边界按 UTC 解释，坏 ts 不匹配。"""
    from datetime import UTC, datetime, timedelta

    _record(status_code=200)
    now = datetime.now(UTC)
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)

    assert third_party_log.query(ts_from=past, ts_to=future)["total"] == 1
    assert third_party_log.query(ts_from=future)["total"] == 0
    assert third_party_log.query(ts_to=past)["total"] == 0
    # naive 边界与 aware 同值等价（按 UTC 解释）
    assert (
        third_party_log.query(
            ts_from=past.replace(tzinfo=None), ts_to=future.replace(tzinfo=None)
        )["total"]
        == 1
    )

