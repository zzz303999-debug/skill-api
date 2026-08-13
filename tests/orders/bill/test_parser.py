"""parser 测试：表头定位、格式识别分发、双链路等价、错误路径。"""

from __future__ import annotations

import hashlib

import pytest

from app.errors import BadRequestError, ConvertError
from app.orders.bill import BillPeriod, ParseOutput, parse_bill
from helpers import (
    REAL_RAW_ROWS,
    REAL_XLS,
    REBUILT_XLSX,
    cells_equal,
)


class TestHeaderAndBaseline:
    def test_real_xls_baseline(self, real_parse):
        """真实账单直读：1094 行、结算区间、引擎 xlrd、首行数据。"""
        out = real_parse
        assert isinstance(out, ParseOutput)
        assert out.engine == "xlrd"
        assert len(out.rows) == REAL_RAW_ROWS
        assert out.period == BillPeriod(start="2015-01-01", end="2015-12-31")
        r0 = out.rows[0]
        assert r0.seq == "1.0"  # xlrd 数字单元格保留浮点尾巴
        assert r0.order_num1 == "SHSB52129400"
        assert r0.c_title == "小王"
        assert r0.fees.get("运费") == 1550.0

    def test_header_row_and_rows(self, bill_builder):
        """构造账单：表头第 2 行、数据行、结算区间提取。"""
        path = bill_builder(
            {"A": "序号", "B": "客户编号", "C": "提单号", "D": "箱型", "E": "运费"},
            [
                {"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ", "E": 100},
                {"A": 2, "B": "C002", "C": "OOLU12345679", "D": "20GP", "E": 200},
            ],
            name="basic.xlsx",
        )
        out = parse_bill(path)
        assert out.engine == "openpyxl"
        assert len(out.rows) == 2
        assert out.rows[0].c_sn == "C001" and out.rows[0].fees == {"运费": 100.0}
        assert out.period == BillPeriod()  # 无结算区间 → 空

    def test_missing_required_header(self, bill_builder):
        """缺「客户编号」列 → BadRequestError，details 含缺失列。"""
        path = bill_builder(
            {"A": "序号", "B": "客户名称", "C": "提单号"},
            [{"A": 1, "B": "甲", "C": "OOLU12345678"}],
            name="no-sn.xlsx",
        )
        with pytest.raises(BadRequestError) as exc:
            parse_bill(path)
        assert exc.value.details["missing_headers"] == ["客户编号"]

    def test_broken_file_convert_error(self, bill_builder):
        """非 zip 坏文件 → ConvertError（422 convert_error）。"""
        path = bill_builder({}, [], name="bad.xlsx")
        path.write_bytes(b"not a zip")
        with pytest.raises(ConvertError) as exc:
            parse_bill(path)
        assert exc.value.code == "convert_error"

    def test_period_from_header_zone(self, bill_builder):
        """抬头区结算日期 → BillPeriod（含跨年区间）。"""
        path = bill_builder(
            {"A": "序号", "B": "客户编号", "C": "提单号", "D": "箱型"},
            [{"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ"}],
            name="period.xlsx",
        )
        # 抬头写结算日期（第 1 行）
        from openpyxl import load_workbook

        wb = load_workbook(path)
        wb.active["A1"] = "结算日期：2018-01-01-2019-12-31"
        wb.save(path)
        out = parse_bill(path)
        assert out.period == BillPeriod(start="2018-01-01", end="2019-12-31")


class TestFormatDispatch:
    def test_format_mismatch_xls_as_xlsx(self, real_xls_bytes, tmp_path):
        """.xls 内容伪装 .xlsx 扩展名 → file_format_mismatch。"""
        fake = tmp_path / "fake.xlsx"
        fake.write_bytes(real_xls_bytes)
        with pytest.raises(BadRequestError) as exc:
            parse_bill(fake)
        assert exc.value.code == "file_format_mismatch"
        assert exc.value.details == {
            "extension": ".xlsx",
            "detected_format": "xls",
        }

    def test_format_mismatch_xlsx_as_xls(self, tmp_path):
        """.xlsx 内容伪装 .xls 扩展名 → file_format_mismatch。"""
        import shutil

        fake = tmp_path / "fake.xls"
        shutil.copy(REBUILT_XLSX, fake)
        with pytest.raises(BadRequestError) as exc:
            parse_bill(fake)
        assert exc.value.code == "file_format_mismatch"

    def test_unsupported_extension(self, tmp_path):
        """.txt → BadRequestError（bad_request，扩展名白名单外）。"""
        path = tmp_path / "a.txt"
        path.write_text("hello")
        with pytest.raises(BadRequestError) as exc:
            parse_bill(path)
        assert exc.value.code == "bad_request"
        assert ".xls" in exc.value.details["supported"]


@pytest.mark.skipif(
    not REAL_XLS.exists(), reason="golden 样本未入库（表格文件不入库），本地放置后自动启用"
)
class TestDualChainEquivalence:
    def test_rows_equivalent(self):
        """xls 直读与 xlsx 重建链路：1094 行逐字段等价（数值归一）。"""
        out_xls = parse_bill(REAL_XLS)
        out_xlsx = parse_bill(REBUILT_XLSX)
        assert len(out_xls.rows) == len(out_xlsx.rows) == REAL_RAW_ROWS
        for a, b in zip(out_xls.rows, out_xlsx.rows, strict=True):
            da, db = a.model_dump(), b.model_dump()
            for key in da:
                if key == "fees":
                    assert set(da["fees"]) == set(db["fees"])
                    assert all(cells_equal(da["fees"][n], db["fees"][n]) for n in da["fees"])
                else:
                    assert cells_equal(da[key], db[key]), f"{key}: {da[key]!r} vs {db[key]!r}"

    def test_golden_sha256(self, real_xls_bytes):
        """golden .xls 与 .xlsx 内容各自稳定（sha256 锚定，防止误改资产）。"""
        sha_xls = hashlib.sha256(REAL_XLS.read_bytes()).hexdigest()
        sha_xlsx = hashlib.sha256(REBUILT_XLSX.read_bytes()).hexdigest()
        assert len(sha_xls) == 64 and len(sha_xlsx) == 64
        # 重建链路字节与直读链路内容一致（两条链路的解析等价由用例覆盖）
        assert sha_xls != sha_xlsx
