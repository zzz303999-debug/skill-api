"""aggregator 测试：真实账单一行一票基线 + 构造用例（清洗/分组/box/driver/日期/c_note）。

2026-08-31 业务拍板：一行一票（每条数据行独立成单，不再按提单号合并）。
"""

from __future__ import annotations

import xlrd

from app.orders.bill import BillPeriod, BillRow, group_orders
from app.orders.bill.aggregator import clean_order_num
from helpers import PERIOD_2015, REAL_ORDER_COUNT, REAL_TOTAL_ROWS, REAL_XLS


def go(rows, period):
    """group_orders 便捷包装：只取 orders。"""
    return group_orders(rows, period).orders


class TestRealBill:
    def test_counts(self, real_orders):
        """真实账单：1090 数据行 → 1090 单（一行一票，单数=行数）。"""
        assert len(real_orders) == REAL_ORDER_COUNT
        assert all(o.row_count == 1 for o in real_orders)
        assert sum(o.row_count for o in real_orders) == REAL_TOTAL_ROWS

    def test_no_tail_rows(self, real_orders):
        """无合计/大写/箱量/制单行混入。"""
        for order in real_orders:
            assert not any(k in str(order.order_data) for k in ("合计", "大写", "箱量", "制单人"))

    def test_cnsha493890(self, real_orders):
        """同提单号两行 → 两单（一行一票）：各单 row_count=1、box 单箱、费用行级。"""
        group = [o for o in real_orders if o.order_num1 == "CNSHA493890"]
        assert len(group) == 2
        assert all(o.row_count == 1 for o in group)
        # 按行序箱型：首行 40GP、次行 40HQ，各 1 箱（不再合并为一票两箱型）
        assert [o.order_data["box"] for o in group] == [
            [{"b_type": "40GP", "box_num": 1}],
            [{"b_type": "40HQ", "box_num": 1}],
        ]
        # 费用不跨行累加：两单运费之和 = 原合并口径 3100
        assert sum(o.order_data["driver"][0]["get_ys_zj"] for o in group) == 3100.0
        for o in group:
            assert o.order_data["data"] == [{"b_order_num": "CNSHA493890"}]
            assert o.order_data["driver"][0]["b_date"] == "2015-09-01"
            assert o.order_data["month"] == "2015-09"

    def test_shsb52307200(self, real_orders):
        """同号三行 40HQ → 三单，各 40HQ×1（不再同型跨行累加）。"""
        group = [o for o in real_orders if o.order_num1 == "SHSB52307200"]
        assert len(group) == 3
        assert all(o.order_data["box"] == [{"b_type": "40HQ", "box_num": 1}] for o in group)

    def test_float_tail_cleaned(self, real_orders):
        """579609736.0 浮点尾巴清洗 → 两单同号，各 40HQ×1。"""
        group = [o for o in real_orders if o.order_num1 == "579609736"]
        assert len(group) == 2
        assert all(o.row_count == 1 for o in group)
        assert all(o.order_data["box"] == [{"b_type": "40HQ", "box_num": 1}] for o in group)

    def test_45hq_pure_tickets(self, real_orders):
        """纯 45HQ 票：45HQ 合法（业务确认 2026-08-12），正常归集无缺失。"""
        o = next(o for o in real_orders if o.order_num1 == "SHSO56743700")
        assert o.order_data["box"] == [{"b_type": "45HQ", "box_num": 1}]
        assert "box" not in o.missing_fields
        group = [o for o in real_orders if o.order_num1 == "NYKS2600413120"]
        assert len(group) == 2
        assert all(o.order_data["box"] == [{"b_type": "45HQ", "box_num": 1}] for o in group)
        assert all("box" not in o.missing_fields for o in group)

    def test_45hq_mixed_ticket(self, real_orders):
        """混合票三行 → 三单，各一箱型（按行序 40HQ/40HQ/45HQ）。"""
        group = [o for o in real_orders if o.order_num1 == "SHSO57174600"]
        assert len(group) == 3
        assert [o.order_data["box"][0]["b_type"] for o in group] == ["40HQ", "40HQ", "45HQ"]
        assert all(o.order_data["box"][0]["box_num"] == 1 for o in group)
        assert all("box" not in o.missing_fields for o in group)

    def test_max_group_eglv(self):
        """原最大组 14 行 → 14 单，箱型分布与 xlrd 手工核算一致。"""
        rows, _ = group_orders_rows()
        group = [o for o in rows if o.order_num1 == "EGLV142554617406"]
        wb = xlrd.open_workbook(REAL_XLS, formatting_info=True)
        ws = wb.sheet_by_index(0)
        manual = {}
        for r in range(6, ws.nrows):
            if str(ws.cell_value(r, 7)).strip() == "EGLV142554617406":
                bt = str(ws.cell_value(r, 9)).strip().upper()
                manual[bt] = manual.get(bt, 0) + 1
        assert len(group) == 14
        assert all(o.row_count == 1 for o in group)
        got: dict[str, int] = {}
        for o in group:
            for b in o.order_data["box"]:
                got[b["b_type"]] = got.get(b["b_type"], 0) + b["box_num"]
        assert manual == got


def group_orders_rows():
    """真实账单：parse + group，返回 (orders, period)。"""
    from app.orders.bill import parse_bill

    out = parse_bill(REAL_XLS)
    return go(out.rows, out.period), out.period


class TestConstructed:
    def test_empty_bl_standalone(self):
        """空提单号两行 → 各自单独成组、None、原文未找到。"""
        rows = [
            BillRow(seq="1", c_title="甲", b_type="40HQ"),
            BillRow(seq="2", c_title="乙", b_type="40HQ"),
        ]
        orders = go(rows, PERIOD_2015)
        assert len(orders) == 2
        assert all(o.row_count == 1 and o.order_num1 is None for o in orders)
        assert all(o.missing_reasons.get("order_num1") == "原文未找到" for o in orders)
        assert all(o.order_data["order_num1"] is None for o in orders)

    def test_alpha_bl_invalid(self):
        """纯字母提单号 → 格式不合法。"""
        assert clean_order_num("ABCDEFGHI") == (None, "格式不合法")
        o = go(
            [BillRow(seq="1", c_title="丙", order_num1="ABCDEFGHI", b_type="40HQ")], PERIOD_2015
        )[0]
        assert o.order_num1 is None
        assert o.missing_reasons.get("order_num1") == "格式不合法"

    def test_blank_box_missing(self):
        """箱型列为空 → box 缺失（原文未找到），不做格式校验。"""
        o = go([BillRow(seq="1", c_title="壬", order_num1="TESTBL10007")], PERIOD_2015)[0]
        assert o.order_data["box"] == []
        assert o.missing_fields == ["box"]
        assert o.missing_reasons.get("box") == "原文未找到"

    def test_53xx_box_valid(self):
        """放宽后 53 尺等非 20/25/40/45 前缀箱型（53HC）同样正常归集。"""
        o = go(
            [BillRow(seq="1", c_title="癸", order_num1="TESTBL10008", b_type="53HC")], PERIOD_2015
        )[0]
        assert o.order_data["box"] == [{"b_type": "53HC", "box_num": 1}]
        assert "box" not in o.missing_fields

    def test_ot_rf_rh_suffix_valid(self):
        """真实行业后缀 OT（开顶）/RF（冷藏）/RH（冷藏高柜）均正常归集。"""
        orders = go(
            [
                BillRow(seq="1", c_title="子", order_num1="TESTBL10009", b_type="40OT"),
                BillRow(seq="2", c_title="子", order_num1="TESTBL10009", b_type="40RF"),
                BillRow(seq="3", c_title="子", order_num1="TESTBL10009", b_type="45RH"),
            ],
            PERIOD_2015,
        )
        assert [o.order_data["box"] for o in orders] == [
            [{"b_type": "40OT", "box_num": 1}],
            [{"b_type": "40RF", "box_num": 1}],
            [{"b_type": "45RH", "box_num": 1}],
        ]
        assert all("box" not in o.missing_fields for o in orders)

    def test_nonstandard_box_valid(self):
        """业务确认的各类非标箱型表述（车型/中文/尺寸/单位）均正常归集。"""
        orders = go(
            [
                BillRow(seq="1", c_title="丑", order_num1="TESTBL10010", b_type="大冷"),
                BillRow(seq="2", c_title="丑", order_num1="TESTBL10010", b_type="2X20"),
                BillRow(seq="3", c_title="丑", order_num1="TESTBL10010", b_type="17M飞翼车"),
                BillRow(seq="4", c_title="丑", order_num1="TESTBL10010", b_type="拼箱"),
                BillRow(seq="5", c_title="丑", order_num1="TESTBL10010", b_type="12T"),
                BillRow(seq="6", c_title="丑", order_num1="TESTBL10010", b_type="B/L"),
            ],
            PERIOD_2015,
        )
        assert [o.order_data["box"] for o in orders] == [
            [{"b_type": "大冷", "box_num": 1}],
            [{"b_type": "2X20", "box_num": 1}],
            [{"b_type": "17M飞翼车", "box_num": 1}],
            [{"b_type": "拼箱", "box_num": 1}],
            [{"b_type": "12T", "box_num": 1}],
            [{"b_type": "B/L", "box_num": 1}],
        ]
        assert all("box" not in o.missing_fields for o in orders)

    def test_nonstandard_box_no_cross_row_merge(self):
        """同号两行同非标箱型不再跨行累加 → 两单各大冷×1（一行一票）。"""
        orders = go(
            [
                BillRow(seq="1", c_title="寅", order_num1="TESTBL10011", b_type="大冷"),
                BillRow(seq="2", c_title="寅", order_num1="TESTBL10011", b_type="大冷"),
            ],
            PERIOD_2015,
        )
        assert len(orders) == 2
        assert all(o.order_data["box"] == [{"b_type": "大冷", "box_num": 1}] for o in orders)
        assert all("box" not in o.missing_fields for o in orders)

    def test_same_bl_no_rows_independent(self):
        """同提单号两行 → 两单：费用不跨行累加，本行箱号进 container_no。"""
        rows = [
            BillRow(
                seq="1", c_title="甲", order_num1="TESTBL10015", b_type="20GP",
                fees={"运费": 1800, "其它费": 140}, container_no="OWLU2201353",
            ),
            BillRow(
                seq="2", c_title="甲", order_num1="TESTBL10015", b_type="20GP",
                fees={"运费": 1800, "其它费": 140}, container_no="OWLU2201369",
            ),
        ]
        orders = go(rows, PERIOD_2015)
        assert len(orders) == 2
        assert [o.container_no for o in orders] == ["OWLU2201353", "OWLU2201369"]
        for o in orders:
            entries = {k: v["money"] for e in o.order_data["shou"] for k, v in e.items()}
            assert entries == {"运费": 1800.0, "其它费": 140.0}
            assert o.order_data["driver"][0]["get_ys_zj"] == 1940.0
            assert o.order_data["box"] == [{"b_type": "20GP", "box_num": 1}]

    def test_cross_year_fill(self):
        """跨年补年份：12-7→2018-12-07；1-5→2018-01-05（区间内取最早满足年份）。"""
        period = BillPeriod(start="2018-01-01", end="2019-12-31")
        rows = [
            BillRow(seq="1", c_title="丁", order_num1="TESTBL10001", b_type="40HQ", b_date="12-7"),
            BillRow(seq="2", c_title="丁", order_num1="TESTBL10001", b_type="40HQ", b_date="1-5"),
        ]
        orders = go(rows, period)
        # 一行一票：各行日期独立补年份，不再有「其余日期入 c_note」
        assert [o.order_data["driver"][0]["b_date"] for o in orders] == ["2018-12-07", "2018-01-05"]
        assert all("c_note" not in o.order_data for o in orders)
        o2 = go(
            [BillRow(seq="1", c_title="丁", order_num1="TESTBL10002", b_type="40HQ", b_date="1-5")],
            period,
        )[0]
        assert o2.order_data["driver"][0]["b_date"] == "2018-01-05"  # 多年可满足取最早年份

    def test_no_period_b_date_omitted(self):
        """无 period → b_date/month 省略且不进 missing_fields。"""
        o = go(
            [BillRow(seq="1", c_title="戊", order_num1="TESTBL10003", b_type="40HQ", b_date="9-1")],
            BillPeriod(),
        )[0]
        assert "b_date" not in o.order_data["driver"][0]
        assert "month" not in o.order_data
        assert o.missing_fields == []

    def test_row_dates_independent(self):
        """同号两行不同日期 → 两单各用本行日期（不再首行+备注合并）。"""
        rows = [
            BillRow(seq="1", c_title="己", order_num1="TESTBL10004", b_type="40HQ", b_date="9-1"),
            BillRow(seq="2", c_title="己", order_num1="TESTBL10004", b_type="40HQ", b_date="9-2"),
        ]
        orders = go(rows, PERIOD_2015)
        assert [o.order_data["driver"][0]["b_date"] for o in orders] == ["2015-09-01", "2015-09-02"]
        assert all("c_note" not in o.order_data for o in orders)

    def test_c_note_segments(self):
        """c_note 段顺序与分隔符；全空段省略。"""
        row = BillRow(
            seq="1",
            c_title="庚",
            order_num1="TESTBL10005",
            b_type="40HQ",
            c_sn="SN001",
            biz_type="进口",
            container_no="TCLU1111111",
            fleet="车队A",
            remark="备注X",
            payable_remark="应付备注Y",
            b_date="9-1",
        )
        o = go([row], PERIOD_2015)[0]
        assert o.order_data.get("c_note") == (
            "客户编号：SN001；业务类型：进口；箱号：TCLU1111111；车队：车队A；备注X；应付备注Y"
        )
        o2 = go(
            [BillRow(seq="1", c_title="辛", order_num1="TESTBL10006", b_type="40HQ")], PERIOD_2015
        )[0]
        assert "c_note" not in o2.order_data

    def test_plate_cleaned_per_row(self):
        """多行车牌：浮点尾巴清洗按行生效（9486.0→9486），单行车牌不追加 c_note 段。"""
        rows = [
            BillRow(seq="1", c_title="卯", order_num1="TESTBL10012", b_type="40HQ", d_num="9486.0"),
            BillRow(seq="2", c_title="卯", order_num1="TESTBL10012", b_type="40HQ", d_num="7399.0"),
            BillRow(seq="3", c_title="卯", order_num1="TESTBL10012", b_type="40HQ", d_num="9002.0"),
        ]
        orders = go(rows, PERIOD_2015)
        assert [o.order_data["driver"][0]["d_num"] for o in orders] == ["9486", "7399", "9002"]
        assert all("c_note" not in o.order_data for o in orders)

    def test_single_plate_cleaned_no_c_note_segment(self):
        """单车牌：清洗浮点尾巴，不追加 c_note 车牌段（备注保持原样）。"""
        o = go(
            [BillRow(seq="1", c_title="辰", order_num1="TESTBL10013", b_type="40HQ", d_num="9486.0")],
            PERIOD_2015,
        )[0]
        assert o.order_data["driver"][0]["d_num"] == "9486"
        assert "c_note" not in o.order_data

    def test_text_plate_unchanged(self):
        """文字车牌（沪A12345）不受清洗影响。"""
        o = go(
            [BillRow(seq="1", c_title="巳", order_num1="TESTBL10014", b_type="40HQ", d_num="沪A12345")],
            PERIOD_2015,
        )[0]
        assert o.order_data["driver"][0]["d_num"] == "沪A12345"
