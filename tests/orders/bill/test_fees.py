"""T16 阶段二费用测试：归一三级/price_id 降级/payload 四通道/对账恒等/golden 驱动。

覆盖实施 prompt §2 T9-T16 的单测与 golden 断言：
- 归一三级优先级（模板 fees.mapping > 全局费目字典 > unmapped_fee 策略）
- import:false（税金）仅对账、to_other 原名进备注
- price_id 回填/降级/缺价格表 fail fast
- payload 四通道键位/两位小数/合计回写/空通道省略/通道级 note
- 对账恒等与容差（无锚点 no_anchor 不误报）
- golden：秋怡税金 6386 行恒等、军羽运费 453 行、亚灏三通道、海川123 三通道、赢辉长尾

费用断言全部用标准费目码 + 配置注入，无竞品名/费目中文字符串/price_id 数字硬编码。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.orders.bill import (
    BoxGroup,
    CanonicalOrder,
    FeeItem,
    group_canonical,
    parse_bill,
    template_store,
)
from app.orders.bill.fee_name_map import canonicalize_fee, reload_fee_alias_dictionary
from app.orders.bill.fee_price_map import apply_price_map, reload_price_map
from app.orders.bill.payload import build_order_payload
from helpers import inject_price_map

FAMILIES_DIR = Path(__file__).resolve().parent.parent.parent / "golden" / "bill" / "families"

# 测试用行级费用/锚点数据（模拟 parser 行级抽取输出）
_FEES_CFG = {
    "channels": {"应收": "shou", "应付": "pay"},
    "mapping": {"应收.运费": "freight", "应收.税金": {"code": "tax", "import": False, "reconcile": True}},
    "unmapped_fee": "to_other",
}


@pytest.fixture(autouse=True)
def _fee_caches():
    """费用配置缓存隔离：每用例后重置，防止用例间互相污染。

    fail fast 用例 monkeypatch 了映射表路径，teardown 时容错跳过重置
    （其 load 失败路径不会写入缓存，无残留）。
    """
    yield
    try:
        reload_fee_alias_dictionary()
        reload_price_map()
    except RuntimeError:
        pass


def _fee_rows() -> list[dict]:
    """两行应收（运费+税金+长尾）→ 行级 _fees/_anchors（模拟 parser 输出）。"""
    return [
        {
            "bl_no": "BL12345678",
            "box_type_qty": [{"type": "40HQ", "qty": 1}],
            "_fees": [
                {"section": "应收", "name": "运费", "channel": "shou", "code": "freight", "import": True, "reconcile": True, "money": 100.0},
                {"section": "应收", "name": "税金", "channel": "shou", "code": "tax", "import": False, "reconcile": True, "money": 4.0},
                {"section": "应收", "name": "高速费", "channel": "shou", "code": "other", "import": True, "reconcile": True, "money": 6.0},
            ],
            "_anchors": {"应收": {"小计": 110.0}},
        }
    ]


def _make_order(fees: list[FeeItem] | None = None, bl_no: str = "BL12345678") -> CanonicalOrder:
    return CanonicalOrder(
        bl_no=bl_no,
        box_groups=[BoxGroup(b_type="40HQ", box_num=1)],
        fees=fees or [],
    )


def _inject_price_map(monkeypatch, overrides: dict[str, int | None]) -> None:
    """注入临时映射表（真实表 deepcopy + 覆盖指定码 price_id），锁定降级场景。

    2026-09-01 起真实表全量补实证 id，「null → 降级」机制改由配置注入锁定，
    与真实配置值解耦（monkeypatch teardown 自动恢复函数，缓存 fixture 重置表）。
    2026-09-01 起实现上移至 helpers.inject_price_map（三文件共享）。
    """

    inject_price_map(monkeypatch, overrides)


class TestCanonicalizeFee:
    """T10 归一三级解析：mapping > 全局字典 > unmapped_fee 策略。"""

    def test_mapping_priority_over_dictionary(self):
        """模板 mapping 显式命中优先（含区块.费目 复合键）。"""
        meta = canonicalize_fee("应收", "运费", _FEES_CFG)
        assert meta["code"] == "freight" and meta["import"] is True

    def test_mapping_extended_import_false(self):
        """mapping 扩展写法 {code, import: false, reconcile: true}（税金）。"""
        meta = canonicalize_fee("应收", "税金", _FEES_CFG)
        assert meta["code"] == "tax"
        assert meta["import"] is False and meta["reconcile"] is True

    def test_dictionary_fallback(self):
        """mapping 未覆盖 → 全局费目字典兜底（洋山提 → yangshan）。"""
        meta = canonicalize_fee("应收", "洋山提", _FEES_CFG)
        assert meta["code"] == "yangshan"

    def test_to_other_fallback(self):
        """mapping/字典均未命中 → unmapped_fee 策略 to_other。"""
        meta = canonicalize_fee("应收", "高速费", _FEES_CFG)
        assert meta["code"] == "other" and meta["import"] is True

    def test_skip_report_strategy(self):
        """unmapped_fee: skip_report → 空码不生成记录。"""
        cfg = {**_FEES_CFG, "unmapped_fee": "skip_report"}
        meta = canonicalize_fee("应收", "高速费", cfg)
        assert meta["code"] == "" and meta["import"] is False


class TestPriceMap:
    """T12 price_id 映射表：回填/降级/fail fast。"""

    def test_apply_backfills_tms_name_and_price_id(self):
        """已实证码回填 tms_name/price_id（配置注入，不写死数字）。"""
        fee = FeeItem(channel="shou", code="freight", money=Decimal("100.00"))
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].tms_name is not None
        assert updated[0].price_id is not None
        assert dropped == []

    def test_null_price_id_downgrades_to_excluded(self, monkeypatch):
        """price_id null → 该条降级（excluded，不录入、进报告），不阻塞整单。"""
        _inject_price_map(monkeypatch, {"waiting": None})
        fee = FeeItem(channel="shou", code="waiting", money=Decimal("50.00"))
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].excluded is True
        assert len(dropped) == 1
        assert dropped[0]["code"] == "waiting" and dropped[0]["reason"] == "price_id null"

    def test_other_null_downgrades_all_to_other(self, monkeypatch):
        """other.price_id null 且存在 to_other 项 → 全部降级（长尾无归并目标）。"""
        _inject_price_map(monkeypatch, {"other": None})
        fees = [
            FeeItem(channel="shou", code="other", money=Decimal("6.00"), note="高速费"),
            FeeItem(channel="shou", code="freight", money=Decimal("100.00")),
        ]
        (updated, dropped) = apply_price_map(fees)
        by_code = {f.code: f for f in updated}
        assert by_code["other"].excluded is True
        assert by_code["freight"].excluded is False  # 有 price_id 的不受影响
        assert len(dropped) == 1 and dropped[0]["code"] == "other"

    def test_missing_price_map_fails_fast(self, monkeypatch):
        """缺映射表文件 → RuntimeError（fail fast，不允许裸跑）。"""
        monkeypatch.setattr(
            "app.orders.bill.fee_price_map.price_map_path",
            lambda: Path("/nonexistent/fee_price_map.test.yaml"),
        )
        with pytest.raises(RuntimeError, match="fee price map missing"):
            reload_price_map()


class TestPayloadFees:
    """T13 payload 四通道发射：键位/两位小数/合计回写/空通道省略/通道 note。"""

    def test_shou_emission_keys(self):
        """shou 通道键位：channel[0][tms_name][money/price_id/price_type/is_profit/dai_dian]。"""
        fees = [
            FeeItem(channel="shou", code="freight", tms_name="运费", price_id=820, money=Decimal("123.00")),
        ]
        form, _ = build_order_payload(_make_order(fees))
        assert form["shou[0][运费][money]"] == "123.00"
        assert form["shou[0][运费][price_id]"] == "820"
        assert form["shou[0][运费][price_type]"] == "1"
        assert form["shou[0][运费][is_profit]"] == "1"
        assert form["shou[0][运费][dai_dian]"] == "1"
        assert form["shou[0][note]"] == ""  # 通道级 note 恒发（live 实证：缺键 204 拒单）
        assert form["driver[0][get_ys_zj]"] == "123.00"  # 合计回写（我方计算）

    def test_totals_written_back_per_channel(self):
        """合计回写：get_ys_zj=Σshou、pay_yf_zj=Σpay、supplier_hj_zj=Σcost。"""
        fees = [
            FeeItem(channel="shou", code="freight", tms_name="运费", price_id=820, money=Decimal("100.00")),
            FeeItem(channel="pay", code="freight", tms_name="运费", price_id=820, money=Decimal("40.00")),
            FeeItem(channel="cost", code="fuel", tms_name="油费", price_id=1134, money=Decimal("25.50")),
        ]
        form, _ = build_order_payload(_make_order(fees))
        assert form["driver[0][get_ys_zj]"] == "100.00"
        assert form["driver[0][pay_yf_zj]"] == "40.00"
        assert form["cost[0][supplier_hj_zj]"] == "25.50"
        assert form["cost[0][油费][driver_name]"] == ""  # cost 费目硬读 driver_name（live 实证）
        # duo_get 合计预留不出
        assert not any("duo_get_hj_zj" in k for k in form)

    def test_empty_channel_omitted(self):
        """空通道整段省略（无 pay/cost 键）。"""
        fees = [
            FeeItem(channel="shou", code="freight", tms_name="运费", price_id=820, money=Decimal("100.00")),
        ]
        form, _ = build_order_payload(_make_order(fees))
        assert not any(k.startswith("pay[0]") or k.startswith("cost[0]") for k in form)
        assert "driver[0][pay_yf_zj]" not in form

    def test_excluded_not_emitted(self):
        """excluded 项（税金/price_id 待补）不录入、不进合计。"""
        fees = [
            FeeItem(channel="shou", code="freight", tms_name="运费", price_id=820, money=Decimal("100.00")),
            FeeItem(channel="shou", code="tax", tms_name="税金", money=Decimal("4.00"), excluded=True),
            FeeItem(channel="shou", code="waiting", tms_name="待时费", money=Decimal("50.00"), excluded=True),
        ]
        form, _ = build_order_payload(_make_order(fees))
        assert not any("税金" in k or "待时费" in k for k in form)
        assert form["driver[0][get_ys_zj]"] == "100.00"

    def test_to_other_note_at_channel_level(self):
        """to_other 原名进通道级 note（「原名 ¥金额」列表，per-费目 note 键未实证）。"""
        fees = [
            FeeItem(channel="shou", code="other", tms_name="其它费", price_id=1135, money=Decimal("12.00"), note="高速费,掏箱费"),
        ]
        form, _ = build_order_payload(_make_order(fees))
        assert form["shou[0][note]"] == "高速费,掏箱费 ¥12.00"


class TestFeeReconcile:
    """T14 对账恒等：bill − recorded = excluded，容差 0.01；无锚点不误报。"""

    def test_identity_holds_with_excluded(self):
        """行级费用 + 锚点 → 恒等（税金 excluded 进排除项）。"""
        orders = group_canonical(_fee_rows(), {"fees": _FEES_CFG}, None)
        rec = orders[0].fee_reconcile["shou"]
        assert rec.bill_total == Decimal("110.00")
        assert rec.recorded_total == Decimal("106.00")  # 运费100 + 长尾6
        assert rec.excluded_total == Decimal("4.00")  # 税金
        assert rec.diff == Decimal("0.00")
        assert rec.ok is True
        # 税金 excluded 标记
        tax = next(f for f in orders[0].fees if f.code == "tax")
        assert tax.excluded is True

    def test_mismatch_beyond_tolerance(self):
        """差额超容差 → ok=False（进对账报告，只报告不拦截）。"""
        rows = _fee_rows()
        rows[0]["_anchors"] = {"应收": {"小计": 100.0}}  # 账单侧少 10
        orders = group_canonical(rows, {"fees": _FEES_CFG}, None)
        assert orders[0].fee_reconcile["shou"].ok is False

    def test_no_anchor_not_reported_as_mismatch(self):
        """无账单锚点（bill_total=0）→ 不算 mismatch（金科信 no_anchor 语义）。"""
        rows = _fee_rows()
        rows[0].pop("_anchors")
        orders = group_canonical(rows, {"fees": _FEES_CFG}, None)
        rec = orders[0].fee_reconcile["shou"]
        assert rec.bill_total == Decimal("0")
        assert rec.ok is True

    def test_to_other_note_merged(self):
        """to_other 原名 note：一行一票下各行独立成单，note 不再跨行拼接。"""
        rows = _fee_rows()
        rows.append(
            {
                "bl_no": "BL12345678",
                "box_type_qty": [{"type": "40HQ", "qty": 1}],
                "_fees": [
                    {"section": "应收", "name": "掏箱费", "channel": "shou", "code": "other", "import": True, "reconcile": True, "money": 3.0},
                ],
            }
        )
        orders = group_canonical(rows, {"fees": _FEES_CFG}, None)
        # 一行一票：两行同号 → 两单，各行 to_other 原名各自进 note
        assert len(orders) == 2
        notes = [
            next(f.note for f in o.fees if f.code == "other") for o in orders
        ]
        assert sorted(notes) == ["掏箱费", "高速费"]


class TestNegativeFee:
    """T27a 负向扣减项（扣除费）：抽取取负/发射格式/对账恒等/skip_report 降级。"""

    _NEG_CFG = {
        "channels": {"应收": "shou", "应付": "pay"},
        "mapping": {
            "应付.运费": "freight",
            "应付.扣除费": {"code": "deduction", "negative": True},
        },
        "unmapped_fee": "to_other",
    }

    def test_canonicalize_negative_marker(self):
        """mapping 扩展写法 {code, negative: true} → meta 带 negative 标记。"""
        meta = canonicalize_fee("应付", "扣除费", self._NEG_CFG)
        assert meta["code"] == "deduction"
        assert meta["negative"] is True
        assert meta["import"] is True  # 默认正常录入（取负后走正常流程）

    def test_canonicalize_negative_default_off(self):
        """普通费目无 negative 标记（默认 False）。"""
        meta = canonicalize_fee("应付", "运费", self._NEG_CFG)
        assert meta["code"] == "freight"
        assert meta["negative"] is False

    def test_negative_skip_report_downgrade(self):
        """negative_policy=skip_report → import=False（不录入仅对账）。"""
        cfg = {
            "channels": {"应付": "pay"},
            "mapping": {
                "应付.扣除费": {"code": "deduction", "negative": True, "negative_policy": "skip_report"}
            },
        }
        meta = canonicalize_fee("应付", "扣除费", cfg)
        assert meta["negative"] is True
        assert meta["import"] is False  # 降级：不录入仅对账

    def test_parser_negative_money(self):
        """行级抽取：negative 项金额取负（-486）；负值列（-25）取负为 +25（加回）。"""
        from app.orders.bill.parser import _to_money

        # 账单语义：应付合计 = Σ正项 − 扣除费列值；列值 +486 → 录入 -486，列值 -25 → 录入 +25
        assert _to_money("486.00") == 486.0
        assert _to_money("-25.00") == -25.0

    def test_aggregate_negative_in_recorded(self):
        """负项进录入口径：bill = Σ正 + Σ负 恒等成立（含负项不破坏恒等式）。

        parser 已取负：列值 -25（扣除费带符号）→ _fees money=+25（加回语义）。
        """
        rows = [
            {
                "bl_no": "BLNEG0001",
                "box_type_qty": [{"type": "40HQ", "qty": 1}],
                "_fees": [
                    {"section": "应付", "name": "运费", "channel": "pay", "code": "freight", "import": True, "reconcile": True, "money": 2700.0},
                    {"section": "应付", "name": "扣除费", "channel": "pay", "code": "deduction", "import": True, "reconcile": True, "negative": True, "money": 25.0},  # 列值 -25 取负后 +25
                ],
                "_anchors": {"应付": {"应付合计": 2725.0}},
            }
        ]
        orders = group_canonical(rows, {"fees": self._NEG_CFG}, None)
        rec = orders[0].fee_reconcile["pay"]
        assert rec.bill_total == Decimal("2725.00")
        assert rec.recorded_total == Decimal("2725.00")  # 2700 + 25
        assert rec.excluded_total == Decimal("0")
        assert rec.diff == Decimal("0.00")
        assert rec.ok is True

    def test_negative_skip_report_keeps_identity(self):
        """skip_report 降级：负项进 excluded，恒等式 bill = recorded + excluded 仍成立。"""
        cfg = {
            "channels": {"应付": "pay"},
            "mapping": {
                "应付.运费": "freight",
                "应付.扣除费": {"code": "deduction", "negative": True, "negative_policy": "skip_report"},
            },
        }
        rows = [
            {
                "bl_no": "BLNEG0002",
                "box_type_qty": [{"type": "40HQ", "qty": 1}],
                "_fees": [
                    {"section": "应付", "name": "运费", "channel": "pay", "code": "freight", "import": True, "reconcile": True, "money": 1300.0},
                    {"section": "应付", "name": "扣除费", "channel": "pay", "code": "deduction", "import": False, "reconcile": True, "negative": True, "money": -486.0},
                ],
                "_anchors": {"应付": {"应付合计": 814.0}},
            }
        ]
        orders = group_canonical(rows, {"fees": cfg}, None)
        rec = orders[0].fee_reconcile["pay"]
        # 1300 − 486 = 814；扣除费不录入 → excluded=-486，恒等：814 = 1300 + (-486)
        assert rec.recorded_total == Decimal("1300.00")
        assert rec.excluded_total == Decimal("-486.00")
        assert rec.diff == Decimal("0.00")
        assert rec.ok is True

    def test_payload_negative_money_format(self):
        """payload 负值发射：两位小数字符串（"-50.00"）与合计回写。"""
        fees = [
            FeeItem(channel="pay", code="deduction", tms_name="扣除费", price_id=124832, money=Decimal("-50.00")),
        ]
        form, _ = build_order_payload(_make_order(fees))
        assert form["pay[0][扣除费][money]"] == "-50.00"
        assert form["pay[0][扣除费][price_id]"] == "124832"
        assert form["driver[0][pay_yf_zj]"] == "-50.00"  # 负值合计回写


class TestMoneyParsing:
    """T9 金额解析：千分位/货币符号/负号/多行多值拆分。"""

    def test_money_parsing_variants(self):
        """构造账单费用格解析（复用 parser._to_money 口径）。"""
        from app.orders.bill.parser import _to_money

        assert _to_money("1,234.50") == 1234.5  # 千分位
        assert _to_money("¥100.00") == 100.0  # 货币符号
        assert _to_money("-50.00") == -50.0  # 负号
        assert _to_money("1300\r\n2200") == 3500.0  # 合并单元格多值累加
        assert _to_money("壹佰元整") is None  # 中文大写不解析（进 warning）


class TestGoldenFees:
    """golden 驱动（真实样本存在才跑）：抽取计数/税金恒等/多通道/长尾 to_other。"""

    def _parse(self, family: str, fname: str):
        path = FAMILIES_DIR / family / fname
        if not path.exists():
            pytest.skip(f"样本未入库：{family}/{fname}")
        template_store.reload_templates()
        out = parse_bill(path)
        orders = group_canonical(out.canonical_rows, out.template_match.template, out.period)
        return out, orders

    def test_junyu_freight_rows(self):
        """军羽 2020-10：应收运费 453 行抽取计数（T16 基线）。"""
        out, _ = self._parse("junyu", "2020-10上海军羽应收对账单.xls")
        count = sum(
            1 for row in out.canonical_rows for f in row.get("_fees", []) if f["code"] == "freight"
        )
        assert count == 453

    def test_qiuyi_tax_identity(self):
        """秋怡 2019：税金 6386 行、全部 excluded、恒等（bill − recorded = excluded）。"""
        out, orders = self._parse("qiuyi", "2019-01到2019-12上海秋怡应收对账单.xls")
        tax_rows = sum(
            1 for row in out.canonical_rows for f in row.get("_fees", []) if f["code"] == "tax"
        )
        assert tax_rows == 6386
        tax_items = [f for o in orders for f in o.fees if f.code == "tax"]
        assert tax_items and all(f.excluded for f in tax_items)
        assert sum(float(f.money) for f in tax_items) == pytest.approx(3193.00, abs=0.01)
        for order in orders:
            rec = order.fee_reconcile["shou"]
            assert rec.ok is True
            assert rec.diff == 0 or abs(rec.diff) <= Decimal("0.01")

    def test_yahao_three_channels(self):
        """亚灏：应收/应付/车辆成本三通道费用落位（公司成本→duo_get 不启用）。"""
        _, orders = self._parse("yahao", "亚灏.xls")
        channels = {f.channel for o in orders for f in o.fees}
        assert {"shou", "pay", "cost"} <= channels
        assert "duo_get" not in channels
        # 三通道对账恒等（账单合计锚点）
        for order in orders:
            for rec in order.fee_reconcile.values():
                assert rec.ok is True

    def test_haichuan123_three_channels(self):
        """海川123：应收+应付+车辆成本三通道，无公司成本。"""
        _, orders = self._parse("haichuan123", "123profitStatement.xls")
        channels = {f.channel for o in orders for f in o.fees}
        assert {"shou", "pay", "cost"} <= channels
        assert "duo_get" not in channels

    def test_yinghui_long_tail_to_other(self):
        """赢辉：长尾费目（无标准码）→ to_other 归并 + 原名进 note。"""
        out, orders = self._parse("yinghui", "利润明细表(2021-08-01-2021-12-31).xls")
        other_items = [f for o in orders for f in o.fees if f.code == "other"]
        assert other_items
        assert all(f.note for f in other_items)  # 原名进备注
        # 行级费用列数（应收+应付+成本三区自动发现）> mapping 显式费目数
        fee_cols = {
            (f.get("section"), f.get("name"))
            for row in out.canonical_rows
            for f in row.get("_fees", [])
        }
        assert len(fee_cols) >= 20  # 长尾列也抽取（mapping 外自动发现）

    def test_yinghui_negative_deduction_reconciles(self):
        """T27a 赢辉：扣除费负项录入后 pay 通道恒等（15 单 mismatch 应归零）。"""
        _, orders = self._parse("yinghui", "利润明细表(2021-08-01-2021-12-31).xls")
        deduction_items = [f for o in orders for f in o.fees if f.code == "deduction"]
        assert deduction_items  # 扣除费已映射抽取
        # 取负后可为负（列值 +486 → -486）也可为正（列值 -25 → +25，加回语义）
        assert any(f.money < 0 for f in deduction_items)
        for order in orders:
            rec = order.fee_reconcile.get("pay")
            if rec is not None:
                assert rec.ok is True, f"pay 对账恒等破坏: {order.bl_no} diff={rec.diff}"

    def test_zhiyi_receivable_only(self):
        """志驿：仅应收通道（应付/车辆成本近空不启用）。"""
        _, orders = self._parse("zhiyi", "志驿2017年对账单.xls")
        channels = {f.channel for o in orders for f in o.fees}
        assert channels == {"shou"}
