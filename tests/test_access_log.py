"""请求访问日志（/logs 页面与 /api/logs 接口）测试。"""

from __future__ import annotations

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

    def fake_run(*, file_bytes, filename, options):
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
    from app.config import settings

    monkeypatch.setattr(settings, "order_api_ext_app_id", "89")
    monkeypatch.setattr(settings, "order_api_ext_user_id", "jijuTms")
    monkeypatch.setattr(settings, "order_api_jxt_open_id", "open-id")
    monkeypatch.setattr(settings, "order_api_user_id", "dony")

    async def fake_publish(order_data, *, room_id):
        return {"code": "200", "msg": "ok", "data": []}

    monkeypatch.setattr(main_module, "_publish_order", fake_publish)
    client = TestClient(app)
    resp = client.post(
        "/orders",
        json={"content": "提单号：KMTCSHAP950393；托运人：海丰", "roomId": "audit-room-1"},
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

    monkeypatch.setattr(settings, "order_api_ext_app_id", "89")
    monkeypatch.setattr(settings, "order_api_ext_user_id", "jijuTms")
    monkeypatch.setattr(settings, "order_api_jxt_open_id", "open-id")
    monkeypatch.setattr(settings, "order_api_user_id", "dony")
    monkeypatch.setattr(settings, "access_log_body_max_chars", 64)

    async def fake_publish(order_data, *, room_id):
        return {"code": "200", "msg": "ok", "data": []}

    monkeypatch.setattr(main_module, "_publish_order", fake_publish)
    client = TestClient(app)
    resp = client.post(
        "/orders",
        # 内容超长（含必要字段），验证日志侧截断不影响业务解析
        json={"content": "提单号：KMTCSHAP950393；托运人：海丰；" + "A" * 500, "roomId": "audit-room-2"},
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

    def fake_run(*, file_bytes, filename, options):
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
