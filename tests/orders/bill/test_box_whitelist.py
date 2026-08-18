"""箱型白名单（上传前拦截）测试：白名单加载/标准代码判定/未知箱型过滤/service 集成。

规则（2026-08-18 用户拍板，叠加在既有「非空即合法、非标表述放行」之上）：
- 标准代码形态（两位数字 + 2~3 位大写字母，如 40HQ/40GOH）必须命中白名单；
- 非标准形态（大冷/拼箱/17M飞翼车/12T 等）不校验照常提交；
- 未命中 → 该单拒绝（unknown_box_type「系统没有此箱型：<箱型>，请联系客服」），
  不调下游；force_unknown_box_types=true 跳过校验强制提交。
"""

from __future__ import annotations

import pytest

import app.orders.bill.box_whitelist as whitelist_module
import app.orders.bill.client as client_module
from app.orders.bill import BillOrder, BoxGroup, CanonicalOrder, build_result
from app.orders.bill.service import _reject_unknown_box_types
from helpers import FakeResponse, build_bill_bytes

# junyu 家族表头（canonical 管线，box_type_qty → box_groups）
_JUNYU_HEADERS = {
    "A": "序号",
    "B": "客户名称",
    "C": "门点",
    "D": "箱型箱量",
    "E": "提单号",
    "F": "箱号",
    "G": "做箱时间",
    "H": "港区",
    "I": "司机",
    "J": "应收备注",
}


@pytest.fixture(autouse=True)
def _restore_box_whitelist_cache():
    """用例后恢复真实白名单缓存（monkeypatch 临时配置不残留污染后续用例）。"""
    yield
    whitelist_module.reload_box_types()


class TestWhitelistLoad:
    def test_real_config_loaded(self):
        """真实配置文件加载：TMS 全量箱型标准代码均在列（含首版补充项 40HQ/45HQ）。"""
        box_types = whitelist_module.load_box_types()
        assert {
            "20GP",
            "40GP",
            "45GP",
            "20HC",
            "40HC",
            "45HC",
            "20RF",
            "40RF",
            "20RH",
            "40RH",
            "20OH",
            "20OT",
            "40OT",
            "45OT",
            "20FR",
            "40FR",
            "45FR",
            "20HT",
            "40HT",
            "45HT",
            "20TK",
            "40TK",
            "40HH",
            "40HG",
            "40HW",
            "40OH",
            "40HQ",
            "40HN",
            "40TG",
            "45HQ",
            "20SQ",
            "40SQ",
            "40FQ",
            "40PF",
            "40FH",
            "40SD",
            "40SR",
            "40HR",
            "20PF",
            "30TK",
            "10GP",
            "20NOR",
            "40NOR",
            "53HC",
            "12GP",
            "53HQ",
            "48FR",
            "40FL",
        } <= box_types

    def test_missing_config_disables_check(self, tmp_path, monkeypatch):
        """配置缺失 → 空集（校验禁用，全放行），不抛异常。"""
        monkeypatch.setattr(
            whitelist_module,
            "box_types_path",
            lambda: tmp_path / "no-such-file.yaml",
        )
        assert whitelist_module.reload_box_types() == frozenset()
        assert whitelist_module.check_unknown_box_types(["40GOH"]) == []

    def test_invalid_config_disables_check(self, tmp_path, monkeypatch):
        """YAML 损坏 → 空集（校验禁用），不抛异常。"""
        bad = tmp_path / "bad.yaml"
        bad.write_text("box_types: [", encoding="utf-8")
        monkeypatch.setattr(whitelist_module, "box_types_path", lambda: bad)
        assert whitelist_module.reload_box_types() == frozenset()
        assert whitelist_module.check_unknown_box_types(["40GOH"]) == []

    def test_reload_picks_up_changes(self, tmp_path, monkeypatch):
        """reload 后新配置生效（配置更新零重启）。"""
        cfg = tmp_path / "wl.yaml"
        cfg.write_text("box_types:\n  - 40HQ\n", encoding="utf-8")
        monkeypatch.setattr(whitelist_module, "box_types_path", lambda: cfg)
        reloaded = whitelist_module.reload_box_types()
        assert reloaded == frozenset({"40HQ"})
        assert whitelist_module.check_unknown_box_types(["40HQ", "40GOH"]) == ["40GOH"]


class TestStandardCode:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("40HQ", True),
            ("40GOH", True),
            ("45HC", True),
            ("53HQ", True),
            ("48FR", True),
            ("20GP", True),
            # 非标准形态：不校验放行
            ("大冷", False),
            ("拼箱", False),
            ("17M飞翼车", False),
            ("12T", False),
            ("B/L", False),
            ("45P1", False),
            ("2X20", False),
            ("16米", False),
            ("", False),
        ],
    )
    def test_standard_code_judgement(self, text, expected):
        assert whitelist_module.is_standard_code(text) is expected


class TestCheckUnknown:
    def test_unknown_standard_code_filtered(self):
        """40GOH（标准形态、不在白名单）→ 拦截。"""
        assert whitelist_module.check_unknown_box_types(["40GOH"]) == ["40GOH"]

    def test_whitelisted_codes_pass(self):
        """白名单内代码（含补充项）→ 放行。"""
        assert whitelist_module.check_unknown_box_types(
            ["40HQ", "20GP", "40OT", "45HQ", "10GP", "53HC"]
        ) == []

    def test_non_standard_expressions_pass(self):
        """非标表述（既有规则放行）→ 不校验。"""
        assert whitelist_module.check_unknown_box_types(
            ["大冷", "拼箱", "17M飞翼车", "12T", "B/L", "45P1", "2X20"]
        ) == []

    def test_mixed_keeps_known_removes_unknown(self):
        assert whitelist_module.check_unknown_box_types(
            ["40HQ", "40GOH", "大冷"]
        ) == ["40GOH"]

    def test_dedup_preserves_order(self):
        assert whitelist_module.check_unknown_box_types(
            ["40GOH", "20GOH", "40GOH"]
        ) == ["40GOH", "20GOH"]


def _canonical_order(bl_no: str, b_type: str) -> CanonicalOrder:
    return CanonicalOrder(bl_no=bl_no, box_groups=[BoxGroup(b_type=b_type, box_num=1)])


class TestRejectUnknownBoxTypes:
    """service 层校验函数：canonical（box_groups）与既有（order_data.box）双管线。"""

    def test_canonical_unknown_rejected(self):
        order = _canonical_order("OOLU10000001", "40GOH")
        _reject_unknown_box_types([order], force=False)
        result = order.create_result
        assert result["success"] is False
        assert result["error"]["code"] == "unknown_box_type"
        assert result["error"]["message"] == "系统没有此箱型：40GOH，请联系客服"
        assert result["error"]["details"] == {"unknown_box_types": ["40GOH"]}

    def test_canonical_known_pass(self):
        order = _canonical_order("OOLU10000002", "40HQ")
        _reject_unknown_box_types([order], force=False)
        assert order.create_result is None  # 未被标记 → 后续照常提交

    def test_legacy_order_data_box_rejected(self):
        """既有语义管线（BillOrder.order_data.box）同样校验。"""
        order = BillOrder(order_num1="OOLU10000003", order_data={"box": [{"b_type": "40GOH"}]})
        _reject_unknown_box_types([order], force=False)
        assert order.create_result["error"]["code"] == "unknown_box_type"

    def test_mixed_only_unknown_rejected(self):
        orders = [
            _canonical_order("OOLU10000004", "40HQ"),
            _canonical_order("OOLU10000005", "40GOH"),
            _canonical_order("OOLU10000006", "大冷"),
        ]
        _reject_unknown_box_types(orders, force=False)
        assert orders[0].create_result is None
        assert orders[1].create_result["error"]["code"] == "unknown_box_type"
        assert orders[2].create_result is None  # 非标表述不校验

    def test_force_skips_check(self):
        order = _canonical_order("OOLU10000007", "40GOH")
        _reject_unknown_box_types([order], force=True)
        assert order.create_result is None  # force 跳过 → 照常提交

    def test_already_marked_skipped_not_overwritten(self):
        order = _canonical_order("OOLU10000008", "40GOH")
        order.create_result = {"success": True, "skipped": True, "sn": "EX1", "error": None}
        _reject_unknown_box_types([order], force=False)
        assert order.create_result["skipped"] is True  # 历史成功单不重复校验


class TestBuildResultIntegration:
    """端到端（junyu 家族 canonical 管线 + mock 下游）：拦截与 force 行为。"""

    @staticmethod
    def _build_bytes(rows: list[dict]) -> bytes:
        return build_bill_bytes(_JUNYU_HEADERS, rows)

    @staticmethod
    def _fake_downstream(monkeypatch):
        """GetWebKey → login → AddWork 全 mock；统计 AddWork 调用次数。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            if "GetWebKey" in url:
                return FakeResponse({"code": 200, "msg": "ok", "web_key": "wk"})
            if "login" in url:
                return FakeResponse({"code": 200, "data": {"token": "sk"}, "msg": "ok"})
            calls["addwork"] += 1
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        return calls

    def test_unknown_box_type_rejected_not_submitted(self, monkeypatch):
        """40GOH 单拒绝（unknown_box_type、不调下游）；40HQ 单正常提交。"""
        calls = self._fake_downstream(monkeypatch)
        result = build_result(
            filename="mix.xlsx",
            file_bytes=self._build_bytes(
                [
                    {"A": 1, "B": "客户甲", "E": "OOLU12345678", "D": "40HQ"},
                    {"A": 2, "B": "客户甲", "E": "OOLU12345679", "D": "40GOH*16"},
                ]
            ),
            create_order=True,
        )
        assert result.summary["total"] == 2
        assert result.summary["success"] == 1
        assert result.summary["failed"] == 1
        assert calls["addwork"] == 1  # 仅 40HQ 单提交
        by_bl = {o.bl_no: o for o in result.canonical_orders}
        assert by_bl["OOLU12345678"].create_result["success"] is True
        rejected = by_bl["OOLU12345679"].create_result
        assert rejected["success"] is False
        assert rejected["error"]["code"] == "unknown_box_type"
        assert rejected["error"]["message"] == "系统没有此箱型：40GOH，请联系客服"

    def test_force_unknown_submits_all(self, monkeypatch):
        """force_unknown_box_types=true → 40GOH 也照常提交。"""
        calls = self._fake_downstream(monkeypatch)
        result = build_result(
            filename="force.xlsx",
            file_bytes=self._build_bytes(
                [
                    {"A": 1, "B": "客户甲", "E": "OOLU12345680", "D": "40GOH*1"},
                    {"A": 2, "B": "客户甲", "E": "OOLU12345681", "D": "40HQ"},
                ]
            ),
            create_order=True,
            force_unknown_box_types=True,
        )
        assert result.summary["success"] == 2
        assert result.summary["failed"] == 0
        assert calls["addwork"] == 2

    def test_non_standard_expression_passes(self, monkeypatch):
        """非标表述（大冷）→ 不校验照常提交（既有规则不变）。"""
        calls = self._fake_downstream(monkeypatch)
        result = build_result(
            filename="nonstd.xlsx",
            file_bytes=self._build_bytes(
                [{"A": 1, "B": "客户甲", "E": "OOLU12345682", "D": "大冷"}]
            ),
            create_order=True,
        )
        assert result.summary["success"] == 1
        assert result.summary["failed"] == 0
        assert calls["addwork"] == 1

    def test_preview_never_checks_or_submits(self, monkeypatch):
        """preview（create_order=false）不读白名单、不调下游（零副作用）。"""
        calls = self._fake_downstream(monkeypatch)
        result = build_result(
            filename="preview.xlsx",
            file_bytes=self._build_bytes(
                [{"A": 1, "B": "客户甲", "E": "OOLU12345683", "D": "40GOH*1"}]
            ),
        )
        assert result.summary is None
        assert calls["addwork"] == 0
        assert all(o.create_result is None for o in result.canonical_orders)
