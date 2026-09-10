"""未识别列告警测试：ParseOutput.unmatched_headers。"""

from __future__ import annotations

from app.orders.bill import parse_bill
from helpers import build_bill_bytes


class TestRealBill:
    def test_unmatched_empty(self, real_parse):
        """真实账单 32 列全覆盖：清单为空。"""
        assert real_parse.unmatched_headers == []

    def test_ignored_headers_not_reported(self, real_parse):
        """「当前状态」「已收/付金额」属有意忽略，不在告警清单。"""
        assert "当前状态" not in real_parse.unmatched_headers
        assert "已收/付金额" not in real_parse.unmatched_headers


class TestConstructed:
    HEADERS = {"A": "序号", "B": "客户编号", "C": "提单号", "D": "箱型"}

    def test_unknown_column_with_data(self, tmp_path):
        """数字未知列（含金额）→ 动态收录为费用，不再上报（
        费用项不写死、账单新增费用列自动收录，无需先声明模板）。"""
        path = tmp_path / "unknown.xlsx"
        path.write_bytes(
            build_bill_bytes(
                {**self.HEADERS, "E": "其他费"},
                [{"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ", "E": 500}],
            )
        )
        out = parse_bill(path)
        assert out.unmatched_headers == []  # 已收录不再上报
        assert out.rows[0].fees["其他费"] == 500.0

    def test_unknown_text_column_still_reported(self, tmp_path):
        """纯文本未知列（无可转金额单元格）保持未识别上报（动态收录判据=
        数据区含金额，避免备注类文本列误收）。"""
        path = tmp_path / "unknown-text.xlsx"
        path.write_bytes(
            build_bill_bytes(
                {**self.HEADERS, "E": "客户账期"},
                [{"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ", "E": "月结30天"}],
            )
        )
        out = parse_bill(path)
        assert out.unmatched_headers == ["客户账期"]
        assert "客户账期" not in out.rows[0].fees

    def test_decor_column_empty(self, tmp_path):
        """装饰列表头但数据全空 → 不上报。"""
        path = tmp_path / "decor.xlsx"
        path.write_bytes(
            build_bill_bytes(
                {**self.HEADERS, "E": "装饰列"},
                [{"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ"}],
            )
        )
        assert parse_bill(path).unmatched_headers == []

    def test_derived_month_column_reported(self, tmp_path):
        """「月份」列走派生逻辑（不在 IGNORED_HEADERS）→ 良性告警。"""
        path = tmp_path / "month.xlsx"
        path.write_bytes(
            build_bill_bytes(
                {**self.HEADERS, "E": "月份"},
                [{"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ", "E": "2015-09"}],
            )
        )
        assert parse_bill(path).unmatched_headers == ["月份"]

    def test_ignored_headers_with_data(self, tmp_path):
        """「当前状态」列即使有数据也不告警（IGNORED_HEADERS 生效）。"""
        path = tmp_path / "ignored.xlsx"
        path.write_bytes(
            build_bill_bytes(
                {**self.HEADERS, "E": "当前状态"},
                [{"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ", "E": "接单"}],
            )
        )
        assert parse_bill(path).unmatched_headers == []
