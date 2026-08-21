"""service 编排测试：preview 零副作用、create 成功/拦截/去重/箱型拒绝、summary/upstream。"""

from __future__ import annotations

import io

from openpyxl import load_workbook

import app.orders.manifest.service as service_module
from app.orders.manifest import build_manifest_result


# service 内 `from .client import submit_manifest` 为命名空间绑定：
# 必须 patch service 命名空间（patch client 模块属性不生效）
def _patch_submit(monkeypatch, payload):
    """mock service.submit_manifest → 固定响应（失败隔离：不抛异常）。"""
    calls = {"n": 0}

    def fake(_payload, _sk):
        calls["n"] += 1
        return payload

    monkeypatch.setattr(service_module, "submit_manifest", fake)
    return calls


class TestPreview:
    """preview：零下游调用、零注册表读写。"""

    def test_zero_side_effects(self, auth_bytes, monkeypatch):
        """无 submit 调用；注册表无写入；summary/upstream 为 null。"""
        calls = _patch_submit(monkeypatch, {"success": True, "sn": "1"})
        result = build_manifest_result("a.xlsx", auth_bytes, create_order=False)
        assert calls["n"] == 0
        assert result.summary is None
        assert result.upstream is None
        assert result.orders[0].create_result is None
        from app.orders.manifest import registry

        assert registry.get_manifest_registry().snapshot() == {}

    def test_unit_price_defaults_to_zero(self, auth_bytes):
        """每箱运价恒 0（模版无运价字段）；不登记 unit_price 缺失。"""
        result = build_manifest_result("a.xlsx", auth_bytes, create_order=False)
        order = result.orders[0]
        assert "unit_price" not in order.missing_fields
        info = order.order_data["orderInfos"][0]
        assert info["unitPrice"] == "0"
        assert info["totalPrice"] == 0

    def test_meta_fields(self, auth_bytes):
        result = build_manifest_result("a.xlsx", auth_bytes)
        assert result.meta["family"] == "authorization"
        assert result.meta["parser"] == "openpyxl"
        assert result.meta["source_bytes"] == len(auth_bytes)
        assert len(result.meta["source_sha256"]) == 64


class TestCreate:
    """create：成功提交+登记、必填拦截、去重 skipped、箱型拒绝、上游回显。"""

    def test_success_submits_and_registers(self, auth_bytes, monkeypatch):
        """成功：提交体=order_data（实际提交回显）；注册表登记 bId；summary/upstream。"""
        _patch_submit(
            monkeypatch,
            {"success": True, "sn": "11801", "error": None, "upstream": {"bId": 11801}},
        )
        result = build_manifest_result("a.xlsx", auth_bytes, create_order=True, sk="tk")
        order = result.orders[0]
        assert order.create_result["success"] is True
        assert order.create_result["sn"] == "11801"
        assert result.summary == {
            "total": 1,
            "success": 1,
            "failed": 0,
            "skipped": 0,
            "created": 1,
            "success_sns": ["11801"],
            "failed_details": [],
        }
        assert result.upstream == {"code": "200", "msg": "成功", "data": [{"bId": 11801}]}
        # 实际提交体回显：None→空串已转换
        assert order.order_data["orderInfos"][0]["bDate"] == ""
        # 注册表登记（可被下次去重命中）
        from app.orders.manifest import registry

        assert registry.get_manifest_registry().lookup("SITGBASH006434")["sn"] == "11801"

    def test_required_missing_intercepted(self, auth_bytes, monkeypatch):
        """必填缺失 → create 拦截（manifest_order_not_ready），不提交、不登记。"""
        calls = _patch_submit(monkeypatch, {"success": True, "sn": "1"})
        wb = load_workbook(io.BytesIO(auth_bytes))
        wb.active.cell(14, 4).value = None  # 清 POL
        buf = io.BytesIO()
        wb.save(buf)
        result = build_manifest_result("a.xlsx", buf.getvalue(), create_order=True, sk="tk")
        order = result.orders[0]
        assert calls["n"] == 0
        assert order.create_result["success"] is False
        assert order.create_result["error"]["code"] == "manifest_order_not_ready"
        assert order.create_result["error"]["details"]["missing_fields"] == ["pol"]
        assert result.summary["failed"] == 1
        assert result.upstream == {"code": "204", "msg": "添加失败", "data": []}

    def test_duplicate_skipped(self, auth_bytes, monkeypatch):
        """重导命中注册表 → skipped；不重复提交；summary created=0。"""
        _patch_submit(
            monkeypatch,
            {"success": True, "sn": "11801", "error": None, "upstream": {"bId": 11801}},
        )
        build_manifest_result("a.xlsx", auth_bytes, create_order=True, sk="tk")
        calls = _patch_submit(monkeypatch, {"success": True, "sn": "99999"})
        result = build_manifest_result("a.xlsx", auth_bytes, create_order=True, sk="tk")
        order = result.orders[0]
        assert calls["n"] == 0  # 未再提交
        assert order.create_result["skipped"] is True
        assert order.create_result["sn"] == "11801"
        assert result.summary["skipped"] == 1
        assert result.summary["created"] == 0
        assert result.upstream is None  # 全部 skipped → None（路由层转 409）

    def test_force_reimport_after_tms_delete(self, auth_bytes, monkeypatch):
        """force=True 跳过去重：TMS 侧删单后重录场景（重复风险调用方自负）。"""
        _patch_submit(
            monkeypatch,
            {"success": True, "sn": "11801", "error": None, "upstream": {"bId": 11801}},
        )
        build_manifest_result("a.xlsx", auth_bytes, create_order=True, sk="tk")
        # 常规重导 → skipped 不提交
        calls = _patch_submit(monkeypatch, {"success": True, "sn": "99999"})
        r1 = build_manifest_result("a.xlsx", auth_bytes, create_order=True, sk="tk")
        assert calls["n"] == 0 and r1.orders[0].create_result["skipped"] is True
        # force 重导 → 重新提交
        calls = _patch_submit(
            monkeypatch,
            {"success": True, "sn": "11802", "error": None, "upstream": {"bId": 11802}},
        )
        r2 = build_manifest_result("a.xlsx", auth_bytes, create_order=True, sk="tk", force=True)
        assert calls["n"] == 1
        cr = r2.orders[0].create_result
        assert cr["success"] is True and not cr.get("skipped")
        assert r2.summary["created"] == 1

    def test_upstream_rejection_failed_not_registered(self, auth_bytes, monkeypatch):
        """下游 204 拒绝 → 失败单不登记，可重导重试。"""
        _patch_submit(monkeypatch, {"success": False, "sn": None, "error": {"code": "x", "message": "m"}})
        result = build_manifest_result("a.xlsx", auth_bytes, create_order=True, sk="tk")
        assert result.orders[0].create_result["success"] is False
        assert result.upstream == {"code": "204", "msg": "添加失败", "data": []}
        from app.orders.manifest import registry

        assert registry.get_manifest_registry().snapshot() == {}


class TestBoxWhitelist:
    """箱型白名单文件级拒绝（复用账单配置；preview 亦拒绝）。"""

    def _illegal_box_bytes(self, auth_bytes) -> bytes:
        """把托书箱型改为白名单外标准码 40GOH（标准码形态，不在白名单）。"""
        wb = load_workbook(io.BytesIO(auth_bytes))
        wb.active.cell(15, 8).value = "1*40GOH (FFAU7731669/SITR853037)"
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_unknown_box_type_rejected_preview(self, auth_bytes):
        result = build_manifest_result("a.xlsx", self._illegal_box_bytes(auth_bytes))
        order = result.orders[0]
        assert order.create_result["success"] is False
        assert order.create_result["error"]["code"] == "unknown_box_type"
        assert "40GOH" in order.create_result["error"]["message"]
        assert order.create_result["error"]["details"]["upstream"]["code"] == "204"
        assert result.summary is None  # preview：summary 仍为 null（对齐账单）

    def test_unknown_box_type_not_submitted_create(self, auth_bytes, monkeypatch):
        calls = _patch_submit(monkeypatch, {"success": True, "sn": "1"})
        result = build_manifest_result(
            "a.xlsx", self._illegal_box_bytes(auth_bytes), create_order=True, sk="tk"
        )
        assert calls["n"] == 0
        assert result.summary["failed"] == 1
        assert result.upstream == {"code": "204", "msg": "添加失败", "data": []}


class TestBoxMissing:
    """箱型整体缺失文件级拒绝（v1.7：preview 亦拒绝；不调下游；与白名单互斥）。"""

    def _no_box_bytes(self, auth_bytes) -> bytes:
        """清空托书 Container volume 值格 → 解析无箱型（box_groups 空）。"""
        wb = load_workbook(io.BytesIO(auth_bytes))
        wb.active.cell(15, 8).value = None
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_box_missing_rejected_preview(self, auth_bytes):
        """preview 亦拒绝：create_result 标记 manifest_box_missing，summary 仍 null。"""
        result = build_manifest_result("a.xlsx", self._no_box_bytes(auth_bytes))
        order = result.orders[0]
        assert order.create_result["success"] is False
        assert order.create_result["error"]["code"] == "manifest_box_missing"
        assert "box_groups" in order.create_result["error"]["message"]
        details = order.create_result["error"]["details"]
        assert details["missing_fields"] == ["box_groups"]
        assert details["missing_reasons"]["box_groups"] == "原文未找到"
        assert details["upstream"]["code"] == "204"
        assert result.summary is None  # preview：summary 仍为 null（对齐账单）

    def test_box_missing_not_submitted_create(self, auth_bytes, monkeypatch):
        """create：不调下游、不登记注册表；summary failed=1；upstream 204。"""
        calls = _patch_submit(monkeypatch, {"success": True, "sn": "1"})
        result = build_manifest_result(
            "a.xlsx", self._no_box_bytes(auth_bytes), create_order=True, sk="tk"
        )
        assert calls["n"] == 0
        order = result.orders[0]
        assert order.create_result["error"]["code"] == "manifest_box_missing"
        assert result.summary["failed"] == 1
        assert result.upstream == {"code": "204", "msg": "添加失败", "data": []}
        from app.orders.manifest import registry

        assert registry.get_manifest_registry().snapshot() == {}

    def test_si_variant1_no_box_source_rejected(self, si_bytes):
        """SI 变体 1（无底部汇总/明细无箱型列，天然无箱型）→ 同样文件级拒绝。"""
        result = build_manifest_result("a.xlsx", si_bytes)
        order = result.orders[0]
        assert order.box_groups == []
        assert order.create_result["error"]["code"] == "manifest_box_missing"

    def test_whitelist_unknown_still_takes_precedence(self, auth_bytes):
        """有箱型但白名单外 → 仍走 unknown_box_type（box_missing 不触发，互斥）。"""
        wb = load_workbook(io.BytesIO(auth_bytes))
        wb.active.cell(15, 8).value = "1*40GOH (FFAU7731669/SITR853037)"
        buf = io.BytesIO()
        wb.save(buf)
        result = build_manifest_result("a.xlsx", buf.getvalue())
        order = result.orders[0]
        assert order.create_result["error"]["code"] == "unknown_box_type"
