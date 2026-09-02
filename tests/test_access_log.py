"""请求访问日志（/logs 页面与 /api/logs 接口）测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core import registry
from app.main import app


def _items(payload: dict) -> list[dict]:
    return payload["items"]


def test_logs_page_returns_html():
    resp = TestClient(app).get("/logs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "请求日志" in resp.text


def test_bill_import_page_returns_html():
    resp = TestClient(app).get("/bill-import")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "竞品账单导入" in resp.text


def test_bill_import_help_page_returns_html():
    resp = TestClient(app).get("/bill-import-help")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "用户操作手册" in resp.text


def test_request_recorded_with_fields():
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    payload = client.get("/api/logs").json()
    assert payload["total"] >= 1
    entry = _items(payload)[0]
    assert entry["method"] == "GET"
    assert entry["path"] == "/healthz"
    assert entry["status"] == 200
    assert entry["request_id"]
    assert entry["duration_ms"] >= 0
    assert entry["ts"]


def test_json_response_body_recorded_for_audit():
    """JSON 响应体（输出结果）记录到日志，供前端 logs 页面展示。"""
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    entry = _items(client.get("/api/logs").json())[0]
    assert entry["path"] == "/healthz"
    assert entry["response"] is not None
    assert "ok" in entry["response"]  # healthz 响应体
    assert entry["response_truncated"] is False


def test_response_body_capture_keeps_client_payload_intact(monkeypatch):
    """日志捕获响应体后，客户端仍收到完整响应（不受截断影响）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "access_log_response_max_chars", 8)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"  # 客户端收到完整响应
    entry = _items(client.get("/api/logs").json())[0]
    assert entry["response_truncated"] is True
    assert len(entry["response"]) <= 8


def test_non_json_response_not_captured():
    """非 JSON 响应（HTML 页面）不记录响应体。"""
    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    entry = _items(client.get("/api/logs").json())[0]
    assert entry["path"] == "/docs"
    assert entry["response"] is None


def test_log_endpoints_not_recorded():
    client = TestClient(app)
    client.get("/logs")
    client.get("/api/logs")
    client.get("/healthz")
    payload = client.get("/api/logs").json()
    paths = {entry["path"] for entry in _items(payload)}
    assert paths == {"/healthz"}


def test_query_filters():
    client = TestClient(app)
    client.get("/healthz")
    payload = client.get("/api/logs", params={"path": "/healthz", "status": 200}).json()
    assert all(e["path"] == "/healthz" for e in _items(payload))
    payload = client.get("/api/logs", params={"path": "/no-such-path"}).json()
    assert payload["total"] == 0


def test_error_code_recorded_on_skill_error():
    client = TestClient(app)
    # 空内容文件触发 BadRequestError（code=empty_file），文件名在读取前已记录
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("empty.docx", b"", "application/octet-stream")},
    )
    assert resp.status_code == 400
    payload = client.get("/api/logs", params={"status": 400}).json()
    entry = _items(payload)[0]
    assert entry["error_code"] == "empty_file"
    assert entry["file"] == "empty.docx"
    # 完整错误详情（code/message/description/details）透传日志，供审计导出
    assert entry["error"] == {
        "code": "empty_file",
        "message": "uploaded file is empty",
        "description": "上传文件为空，请重新上传有效文件",
        "details": {"file": "empty.docx"},
    }


def test_extract_records_filename(monkeypatch):
    skill = next(s for s in registry.all_skills() if s.name == "tuoshu")

    async def fake_run(*, file_bytes, filename, options):
        return {
            "result": {"source": {"file": filename, "doc_format": "docx"}},
            "meta": {},
        }

    monkeypatch.setattr(skill, "run", fake_run)
    client = TestClient(app)
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("托书-测试.docx", b"fake content", "application/octet-stream")},
    )
    assert resp.status_code == 200
    payload = client.get("/api/logs", params={"file": "托书-测试"}).json()
    entry = _items(payload)[0]
    assert entry["file"] == "托书-测试.docx"
    assert entry["path"] == "/skills/tuoshu/extract"
    assert entry["status"] == 200


def test_x_request_id_respected():
    client = TestClient(app)
    client.get("/healthz", headers={"x-request-id": "trace-abc-123"})
    payload = client.get("/api/logs", params={"request_id": "trace-abc-123"}).json()
    assert payload["total"] >= 1
    assert _items(payload)[0]["request_id"] == "trace-abc-123"


def test_ip_and_user_agent_recorded():
    client = TestClient(app)
    client.get(
        "/healthz",
        headers={
            "user-agent": "audit-agent/1.0",
            "x-forwarded-for": "203.0.113.7, 10.0.0.1",
        },
    )
    entry = _items(client.get("/api/logs").json())[0]
    # 取 X-Forwarded-For 最后一个地址（nginx $proxy_add_x_forwarded_for 追加
    # 语义下是离服务最近的代理看到的真实客户端 IP），客户端伪造前缀被忽略
    assert entry["ip"] == "10.0.0.1"
    assert entry["x_forwarded_for"] == "203.0.113.7, 10.0.0.1"
    assert entry["user_agent"] == "audit-agent/1.0"


def test_xff_spoofed_prefix_ignored_for_rate_limit():
    """客户端伪造的 XFF 前缀不影响限流 key：取最后一个地址。"""
    from app.main import _resolve_client_ip

    client = TestClient(app)
    response = client.get(
        "/healthz",
        headers={"x-forwarded-for": "1.2.3.4, 198.51.100.7"},
    )
    # TestClient 中 request 可从 app 中间件链路验证：直接检查解析函数行为
    assert _resolve_client_ip(response.request)[0] == "198.51.100.7"


def test_ip_falls_back_to_peer_when_proxy_not_trusted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "access_log_trust_proxy", False)
    client = TestClient(app)
    client.get("/healthz", headers={"x-forwarded-for": "203.0.113.7"})
    entry = _items(client.get("/api/logs").json())[0]
    # 不信任代理头时回退到直连地址（TestClient 固定为 testclient）
    assert entry["ip"] == "testclient"


def test_query_filter_by_ip():
    client = TestClient(app)
    client.get("/healthz", headers={"x-forwarded-for": "198.51.100.9"})
    payload = client.get("/api/logs", params={"ip": "198.51.100"}).json()
    assert payload["total"] >= 1
    assert all(e["ip"] == "198.51.100.9" for e in _items(payload))
    payload = client.get("/api/logs", params={"ip": "192.0.2.1"}).json()
    assert payload["total"] == 0


def test_json_body_recorded_for_orders(monkeypatch):
    import app.main as main_module

    async def fake_publish(order_data, *, room_id, user_id):
        return {"code": "200", "msg": "ok", "data": []}

    monkeypatch.setattr(main_module, "_publish_order", fake_publish)
    client = TestClient(app)
    resp = client.post(
        "/orders",
        json={
            "content": "提单号：KMTCSHAP950393；托运人：海丰",
            "roomId": "audit-room-1",
            "userId": "10",
        },
    )
    assert resp.status_code == 200
    entry = _items(client.get("/api/logs", params={"path": "/orders"}).json())[0]
    assert entry["ip"] == "testclient"
    assert entry["body"]
    assert "KMTCSHAP950393" in entry["body"]
    assert "audit-room-1" in entry["body"]
    assert entry["body_truncated"] is False


def test_json_body_truncated_when_over_limit(monkeypatch):
    import app.main as main_module
    from app.config import settings

    monkeypatch.setattr(settings, "access_log_body_max_chars", 64)

    async def fake_publish(order_data, *, room_id, user_id):
        return {"code": "200", "msg": "ok", "data": []}

    monkeypatch.setattr(main_module, "_publish_order", fake_publish)
    client = TestClient(app)
    resp = client.post(
        "/orders",
        # 内容超长（含必要字段），验证日志侧截断不影响业务解析
        json={
            "content": "提单号：KMTCSHAP950393；托运人：海丰；" + "A" * 500,
            "roomId": "audit-room-2",
            "userId": "10",
        },
    )
    assert resp.status_code == 200
    entry = _items(client.get("/api/logs", params={"path": "/orders"}).json())[0]
    assert entry["body_truncated"] is True
    assert len(entry["body"]) <= 64


def test_http_error_details_recorded_for_non_business_failures():
    client = TestClient(app)
    # 缺字段触发 FastAPI 校验 422（非业务异常），审计也应有错误信息
    resp = client.post("/orders", json={})
    assert resp.status_code == 422
    entry = _items(client.get("/api/logs", params={"status": 422}).json())[0]
    assert entry["error"] == {"code": "http_422", "message": "HTTP 422", "details": None}


def test_extract_records_file_size(monkeypatch):
    skill = next(s for s in registry.all_skills() if s.name == "tuoshu")

    async def fake_run(*, file_bytes, filename, options):
        return {
            "result": {"source": {"file": filename, "doc_format": "docx"}},
            "meta": {},
        }

    monkeypatch.setattr(skill, "run", fake_run)
    client = TestClient(app)
    client.post(
        "/skills/tuoshu/extract",
        files={"file": ("audit.docx", b"fake-content-12b", "application/octet-stream")},
    )
    entry = _items(client.get("/api/logs", params={"file": "audit.docx"}).json())[0]
    assert entry["file"] == "audit.docx"
    assert entry["file_size"] == len(b"fake-content-12b")
    # 文件上传是 multipart，不记录请求体
    assert entry["body"] is None
    assert entry["body_truncated"] is False


def _build_yinghui_bill_bytes() -> bytes:
    """构造最小竞品账单（赢辉家族表头，L2 命中）：1 行数据 = 1 票。"""
    from io import BytesIO

    from openpyxl import Workbook

    headers = [
        "做箱日期", "运单编号", "客户名称", "提单号", "箱型", "提箱堆场", "装卸工厂",
        "港区", "车队", "车牌号", "司机", "司机手机", "进出口", "业务类型",
        "装卸地点", "船名", "航次", "箱号", "封条号",
        "运费", "待时费", "洋山费", "预提费", "落箱费", "应收合计",
        "油费", "出车费", "应付合计", "上下车费", "成本合计",
    ]
    values = [
        "2026-08-10", "LOG260001-1", "审计客户", "BL2026081001", "40HQ*1",
        "测试堆场", "测试门点", "外港", "测试车队", "沪A12345", "王师傅", "13800000000",
        "出口", "出口整箱", "测试门点", "COSCO TEST", "001E", "TCLU1000001", "SEAL000001",
        100.0, None, None, None, None, None, None, None, None, None, None,
    ]
    wb = Workbook()
    ws = wb.active
    for col, name in enumerate(headers, start=1):
        ws.cell(row=2, column=col, value=name)
    for col, value in enumerate(values, start=1):
        ws.cell(row=3, column=col, value=value)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_bill_import_response_summarized_for_log():
    """竞品录入大响应：日志只落排查摘要，客户端仍收到完整响应。"""
    import json

    client = TestClient(app)
    resp = client.post(
        "/orders/bill/import",
        files={
            "file": (
                "audit-bill.xlsx",
                _build_yinghui_bill_bytes(),
                "application/octet-stream",
            )
        },
        data={"create_order": "false"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # 统一响应外壳（code/msg/data）：客户端响应完整（明细不受摘要影响）
    assert body["code"] == "200" and body["msg"] == "请求成功"
    assert body["data"]["order_count"] == 1
    assert "canonical_orders" in body["data"]
    entry = _items(
        client.get("/api/logs", params={"path": "/orders/bill/import"}).json()
    )[0]
    assert entry["response_summarized"] is True
    assert entry["response_truncated"] is False
    summary = json.loads(entry["response"])
    # 摘要保留排查关键字段，丢弃大明细
    assert summary["code"] == "200"  # 统一外壳业务码保留（排查直接可见）
    assert summary["msg"] == "请求成功"
    assert summary["file"] == "audit-bill.xlsx"
    assert summary["order_count"] == 1
    assert summary["total_rows"] == 1
    assert summary["create_order"] is False
    assert summary.get("summary") is None  # preview 无建单统计
    assert summary["meta"]["template"]  # 模板命中回显
    assert "canonical_orders" not in summary
    assert "orders" not in summary
    assert "upstream" not in summary


def test_bill_import_409_summarized_for_log(monkeypatch):
    """409（create 全 skipped）同样走日志摘要：code=409 保留、明细不入摘要、
    审计 error_code 保持旧口径 duplicate_bill（审查修正 2026-08-27 补测）。"""
    import json

    import app.main as main_module
    from app.orders.bill import BillParseResult

    summary = {
        "total": 1,
        "success": 1,
        "failed": 0,
        "skipped": 1,
        "created": 0,
        "success_sns": ["EX26080042"],
        "failed_details": [],
    }

    def fake_build_result(**kwargs):
        return BillParseResult(
            file=kwargs["filename"],
            total_rows=1,
            order_count=1,
            create_order=True,
            summary=summary,
            meta={},
        )

    monkeypatch.setattr(main_module, "build_result", fake_build_result)
    client = TestClient(app)
    resp = client.post(
        "/orders/bill/import",
        files={"file": ("dup-bill.xlsx", b"x", "application/octet-stream")},
        data={"create_order": "true"},
        headers={"sk": "sk-1"},
    )
    assert resp.status_code == 409
    entry = _items(
        client.get("/api/logs", params={"path": "/orders/bill/import"}).json()
    )[0]
    assert entry["response_summarized"] is True
    assert entry["error_code"] == "duplicate_bill"  # 审计列保持旧口径（审查修正）
    summary_log = json.loads(entry["response"])
    assert summary_log["code"] == "409"
    assert summary_log["msg"] == "账单已全部创建过"
    assert summary_log["summary"]["skipped"] == 1
    assert "orders" not in summary_log
    assert "canonical_orders" not in summary_log


def test_bill_import_summarize_disabled_when_paths_empty(monkeypatch):
    """access_log_summarize_paths 置空时恢复完整响应记录。"""
    from app.config import settings

    monkeypatch.setattr(settings, "access_log_summarize_paths", "")
    client = TestClient(app)
    resp = client.post(
        "/orders/bill/import",
        files={
            "file": (
                "audit-bill.xlsx",
                _build_yinghui_bill_bytes(),
                "application/octet-stream",
            )
        },
        data={"create_order": "false"},
    )
    assert resp.status_code == 200
    entry = _items(
        client.get("/api/logs", params={"path": "/orders/bill/import"}).json()
    )[0]
    assert entry["response_summarized"] is False
    assert "canonical_orders" in entry["response"]


def test_summarize_import_response_fallback_on_unparseable():
    """摘要函数对非 JSON / 非对象响应回退 None（调用方保持原文）。"""
    from app.main import _summarize_import_response

    assert _summarize_import_response("not json") is None
    assert _summarize_import_response("[1, 2]") is None
    summary = _summarize_import_response(
        '{"file": "a.xlsx", "order_count": 2, "meta": {"master_data": null}}'
    )
    assert summary is not None
    assert '"order_count":2' in summary


def test_summarize_import_response_truncates_large_meta():
    """未映射表头与建档明细超限时只留前 N 条 + 截断计数。"""
    import json

    from app.main import _summarize_import_response

    payload = {
        "file": "a.xlsx",
        "order_count": 3,
        "meta": {
            "unmatched_headers": [f"h{i}" for i in range(50)],
            "master_data": {
                "mode": "preview",
                "candidates": {"client": 5},
                "degraded": [],
                "archived": [{"kind": "client", "key": f"k{i}"} for i in range(15)],
                "failed": [],
            },
        },
    }
    summary = json.loads(_summarize_import_response(json.dumps(payload)))
    assert len(summary["meta"]["unmatched_headers"]) == 20
    assert summary["meta"]["unmatched_truncated"] == 30
    md = summary["meta"]["master_data"]
    assert md["candidates"] == {"client": 5}
    assert len(md["archived"]) == 10
    assert md["archived_truncated"] == 5
    assert "failed" not in md  # 空明细不占位


def _import_audit_bill(client) -> dict:
    """上传最小竞品账单（preview），返回最新一条 /orders/bill/import 日志条目。"""
    resp = client.post(
        "/orders/bill/import",
        files={
            "file": (
                "audit-bill.xlsx",
                _build_yinghui_bill_bytes(),
                "application/octet-stream",
            )
        },
        data={"create_order": "false"},
    )
    assert resp.status_code == 200
    return _items(
        client.get("/api/logs", params={"path": "/orders/bill/import"}).json()
    )[0]


def test_bill_import_response_full_kept_for_export():
    """摘要化时完整响应体单独保留：列表默认剥离，include_full=1 可取回。"""
    import json

    client = TestClient(app)
    entry = _import_audit_bill(client)
    assert entry["response_summarized"] is True
    # 默认列表剥离大字段，页面轻量
    assert "response_full" not in entry
    # 导出场景：include_full=1 返回完整响应体（含明细）
    full_entry = _items(
        client.get(
            "/api/logs",
            params={"path": "/orders/bill/import", "include_full": "1"},
        ).json()
    )[0]
    assert full_entry["response_full"] is not None
    assert "canonical_orders" in full_entry["response_full"]
    assert full_entry["response_full_truncated"] is False
    # 摘要字段与完整字段同源一致
    assert json.loads(full_entry["response"])["order_count"] == 1


def test_response_full_truncated_when_over_limit(monkeypatch):
    """response_full 超过独立上限时截断并标记（导出仍可拿可用信息）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "access_log_response_full_max_chars", 64)
    client = TestClient(app)
    entry = _import_audit_bill(client)
    full_entry = _items(
        client.get(
            "/api/logs",
            params={"path": "/orders/bill/import", "include_full": "1"},
        ).json()
    )[0]
    assert full_entry["response_full_truncated"] is True
    assert len(full_entry["response_full"]) <= 64
    # 摘要不受 response_full 上限影响
    assert entry["response_summarized"] is True


def test_query_include_full_false_strips_other_paths_too():
    """非摘要路径无 response_full 字段，剥离逻辑对普通条目无副作用。"""
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    entry = _items(client.get("/api/logs").json())[0]
    assert "response_full" not in entry


# ---------- 存储层：按天分文件 / 轮转 / 过期清理 / 回填 ----------


def _log_dir() -> Path:
    import app.access_log as access_log

    log_dir = access_log._log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def test_record_writes_daily_files_by_ts():
    """按日志时间戳写入当日文件 requests-YYYY-MM-DD.jsonl。"""
    from datetime import UTC, datetime, timedelta

    import app.access_log as access_log

    today = datetime.now(UTC).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    access_log.record({"path": "/a", "ts": f"{yesterday}T10:00:00+00:00"})
    access_log.record({"path": "/b", "ts": f"{today.isoformat()}T10:00:00+00:00"})
    log_dir = _log_dir()
    assert (log_dir / f"requests-{yesterday}.jsonl").exists()
    assert (log_dir / f"requests-{today.isoformat()}.jsonl").exists()
    lines = (log_dir / f"requests-{yesterday}.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 1
    assert '"path": "/a"' in lines[0]


def test_record_without_ts_uses_today_file():
    """无 ts 时补当前时间并写入当日文件。"""
    from datetime import UTC, datetime

    import app.access_log as access_log

    access_log.record({"path": "/c"})
    today = datetime.now(UTC).date().isoformat()
    assert (_log_dir() / f"requests-{today}.jsonl").exists()


def test_query_covers_multiple_daily_files():
    """跨日写入的日志都能在内存缓冲查到（最新在前）。"""
    from datetime import UTC, datetime, timedelta

    import app.access_log as access_log

    today = datetime.now(UTC).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    access_log.record({"path": "/a", "ts": f"{yesterday}T10:00:00+00:00"})
    access_log.record({"path": "/b", "ts": f"{today.isoformat()}T10:00:00+00:00"})
    payload = access_log.query(limit=10)
    assert payload["total"] == 2
    assert [e["path"] for e in payload["items"]] == ["/b", "/a"]


def test_rotate_archives_daily_file_with_increment():
    """单日文件超限轮转归档为 .N，编号递增不覆盖旧归档。"""
    from datetime import UTC, datetime

    import app.access_log as access_log

    today = datetime.now(UTC).date().isoformat()
    log_dir = _log_dir()
    access_log.record({"path": "/x", "ts": f"{today}T10:00:00+00:00"})
    daily = log_dir / f"requests-{today}.jsonl"
    access_log._rotate_log_file(daily)
    assert (log_dir / f"requests-{today}.jsonl.1").exists()
    # 主文件重新生成后再次轮转 → .2，.1 保留不覆盖
    access_log.record({"path": "/y", "ts": f"{today}T11:00:00+00:00"})
    access_log._rotate_log_file(daily)
    assert (log_dir / f"requests-{today}.jsonl.1").exists()
    assert (log_dir / f"requests-{today}.jsonl.2").exists()


def test_cleanup_expired_removes_files_by_ts(monkeypatch):
    """过期按天文件按日志 ts（文件名日期）删除，归档同日删除，不依赖 mtime。"""
    from datetime import UTC, datetime, timedelta

    import app.access_log as access_log
    from app.config import settings

    monkeypatch.setattr(settings, "storage_keep_hours", 48)  # keep_days=2
    today = datetime.now(UTC).date()
    for delta in (3, 2, 0):
        day = (today - timedelta(days=delta)).isoformat()
        (_log_dir() / f"requests-{day}.jsonl").write_text(
            '{"path": "/x"}\n', encoding="utf-8"
        )
    expired_day = (today - timedelta(days=3)).isoformat()
    (_log_dir() / f"requests-{expired_day}.jsonl.1").write_text(
        '{"path": "/old"}\n', encoding="utf-8"
    )
    access_log._cleanup_expired(_log_dir())
    assert not (_log_dir() / f"requests-{expired_day}.jsonl").exists()
    assert not (_log_dir() / f"requests-{expired_day}.jsonl.1").exists()
    assert (_log_dir() / f"requests-{(today - timedelta(days=2)).isoformat()}.jsonl").exists()
    assert (_log_dir() / f"requests-{today.isoformat()}.jsonl").exists()


def test_cleanup_removes_legacy_file_by_mtime(monkeypatch):
    """旧版单文件格式（文件名无日期）按 mtime 兜底清理，新归档保留。"""
    import os
    import time

    import app.access_log as access_log
    from app.config import settings

    monkeypatch.setattr(settings, "storage_keep_hours", 24)
    log_dir = _log_dir()
    legacy = log_dir / "requests.jsonl"
    legacy.write_text('{"path": "/old"}\n', encoding="utf-8")
    old = time.time() - 25 * 3600  # 25 小时前 → 过期
    os.utime(legacy, (old, old))
    fresh = log_dir / "requests.jsonl.1"
    fresh.write_text('{"path": "/new"}\n', encoding="utf-8")  # mtime 新 → 保留
    access_log._cleanup_expired(log_dir)
    assert not legacy.exists()
    assert fresh.exists()


def test_cleanup_ignores_unrelated_files():
    """日期非法/无关文件不按 ts 删除（mtime 新则保留）。"""
    import app.access_log as access_log

    log_dir = _log_dir()
    unrelated = log_dir / "requests-abc.jsonl"  # 日期非法 → mtime 兜底
    unrelated.write_text('{"path": "/x"}\n', encoding="utf-8")
    other = log_dir / "other.txt"
    other.write_text("not a log\n", encoding="utf-8")
    access_log._cleanup_expired(log_dir)
    assert unrelated.exists()
    assert other.exists()


def test_backfill_loads_multiple_daily_files():
    """重启后 query 触发回填：多个按天文件尾部进内存，最新在前。"""
    from datetime import UTC, datetime, timedelta

    import app.access_log as access_log

    today = datetime.now(UTC).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    log_dir = _log_dir()
    (log_dir / f"requests-{yesterday}.jsonl").write_text(
        f'{{"path": "/old", "ts": "{yesterday}T10:00:00+00:00"}}\n',
        encoding="utf-8",
    )
    (log_dir / f"requests-{today.isoformat()}.jsonl").write_text(
        f'{{"path": "/new", "ts": "{today.isoformat()}T10:00:00+00:00"}}\n',
        encoding="utf-8",
    )
    payload = access_log.query(limit=10)
    assert payload["total"] == 2
    assert [e["path"] for e in payload["items"]] == ["/new", "/old"]


def test_backfill_orders_archive_before_main_file():
    """回填时同日归档（.N）早于主文件，时间顺序正确。"""
    from datetime import UTC, datetime

    import app.access_log as access_log

    today = datetime.now(UTC).date().isoformat()
    log_dir = _log_dir()
    (log_dir / f"requests-{today}.jsonl.1").write_text(
        f'{{"path": "/archived", "ts": "{today}T10:00:00+00:00"}}\n',
        encoding="utf-8",
    )
    (log_dir / f"requests-{today}.jsonl").write_text(
        f'{{"path": "/current", "ts": "{today}T11:00:00+00:00"}}\n',
        encoding="utf-8",
    )
    payload = access_log.query(limit=10)
    assert [e["path"] for e in payload["items"]] == ["/current", "/archived"]


def test_maybe_maintain_rotates_and_cleans(monkeypatch):
    """写入达到检查点：单日文件超限自动轮转，过期文件按 ts 清理。"""
    from datetime import UTC, datetime, timedelta

    import app.access_log as access_log
    from app.config import settings

    monkeypatch.setattr(access_log, "_ROTATE_CHECK_INTERVAL", 1)
    monkeypatch.setattr(access_log, "_MAX_LOG_FILE_BYTES", 1)  # 非空文件即超限
    monkeypatch.setattr(settings, "storage_keep_hours", 1)  # keep_days=1
    log_dir = _log_dir()
    today = datetime.now(UTC).date()
    access_log.record({"path": "/big"})  # 写入即触发维护
    assert (log_dir / f"requests-{today.isoformat()}.jsonl.1").exists()
    # 清理同时执行：保留窗口外的文件被删
    expired = (today - timedelta(days=2)).isoformat()
    (log_dir / f"requests-{expired}.jsonl").write_text(
        '{"path": "/old"}\n', encoding="utf-8"
    )
    access_log.record({"path": "/big2"})  # 再次触发维护
    assert not (log_dir / f"requests-{expired}.jsonl").exists()
