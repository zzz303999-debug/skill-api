"""T8 家族 golden 测试：8 家族真实样本走「识别 → 解析 → 归一化 → CanonicalOrder」。

用例资产：tests/golden/bill/families/（MANIFEST.md 标注必需/可选；可选文件未入库
时按存在性 skip，不硬编码跳过）。L1 指纹断言以真实文件计算值为准
（template_store.compute_fingerprint 对真实表头行的 md5[:8]）。

覆盖：
- 必需文件每家族 ≥1：L1 命中 + 关键字段抽样（提单号/箱型/做箱时间）+ 行数/单数
- 秋怡/志驿多年份文件族级覆盖（全部 L1，指纹逐年漂移已被初稿 6/5 指纹覆盖）
- L2 族级近似：构造漂移表头（删列）→ L2 命中 + missing 记录
- 一票多箱聚合（秋怡同提单号多行 → containers/box_groups）与军羽 bl_no 兜底归集
- 通寰（金科信同源）经 jinxin_v1 → BillOrder → to_canonical 转换覆盖
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.orders.bill import parse_bill
from app.orders.bill.aggregation.canonical_aggregator import group_canonical
from helpers import build_bill_bytes

FAMILIES_DIR = Path(__file__).resolve().parent.parent.parent / "golden" / "bill" / "families"


def _family_path(family: str, fname: str) -> Path:
    return FAMILIES_DIR / family / fname


def _parse_family(family: str, fname: str):
    """真实文件 → (ParseOutput, CanonicalOrder 列表)；文件缺失 → skip。

    jinxin_v1（通寰）为 BillRow 语义（canonical_rows=None），归集列表为空；
    其余家族走标准字段归集。
    """
    path = _family_path(family, fname)
    if not path.exists():
        pytest.skip(f"样本未入库：{family}/{fname}")
    out = parse_bill(path)
    assert out.template_match is not None, f"{family}/{fname} 未命中任何模板"
    orders = []
    if out.canonical_rows is not None:
        orders = group_canonical(
            out.canonical_rows, out.template_match.template, out.period
        )
    return out, orders


# (family, 必需文件, 期望 template_id, 首行关键字段抽样, 行数下限, 单数下限)
_REQUIRED_CASES = [
    ("junyu", "2020-10上海军羽应收对账单.xls", "junyu_v1", "CNFC434170", 462, 220),
    ("qiuyi", "2016-01到2016-12上海秋怡应收对账单.xls", "qiuyi_v1", "177FWDWDS44551", 1731, 1005),
    ("qiuyi", "2021-01到2021-07上海秋怡应收对账单.xls", "qiuyi_v1", "CNOM460548", 1971, 1124),
    ("zhiyi", "志驿2017年对账单.xls", "zhiyi_v1", "KKLUSH5557952", 2141, 1850),
    ("zhiyi", "志驿2020对账单.xls", "zhiyi_v1", "E236076098", 1961, 1633),
    ("yahao", "亚灏.xls", "yahao_v1", "ONEYRICAKX545700", 2288, 1865),
    ("haichuan123", "123profitStatement.xls", "haichuan123_v1", "AQIZHUA200656876", 3083, 1552),
    ("tonghuan1111", "1111profitStatement.xls", "tonghuan1111_v1", "COSU6286459350", 2386, 1850),
    ("yinghui", "利润明细表(2021-08-01-2021-12-31).xls", "yinghui_v1", "TACVAN210826", 156, 129),
]

# 可选文件（多年份重复结构，CI 可不入库）：按存在性跳过
_OPTIONAL_CASES = [
    ("junyu", "2016-01到2016-12上海军羽应收对账单.xls", "junyu_v1"),
    ("junyu", "2020-12上海军羽应收对账单.xls", "junyu_v1"),
    ("junyu", "2021-02上海军羽应收对账单.xls", "junyu_v1"),
    ("tonghuan", "2016-01到2016-12上海通寰应收对账单.xls", "jinxin_v1"),
    ("tonghuan", "2019-01到2019-12上海通寰应收对账单.xls", "jinxin_v1"),
    ("qiuyi", "2018-01到2018-12上海秋怡应收对账单.xls", "qiuyi_v1"),
    ("qiuyi", "2020-01到2020-12上海秋怡应收对账单.xls", "qiuyi_v1"),
    ("zhiyi", "志驿2018对账单.xls", "zhiyi_v1"),
    ("zhiyi", "志驿对账单2019.xls", "zhiyi_v1"),
]


class TestRequiredFamilies:
    """必需文件：L1 指纹命中（指纹按真实文件计算）+ 关键字段抽样 + 行数/单数。"""

    @pytest.mark.parametrize(
        ("family", "fname", "template_id", "bl_no", "min_rows", "min_orders"),
        _REQUIRED_CASES,
    )
    def test_l1_hit_and_key_fields(
        self, family, fname, template_id, bl_no, min_rows, min_orders
    ):
        out, orders = _parse_family(family, fname)
        match = out.template_match
        # L1 精确命中（真实文件表头指纹 == 配置指纹，独立用例逐文件重算验证）
        assert match.level == "L1"
        assert match.template_id == template_id
        # 关键字段抽样：提单号/箱型/做箱时间（归一化后）
        first = out.canonical_rows[0]
        assert first["bl_no"] == bl_no
        assert first["box_type_qty"][0]["type"]  # 箱型解析为非空列表
        assert first.get("work_date") or first.get("order_date")  # 日期已补年
        # 行数/单数
        assert len(out.canonical_rows) >= min_rows
        assert len(orders) >= min_orders
        # 必填缺失为空（正常家族数据；首单可能为空提单号 standalone 组，取首个有效单）
        valid = [o for o in orders if o.bl_no]
        assert valid and valid[0].missing_fields == []

    @pytest.mark.parametrize(
        ("family", "fname", "template_id"),
        _OPTIONAL_CASES,
    )
    def test_optional_files_l1_hit(self, family, fname, template_id):
        """可选年份文件：存在即验证 L1 命中（同指纹族级覆盖）。"""
        out, _ = _parse_family(family, fname)
        assert out.template_match.level == "L1"
        assert out.template_match.template_id == template_id
        # 通寰（jinxin_v1，BillRow 语义）行在 rows；其余家族在 canonical_rows
        rows = out.canonical_rows if out.canonical_rows is not None else out.rows
        assert len(rows) > 0


class TestFingerprintMatchesRealHeader:
    """L1 指纹断言以真实文件计算值为准（对必需文件逐个重算验证）。"""

    @pytest.mark.parametrize(
        ("family", "fname", "template_id"),
        [(f, n, t) for f, n, t, *_ in _REQUIRED_CASES],
    )
    def test_real_header_fingerprint(self, family, fname, template_id):
        import xlrd

        from app.orders.bill.parsing.parser import _SheetView, _xls_cell_value, _xls_merged_map

        path = _family_path(family, fname)
        if not path.exists():
            pytest.skip(f"样本未入库：{family}/{fname}")
        wb = xlrd.open_workbook(str(path), formatting_info=True)
        sheet = wb.sheet_by_index(0)
        view = _SheetView(
            sheet.nrows,
            sheet.ncols,
            _xls_merged_map(sheet),
            lambda r, c: _xls_cell_value(sheet, r - 1, c - 1),
        )
        wb.release_resources()
        import app.orders.bill.parsing.template_store as template_store

        match = template_store.identify(view)
        assert match is not None and match.level == "L1"
        assert match.template_id == template_id
        # 配置指纹 == 真实表头行指纹（md5[:8]）
        assert match.fingerprint in match.template["match"]["fingerprints"]


class TestL2Approximate:
    """L2 族级近似：表头逐年漂移（删列）→ 列名集合重合度命中 + missing 记录。"""

    def test_drifted_header_hits_l2_with_missing(self, tmp_path):
        """真实表头删掉一列（漂移）→ L2 命中，缺失列记入 missing。"""
        # 用构造账单：列名集合与 junyu 配置高度重合但指纹不同（缺少 客户编号）
        headers = {
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
        path = tmp_path / "junyu-drifted.xlsx"
        path.write_bytes(
            build_bill_bytes(
                headers,
                [{"A": 1, "B": "客户甲", "E": "OOLU12345678", "D": "40HQ", "F": "TCLU1"}],
            )
        )
        out = parse_bill(path)
        match = out.template_match
        assert match is not None
        assert match.level == "L2"
        assert match.template_id == "junyu_v1"
        assert "客户编号" in match.missing  # 缺失列记录不报错
        assert len(out.canonical_rows) == 1
        assert out.canonical_rows[0]["bl_no"] == "OOLU12345678"


class TestAggregation:
    """归集断言：一票多箱聚合（秋怡）与 bl_no 兜底归集（军羽）。"""

    def test_qiuyi_multi_container_aggregation(self):
        """秋怡同提单号多行 → 一行一票多单（KMTCSHA8365615=4 行 → 4 单各 1 箱）。"""
        _, orders = _parse_family("qiuyi", "2016-01到2016-12上海秋怡应收对账单.xls")
        assert all(len(o.containers) <= 1 for o in orders)  # 一行一票：无跨行箱聚合
        group = [o for o in orders if o.bl_no == "KMTCSHA8365615"]
        assert len(group) == 4
        for o in group:
            assert o.box_groups[0].b_type == "40HQ"
            assert o.box_groups[0].box_num == 1
            assert o.missing_fields == []

    def test_junyu_group_by_bl_no(self):
        """军羽无业务编号：一行一票（462 行 → 462 单，不再按 bl_no 归集）。"""
        out, orders = _parse_family("junyu", "2020-10上海军羽应收对账单.xls")
        assert len(orders) == len(out.canonical_rows)
        group = [o for o in orders if o.bl_no == "SITGSHHPH601607"]
        assert len(group) == 4
        for o in group:
            assert o.box_groups[0].b_type == "20GP" and o.box_groups[0].box_num == 1


class TestTonghuanViaJinxin:
    """通寰（与金科信同源）：jinxin_v1 命中 → BillOrder → to_canonical 转换。"""

    def test_tonghuan_goes_through_jinxin_conversion(self):
        """families/tonghuan 真实文件走 jinxin_v1（L1）→ 转换 CanonicalOrder。"""
        path = _family_path("tonghuan", "2015-01到2015-12上海通寰应收对账单.xls")
        if not path.exists():
            pytest.skip("样本未入库：tonghuan/2015")
        out = parse_bill(path)
        assert out.template_match.template_id == "jinxin_v1"
        assert out.template_match.level == "L1"
        # 既有 BillRow 语义（与金科信 golden 同构）
        assert len(out.rows) > 0
        from app.orders.bill import group_orders, to_canonical

        orders = group_orders(out.rows, out.period).orders
        assert len(orders) == 1090  # 一行一票：单数=数据行数（golden 基线）
        canonical = to_canonical(orders[0])
        assert canonical.bl_no == "SHSB52129400"
        assert canonical.source_template == "jinxin_v1"
        assert canonical.box_groups and canonical.box_groups[0].b_type
        assert canonical.missing_fields == []
