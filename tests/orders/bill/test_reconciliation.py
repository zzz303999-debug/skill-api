"""reconciliation 测试：真实账单对账结论 + 篡改/no_anchor 构造用例。"""

from __future__ import annotations

from app.orders.bill import group_orders, parse_bill
from app.orders.bill.cn_amount import parse_cn_upper_amount
from helpers import build_bill_bytes

# 合计行缓存值自解释文案（fees 明细留痕，不参与判定）
NOTE_CACHED = "合计行 SUM 公式缓存旧值（导出未重算），仅供留痕，不参与判定"


class TestCnUpperAmount:
    """中文大写金额解析单测（合计大写行第二锚点）。"""

    def test_cases(self):
        assert parse_cn_upper_amount("壹佰壹拾壹万贰仟肆佰零伍元整") == 1112405.0
        assert parse_cn_upper_amount("壹元贰角叁分") == 1.23
        assert parse_cn_upper_amount("伍角") == 0.5
        assert parse_cn_upper_amount("叁万元整") == 30000.0
        assert parse_cn_upper_amount("贰拾万零叁佰元整") == 200300.0

    def test_non_amount_returns_none(self):
        assert parse_cn_upper_amount("非大写文本 abc 100") is None
        assert parse_cn_upper_amount(None) is None
        assert parse_cn_upper_amount("人民币100元") is None  # 阿拉伯数字非大写段


def reconcile(path) -> dict:
    out = parse_bill(path)
    return group_orders(out.rows, out.period).reconciliation


class TestRealBill:
    def test_status_matched_via_uppercase(self, real_parse):
        """真实账单：合计行费用列为 SUM 公式缓存旧值（每列 4.0），
        合计大写金额（1112405 元）与归集一致 → status=matched。"""
        rec = group_orders(real_parse.rows, real_parse.period).reconciliation
        assert rec["status"] == "matched"
        assert rec["fees_status"] == "matched"
        assert rec["boxes_status"] == "matched"
        # 合计行口径缓存值留痕（数据可追溯，键名自解释）
        assert rec["fees"]["运费"] == {
            "aggregated": 1073480.0,
            "bill_total_cached": 4.0,
            "bill_total_note": NOTE_CACHED,
            "diff": 1073476.0,
        }
        assert rec["fees"]["待时费"] == {
            "aggregated": 5300.0,
            "bill_total_cached": 4.0,
            "bill_total_note": NOTE_CACHED,
            "diff": 5296.0,
        }
        assert rec["fees"]["预提费"] == {
            "aggregated": 27000.0,
            "bill_total_cached": 4.0,
            "bill_total_note": NOTE_CACHED,
            "diff": 26996.0,
        }
        assert rec["fees"]["洋山费"] == {
            "aggregated": 0.0,
            "bill_total_cached": 4.0,
            "bill_total_note": NOTE_CACHED,
            "diff": -4.0,
        }
        assert rec["fees"]["落还箱费"] == {
            "aggregated": 3605.0,
            "bill_total_cached": 4.0,
            "bill_total_note": NOTE_CACHED,
            "diff": 3601.0,
        }
        assert rec["fees"]["其它费"] == {
            "aggregated": 3020.0,
            "bill_total_cached": 4.0,
            "bill_total_note": NOTE_CACHED,
            "diff": 3016.0,
        }
        # 合计大写第二锚点吻合
        assert rec["fees_uppercase_total"] == {
            "aggregated": 1112405.0,
            "bill_total": 1112405.0,
            "diff": 0.0,
        }
        assert "公式缓存" in rec["note"]
        assert "大写金额与归集一致" in rec["note"]

    def test_boxes_all_matched(self, real_parse):
        """真实账单箱型锚点：40HQ/20GP/40GP/45HQ 全部 diff=0（45HQ 已合法化归集）。"""
        rec = group_orders(real_parse.rows, real_parse.period).reconciliation
        boxes = rec["boxes"]
        assert boxes["40HQ"] == {
            "aggregated": 988,
            "bill_total": 988,
            "diff": 0,
        }
        assert boxes["20GP"] == {
            "aggregated": 50,
            "bill_total": 50,
            "diff": 0,
        }
        assert boxes["40GP"] == {
            "aggregated": 48,
            "bill_total": 48,
            "diff": 0,
        }
        assert boxes["45HQ"] == {"aggregated": 4, "bill_total": 4, "diff": 0}

    def test_total_matches_uppercase_amount(self, real_parse):
        """归集费用总和与账单「合计大写」金额（1112405 元）吻合。"""
        rec = group_orders(real_parse.rows, real_parse.period).reconciliation
        total = sum(v["aggregated"] for v in rec["fees"].values())
        assert abs(total - 1112405.0) < 1.0


class TestConstructed:
    HEADERS = {"A": "序号", "B": "客户编号", "C": "提单号", "D": "箱型", "E": "运费"}

    def test_tamper_mismatch(self, tmp_path):
        """篡改：归集 110 vs 合计行 100 → fees mismatch；箱型仍 matched。"""
        path = tmp_path / "tamper.xlsx"
        path.write_bytes(
            build_bill_bytes(
                self.HEADERS,
                [
                    {"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ", "E": 50},
                    {"A": 2, "B": "C002", "C": "OOLU12345679", "D": "40HQ", "E": 60},
                    {"A": "合计:", "E": 100},
                    {"A": "总箱型箱量：40HQ*2"},
                ],
            )
        )
        rec = reconcile(path)
        assert rec["status"] == "mismatch"
        assert rec["fees_status"] == "mismatch"
        assert rec["boxes_status"] == "matched"
        assert rec["fees"]["运费"] == {
            "aggregated": 110.0,
            "bill_total_cached": 100.0,
            "bill_total_note": NOTE_CACHED,
            "diff": 10.0,
        }
        assert rec["fees_uppercase_total"] is None
        assert rec["note"] is None
        assert rec["boxes"]["40HQ"]["diff"] == 0

    def test_no_sum_row_fees_nulled(self, tmp_path):
        """无合计行但有费用 → fees 逐项留痕，锚点值为 null 并注明原因。"""
        path = tmp_path / "no-sum.xlsx"
        path.write_bytes(
            build_bill_bytes(
                self.HEADERS,
                [
                    {"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ", "E": 50},
                    {"A": "总箱型箱量：40HQ*1"},
                ],
            )
        )
        rec = reconcile(path)
        assert rec["fees"] == {
            "运费": {
                "aggregated": 50.0,
                "bill_total_cached": None,
                "bill_total_note": "账单无合计行",
                "diff": None,
            }
        }
        assert rec["fees_status"] == "no_anchor"
        assert rec["boxes_status"] == "matched"
        assert rec["status"] == "matched"

    def test_tamper_with_uppercase_mismatch(self, tmp_path):
        """费用篡改且合计大写同步篡改 → fees_status=mismatch（双锚点均不吻合）。"""
        path = tmp_path / "tamper-upper.xlsx"
        path.write_bytes(
            build_bill_bytes(
                self.HEADERS,
                [
                    {"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ", "E": 50},
                    {"A": "合计:", "E": 100},
                    {"A": "合计大写：", "E": "壹佰贰拾元整"},  # 金额列放费用列，贴近真实账单
                    {"A": "总箱型箱量：40HQ*1"},
                ],
            )
        )
        rec = reconcile(path)
        assert rec["fees_status"] == "mismatch"
        assert rec["status"] == "mismatch"
        assert rec["fees_uppercase_total"] == {
            "aggregated": 50.0,
            "bill_total": 120.0,
            "diff": -70.0,
        }
        assert rec["note"] is None

    def test_uppercase_anchor_matched(self, tmp_path):
        """合计行吻合且大写吻合 → fees_status=matched，note=null。"""
        path = tmp_path / "upper-ok.xlsx"
        path.write_bytes(
            build_bill_bytes(
                self.HEADERS,
                [
                    {"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ", "E": 50},
                    {"A": "合计:", "E": 50},
                    {"A": "合计大写：", "E": "伍拾元整"},
                    {"A": "总箱型箱量：40HQ*1"},
                ],
            )
        )
        rec = reconcile(path)
        assert rec["status"] == "matched"
        assert rec["fees_status"] == "matched"
        assert rec["fees_uppercase_total"] == {
            "aggregated": 50.0,
            "bill_total": 50.0,
            "diff": 0.0,
        }
        assert rec["note"] is None

    def test_no_anchor(self, tmp_path):
        """无合计行无箱量行 → 全 no_anchor，不报错。"""
        path = tmp_path / "no-anchor.xlsx"
        path.write_bytes(
            build_bill_bytes(
                {"A": "序号", "B": "客户编号", "C": "提单号", "D": "箱型"},
                [{"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ"}],
            )
        )
        assert reconcile(path) == {
            "status": "no_anchor",
            "fees_status": "no_anchor",
            "boxes_status": "no_anchor",
            "fees": None,
            "fees_uppercase_total": None,
            "boxes": None,
            "note": None,
        }
