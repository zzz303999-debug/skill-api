"""T1 normalizers 参数化单测：六个归一化函数 + year_hint 解析。

覆盖 37 个样本实测出现过的脏数据形态（见《接入设计》§5 与《字段映射表》§1）：
月-日/完整日期/datetime 日期、浮点尾巴、箱型×数量/描述后缀/非标箱型、
船名航次合并列、件数毛重空值。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.orders.bill.normalizers import (
    box_parse,
    date_flex,
    parse_year_hint,
    strip_float_tail,
    to_int,
    to_number,
    vessel_voyage_split,
)


class TestDateFlex:
    @pytest.mark.parametrize(
        ("value", "year_hint", "expected"),
        [
            # M-D 单年区间
            ("1-1", (2018, 2018), "2018-01-01"),
            ("9-1", (2015, 2015), "2015-09-01"),
            ("12-31", (2018, 2018), "2018-12-31"),
            # M-D 跨年区间：12→1 月进位（M ≤ 6 归结束年，M > 6 归起始年）
            ("12-7", (2018, 2019), "2018-12-07"),
            ("1-5", (2018, 2019), "2019-01-05"),
            ("6-30", (2018, 2019), "2019-06-30"),
            ("7-1", (2018, 2019), "2018-07-01"),
            # 完整日期
            ("2021-01-01", None, "2021-01-01"),
            ("2020-12-11 12:05", None, "2020-12-11"),
            ("2020-12-11T12:05:00", None, "2020-12-11"),
            # 引擎原生值
            (datetime(2018, 5, 4, 9, 30), None, "2018-05-04"),
            # 非法/空值
            ("", (2018, 2018), None),
            ("13-1", (2018, 2018), None),
            ("2021-02-30", None, None),
            ("abc", (2018, 2018), None),
            ("1-5", None, None),  # 无年份提示不补年
        ],
    )
    def test_date_flex(self, value, year_hint, expected):
        assert date_flex(value, year_hint) == expected


class TestParseYearHint:
    def test_filename_range(self):
        assert parse_year_hint("2018-01到2018-12上海通寰应收对账单.xls") == (2018, 2018)

    def test_filename_cross_year_range(self):
        assert parse_year_hint("2018-01到2019-12上海秋怡应收对账单.xls") == (2018, 2019)

    def test_filename_month_only(self):
        assert parse_year_hint("2020-10上海军羽应收对账单.xls") == (2020, 2020)

    def test_filename_year_only(self):
        assert parse_year_hint("志驿2017年对账单.xls") == (2017, 2017)

    def test_filename_embedded_period(self):
        assert parse_year_hint("利润明细表(2021-08-01-2021-12-31).xls") == (2021, 2021)

    def test_settlement_row_preferred(self):
        text = "结算日期：2018-01-01-2018-12-31"
        assert parse_year_hint("2017-01到2017-12xxx.xls", text) == (2018, 2018)

    def test_settlement_row_cross_year(self):
        text = "结算日期：2018-01-01-2019-12-31"
        assert parse_year_hint("xxx.xls", text) == (2018, 2019)

    def test_no_hint(self):
        assert parse_year_hint("对账单.xls") is None
        assert parse_year_hint("对账单.xls", "无日期") is None


class TestStripFloatTail:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("574848870.0", "574848870"),
            ("6588.0", "6588"),
            (574848870.0, "574848870"),  # 引擎数字原生值
            ("OOLU12345678", "OOLU12345678"),  # 非数字原样
            ("1/14号收", "1/14号收"),  # 封条号混文本不误伤
            ("1.5", "1.5"),  # 非 .0 尾巴不处理
            ("", None),
            (None, None),
        ],
    )
    def test_strip_float_tail(self, value, expected):
        assert strip_float_tail(value) == expected


class TestBoxParse:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("40HQ", [{"type": "40HQ", "qty": 1}]),
            ("40HQ*2", [{"type": "40HQ", "qty": 2}]),
            ("40GP+40HQ", [{"type": "40GP", "qty": 1}, {"type": "40HQ", "qty": 1}]),
            ("20GP 主段", [{"type": "20GP", "qty": 1}]),  # 描述后缀清洗
            ("20GP*2 主段", [{"type": "20GP", "qty": 2}]),
            # 非标箱型原样保留
            ("大冷", [{"type": "大冷", "qty": 1}]),
            ("飞翼", [{"type": "飞翼", "qty": 1}]),
            ("拼箱", [{"type": "拼箱", "qty": 1}]),
            ("", None),
            (None, None),
        ],
    )
    def test_box_parse(self, value, expected):
        assert box_parse(value) == expected


class TestVesselVoyageSplit:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("MAERSK SARNIA/752E", ("MAERSK SARNIA", "752E")),
            ("COSCO SHIPPING/012W", ("COSCO SHIPPING", "012W")),
            ("无航次船名", ("无航次船名", None)),  # 无 / 整体归船名
            ("", (None, None)),
            (None, (None, None)),
        ],
    )
    def test_vessel_voyage_split(self, value, expected):
        assert vessel_voyage_split(value) == expected


class TestToIntNumber:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("12", 12),
            (12, 12),
            ("12.0", 12),  # Excel 数字单元格
            ("", None),
            ("abc", None),
            (None, None),
        ],
    )
    def test_to_int(self, value, expected):
        assert to_int(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1.5", 1.5),
            (1500, 1500.0),
            ("1500.0", 1500.0),
            ("", None),
            ("abc", None),
            (None, None),
        ],
    )
    def test_to_number(self, value, expected):
        assert to_number(value) == expected
