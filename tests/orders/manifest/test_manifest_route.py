"""路由层测试：preview/create 语义、sk 校验、409 duplicate_manifest、错误码。"""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.orders.manifest.service as service_module
from app.main import app

CREATE_HEADERS = {"sk": "tk-test"}


def _post(
    client: TestClient,
    content: bytes,
    filename: str = "a.xlsx",
    data: dict | None = None,
    headers: dict | None = None,
):
    return client.post(
        "/orders/manifest/import",
        files={"file": (filename, content, "application/octet-stream")},
        data=data or {},
        headers=headers or {},
    )


class TestRoutePreview:
    """preview：200 + 解析结果；零下游调用。"""

    def test_preview_returns_parsed(self, auth_bytes):
        client = TestClient(app)
        r = _post(client, auth_bytes)
        assert r.status_code == 200
        data = r.json()
        assert data["create_order"] is False
        assert data["summary"] is None
        assert data["upstream"] is None
        order = data["orders"][0]
        assert order["bl_no"] == "SITGBASH006434"
        assert order["family"] == "authorization"
        assert order["create_result"] is None
        assert order["order_data"]["type"] == 2
        assert order["order_data"]["orderInfos"][0]["unitPrice"] == "0"

    def test_preview_no_sk_required(self, auth_bytes):
        """preview 模式不要求 sk 头（零下游调用）。"""
        client = TestClient(app)
        assert _post(client, auth_bytes).status_code == 200

    def test_unknown_family_400(self, unknown_bytes):
        client = TestClient(app)
        r = _post(client, unknown_bytes)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "unknown_manifest_family"


class TestRouteCreate:
    """create：缺 sk 400；成功 200 + summary；全 skipped 409。"""

    def test_create_missing_sk_400(self, auth_bytes):
        client = TestClient(app)
        r = _post(client, auth_bytes, data={"create_order": "true"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "bad_request"
        assert "sk" in r.json()["error"]["message"]

    def test_create_success(self, auth_bytes, monkeypatch):
        def fake(_payload, _sk):
            return {"success": True, "sn": "11801", "error": None, "upstream": {"bId": 11801}}

        monkeypatch.setattr(service_module, "submit_manifest", fake)
        client = TestClient(app)
        r = _post(
            client,
            auth_bytes,
            data={"create_order": "true"},
            headers=CREATE_HEADERS,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["created"] == 1
        assert data["summary"]["success_sns"] == ["11801"]
        assert data["upstream"] == {"code": "200", "msg": "成功", "data": [{"bId": 11801}]}

    def test_create_all_skipped_409(self, auth_bytes, monkeypatch):
        """重导全部命中 → 409 duplicate_manifest（upstream 409 结构）。"""
        def fake(_payload, _sk):
            return {"success": True, "sn": "11801", "error": None, "upstream": {"bId": 11801}}

        monkeypatch.setattr(service_module, "submit_manifest", fake)
        client = TestClient(app)
        assert _post(
            client, auth_bytes, data={"create_order": "true"}, headers=CREATE_HEADERS
        ).status_code == 200
        r = _post(client, auth_bytes, data={"create_order": "true"}, headers=CREATE_HEADERS)
        assert r.status_code == 409
        body = r.json()
        assert body["error"]["code"] == "duplicate_manifest"
        assert body["error"]["details"]["success_sns"] == ["11801"]
        assert body["error"]["details"]["upstream"]["code"] == "409"

    def test_create_all_failed_204_upstream(self, auth_bytes, monkeypatch):
        """下游全拒 → 200 + upstream 204（业务失败，非 HTTP 错误）。"""
        def fake(_payload, _sk):
            return {"success": False, "sn": None, "error": {"code": "x", "message": "rejected"}}

        monkeypatch.setattr(service_module, "submit_manifest", fake)
        client = TestClient(app)
        r = _post(client, auth_bytes, data={"create_order": "true"}, headers=CREATE_HEADERS)
        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["failed"] == 1
        assert data["upstream"] == {"code": "204", "msg": "添加失败", "data": []}
