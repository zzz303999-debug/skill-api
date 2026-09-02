"""client 层测试：addBill 响应判定（数字 200）、sk 透传、失败/网络隔离。"""

from __future__ import annotations

import httpx
from manifest_helpers import FakeResponse

import app.orders.manifest.client as client_module

# 端点由 settings 注入（.env 可覆盖）：测试固定哨兵值，断言透传而非具体域名，
# 避免本地/生产 .env 差异打红测试（与 bill 侧 test_client.py urls fixture 同模式）
_TEST_ADDBILL_URL = "https://svc.example.com/crm/order/bill/addBill"


def _fake_post(monkeypatch, payload, status_code: int = 200):
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse(payload, status_code=status_code)

    monkeypatch.setattr(client_module.httpx, "post", fake_post)
    return captured


class TestSubmitManifest:
    """提交：端点/JSON/sk 透传；响应 code 数字 200 判定（与 AddWork 字符串区分）。"""

    def test_success_parses_bid(self, monkeypatch):
        monkeypatch.setattr(client_module.settings, "jxt_manifest_addbill_url", _TEST_ADDBILL_URL)
        captured = _fake_post(
            monkeypatch,
            {"code": 200, "msg": "成功", "data": [{"bId": 11801, "bOrderNum": "111"}]},
        )
        payload = {"bOrderNum": "111"}
        result = client_module.submit_manifest(payload, "tk-1")
        assert captured["url"] == _TEST_ADDBILL_URL
        assert captured["json"] is payload
        assert captured["headers"]["sk"] == "tk-1"
        assert result == {
            "success": True,
            "sn": "11801",
            "error": None,
            "upstream": {"bId": 11801, "bOrderNum": "111"},
        }

    def test_string_code_200_also_accepted(self, monkeypatch):
        """防御性兼容字符串 "200"（勿与 AddWork 判定混用，兼容无伤）。"""
        _fake_post(monkeypatch, {"code": "200", "msg": "成功", "data": [{"bId": 7}]})
        result = client_module.submit_manifest({}, "tk")
        assert result["success"] is True
        assert result["sn"] == "7"

    def test_200_with_null_data_still_created(self, monkeypatch):
        """live 实证（2026-08-20）：服务端通道 200 + data null 仍真实创建 → 成功，sn 空串。"""
        _fake_post(
            monkeypatch,
            {"code": 200, "msg": "成功", "data": None, "errorCode": 0, "errorMsg": None},
        )
        result = client_module.submit_manifest({}, "tk")
        assert result == {"success": True, "sn": "", "error": None, "upstream": None}

    def test_rejection_passthrough(self, monkeypatch):
        """code 204 → 失败；msg/data 透传 details.upstream（204 口径）。"""
        _fake_post(
            monkeypatch, {"code": 204, "msg": "添加失败", "data": [], "errorMsg": "缺少参数"}
        )
        result = client_module.submit_manifest({}, "tk")
        assert result["success"] is False
        assert result["sn"] is None
        assert result["error"]["code"] == "order_upstream_error"
        assert result["error"]["details"]["upstream"] == {
            "code": "204",
            "msg": "添加失败",
            "data": [],
        }
        assert result["error"]["details"]["upstream_error_msg"] == "缺少参数"
        assert "添加失败" in result["error"]["message"]

    def test_network_error_isolated(self, monkeypatch):
        """超时/网络错误按单记 error（不抛，调用方隔离失败单）。"""

        def fake_post(*_args, **_kwargs):
            raise httpx.TimeoutException("timeout")

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = client_module.submit_manifest({}, "tk")
        assert result["success"] is False
        assert result["error"]["details"]["error_type"] == "TimeoutException"
