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
    # 空内容文件触发 BadRequestError（code=empty_file），文件名在失败前未设置
    resp = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("empty.docx", b"", "application/octet-stream")},
    )
    assert resp.status_code == 400
    payload = client.get("/api/logs", params={"status": 400}).json()
    entry = _items(payload)[0]
    assert entry["error_code"] == "empty_file"
    assert entry["file"] is None


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
