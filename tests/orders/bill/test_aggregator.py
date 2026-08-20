"""aggregator 测试：真实账单归集基线 + 构造用例（清洗/分组/box/driver/日期/c_note）。"""

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
        """真实账单：1090 数据行 → 820 单。"""
        assert len(real_orders) == REAL_ORDER_COUNT
        assert sum(o.row_count for o in real_orders) == REAL_TOTAL_ROWS

    def test_no_tail_rows(self, real_orders):
        """无合计/大写/箱量/制单行混入。"""
        for order in real_orders:
            assert not any(k in str(order.order_data) for k in ("合计", "大写", "箱量", "制单人"))

    def test_cnsha493890(self, real_orders):
        """一票多柜（40GP+40HQ）：box/shou/get_ys_zj/data。"""
        o = {x.order_num1: x for x in real_orders}["CNSHA493890"]
        assert o.row_count == 2
        assert o.order_data["box"] == [
            {"b_type": "40GP", "box_num": 1},
            {"b_type": "40HQ", "box_num": 1},
        ]
        assert o.order_data["shou"] == [{"运费": {"money": 3100.0}}]
        assert o.order_data["driver"][0]["get_ys_zj"] == 3100.0
        assert len(o.order_data["data"]) == 2
        assert all(d["b_order_num"] == "CNSHA493890" for d in o.order_data["data"])
        assert o.order_data["driver"][0]["b_date"] == "2015-09-01"
        assert o.order_data["month"] == "2015-09"

    def test_shsb52307200(self, real_orders):
        """同箱型累加 40HQ×3。"""
        o = {x.order_num1: x for x in real_orders}["SHSB52307200"]
        assert o.order_data["box"] == [{"b_type": "40HQ", "box_num": 3}]

    def test_float_tail_cleaned(self, real_orders):
        """579609736.0 浮点尾巴清洗后同组 40HQ×2。"""
        o = {x.order_num1: x for x in real_orders}["579609736"]
        assert o.order_data["box"] == [{"b_type": "40HQ", "box_num": 2}]
        assert o.row_count == 2

    def test_45hq_pure_tickets(self, real_orders):
        """纯 45HQ 票：45HQ 合法（业务确认 2026-08-12），正常归集无缺失。"""
        by_no = {x.order_num1: x for x in real_orders}
        o = by_no["SHSO56743700"]
        assert o.order_data["box"] == [{"b_type": "45HQ", "box_num": 1}]
        assert "box" not in o.missing_fields
        o = by_no["NYKS2600413120"]
        assert o.order_data["box"] == [{"b_type": "45HQ", "box_num": 2}]
        assert "box" not in o.missing_fields

    def test_45hq_mixed_ticket(self, real_orders):
        """混合票（40HQ×2 + 45HQ×1）：两箱型均正常归集。"""
        o = {x.order_num1: x for x in real_orders}["SHSO57174600"]
        assert o.order_data["box"] == [
            {"b_type": "40HQ", "box_num": 2},
            {"b_type": "45HQ", "box_num": 1},
        ]
        assert "box" not in o.missing_fields

    def test_max_group_eglv(self):
        """最大组 14 行：row_count 与 xlrd 手工核算 box 一致。"""
        rows, _ = group_orders_rows()
        o = {x.order_num1: x for x in rows}["EGLV142554617406"]
        wb = xlrd.open_workbook(REAL_XLS, formatting_info=True)
        ws = wb.sheet_by_index(0)
        manual = {}
        for r in range(6, ws.nrows):
            if str(ws.cell_value(r, 7)).strip() == "EGLV142554617406":
                bt = str(ws.cell_value(r, 9)).strip().upper()
                manual[bt] = manual.get(bt, 0) + 1
        got = {b["b_type"]: b["box_num"] for b in o.order_data["box"]}
        assert o.row_count == 14
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
        o = go(
            [
                BillRow(seq="1", c_title="子", order_num1="TESTBL10009", b_type="40OT"),
                BillRow(seq="2", c_title="子", order_num1="TESTBL10009", b_type="40RF"),
                BillRow(seq="3", c_title="子", order_num1="TESTBL10009", b_type="45RH"),
            ],
            PERIOD_2015,
        )[0]
        assert o.order_data["box"] == [
            {"b_type": "40OT", "box_num": 1},
            {"b_type": "40RF", "box_num": 1},
            {"b_type": "45RH", "box_num": 1},
        ]
        assert "box" not in o.missing_fields

    def test_nonstandard_box_valid(self):
        """业务确认的各类非标箱型表述（车型/中文/尺寸/单位）均正常归集。"""
        o = go(
            [
                BillRow(seq="1", c_title="丑", order_num1="TESTBL10010", b_type="大冷"),
                BillRow(seq="2", c_title="丑", order_num1="TESTBL10010", b_type="2X20"),
                BillRow(seq="3", c_title="丑", order_num1="TESTBL10010", b_type="17M飞翼车"),
                BillRow(seq="4", c_title="丑", order_num1="TESTBL10010", b_type="拼箱"),
                BillRow(seq="5", c_title="丑", order_num1="TESTBL10010", b_type="12T"),
                BillRow(seq="6", c_title="丑", order_num1="TESTBL10010", b_type="B/L"),
            ],
            PERIOD_2015,
        )[0]
        assert o.order_data["box"] == [
            {"b_type": "大冷", "box_num": 1},
            {"b_type": "2X20", "box_num": 1},
            {"b_type": "17M飞翼车", "box_num": 1},
            {"b_type": "拼箱", "box_num": 1},
            {"b_type": "12T", "box_num": 1},
            {"b_type": "B/L", "box_num": 1},
        ]
        assert "box" not in o.missing_fields

    def test_nonstandard_box_accumulate(self):
        """同非标箱型多行同样累加（如 大冷×2）。"""
        o = go(
            [
                BillRow(seq="1", c_title="寅", order_num1="TESTBL10011", b_type="大冷"),
                BillRow(seq="2", c_title="寅", order_num1="TESTBL10011", b_type="大冷"),
            ],
            PERIOD_2015,
        )[0]
        assert o.order_data["box"] == [{"b_type": "大冷", "box_num": 2}]
        assert "box" not in o.missing_fields

    def test_cross_year_fill(self):
        """跨年补年份：12-7→2018-12-07；1-5 其余日期入 c_note；单独组取最早年份。"""
        period = BillPeriod(start="2018-01-01", end="2019-12-31")
        rows = [
            BillRow(seq="1", c_title="丁", order_num1="TESTBL10001", b_type="40HQ", b_date="12-7"),
            BillRow(seq="2", c_title="丁", order_num1="TESTBL10001", b_type="40HQ", b_date="1-5"),
        ]
        o = go(rows, period)[0]
        assert o.order_data["driver"][0]["b_date"] == "2018-12-07"
        assert "其他做箱日期：1-5" in o.order_data.get("c_note", "")
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

    def test_multi_dates_into_c_note(self):
        """组内多日期 → 其余进 c_note「其他做箱日期」。"""
        rows = [
            BillRow(seq="1", c_title="己", order_num1="TESTBL10004", b_type="40HQ", b_date="9-1"),
            BillRow(seq="2", c_title="己", order_num1="TESTBL10004", b_type="40HQ", b_date="9-2"),
        ]
        o = go(rows, PERIOD_2015)[0]
        assert o.order_data["driver"][0]["b_date"] == "2015-09-01"
        assert o.order_data.get("c_note") == "其他做箱日期：9-2"

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
