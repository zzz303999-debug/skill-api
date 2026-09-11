"""请求访问日志查询（/api/logs）扩展过滤测试：时间范围/业务码/状态范围/方法/耗时。

基础过滤（path/file/status/ip/request_id）见 test_access_log.py；
本文件覆盖 2026-09-11 新增的排障向过滤参数。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app


def test_query_filter_method():
    client = TestClient(app)
    client.get("/healthz")
    assert client.get("/api/logs", params={"method": "get"}).json()["total"] >= 1
    # 大小写不敏感；POST 无记录
    assert client.get("/api/logs", params={"method": "POST"}).json()["total"] == 0


def test_query_filter_status_range():
    client = TestClient(app)
    client.get("/healthz")  # 200
    assert client.get("/api/logs", params={"status_min": 200}).json()["total"] >= 1
    assert (
        client.get("/api/logs", params={"status_min": 200, "status_max": 299}).json()[
            "total"
        ]
        >= 1
    )
    assert client.get("/api/logs", params={"status_min": 400}).json()["total"] == 0
    assert client.get("/api/logs", params={"status_max": 199}).json()["total"] == 0


def test_query_filter_min_duration():
    client = TestClient(app)
    client.get("/healthz")
    assert client.get("/api/logs", params={"min_duration_ms": 0}).json()["total"] >= 1
    # 超长阈值：本地请求不可能达到
    assert (
        client.get("/api/logs", params={"min_duration_ms": 600_000}).json()["total"] == 0
    )


def test_query_filter_ts_range_http():
    """HTTP 层 ISO8601 解析：aware 与 naive（按 UTC）边界均生效。"""
    client = TestClient(app)
    client.get("/healthz")
    now = datetime.now(UTC)
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)

    hit = client.get(
        "/api/logs",
        params={"ts_from": past.isoformat(), "ts_to": future.isoformat()},
    ).json()
    assert hit["total"] >= 1

    miss = client.get("/api/logs", params={"ts_from": future.isoformat()}).json()
    assert miss["total"] == 0

    # naive 边界按 UTC 解释：与 aware 同值等价
    naive = client.get(
        "/api/logs",
        params={
            "ts_from": past.replace(tzinfo=None).isoformat(),
            "ts_to": future.replace(tzinfo=None).isoformat(),
        },
    ).json()
    assert naive["total"] >= 1

    # 非法时间格式化 422（FastAPI 参数校验）
    bad = client.get("/api/logs", params={"ts_from": "not-a-time"})
    assert bad.status_code == 422


def test_query_filter_business_code():
    """按统一外壳业务码过滤（response 为 JSON 文本，解析 code 比较）。"""
    import app.core.access_log_store as access_log

    access_log.record(
        {
            "request_id": "rid-code-1",
            "ip": "127.0.0.1",
            "x_forwarded_for": None,
            "user_agent": "pytest",
            "method": "POST",
            "path": "/orders/bill/import",
            "file": "a.xlsx",
            "file_size": 1,
            "body": None,
            "body_truncated": False,
            "response": '{"code":"204","msg":"添加失败","data":null}',
            "response_truncated": False,
            "response_summarized": False,
            "response_full": None,
            "response_full_truncated": False,
            "status": 200,
            "error_code": None,
            "error": None,
            "duration_ms": 12.3,
        }
    )
    client = TestClient(app)
    hit = client.get("/api/logs", params={"code": "204"}).json()
    assert hit["total"] == 1
    assert hit["items"][0]["request_id"] == "rid-code-1"
    assert client.get("/api/logs", params={"code": "200"}).json()["total"] == 0
