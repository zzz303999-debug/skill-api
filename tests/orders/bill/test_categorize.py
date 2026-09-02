"""四类归集单测（第 0 层）：Excel 解析后业务/财务/基础信息/费用栏目四类数据归集正确。

- 注入最小「混合类别」模板（monkeypatch template_store 缓存，L1 指纹命中），
  构造最小 Excel fixture 走真实解析管线（parse_bill → group_canonical）；
- 业务信息：CanonicalOrder 字段；财务信息：fees 四通道归属（应收→shou/
  应付→pay/车辆成本→cost）；基础信息：collect_candidates 三类候选；
  费用栏目：fee_bootstrap planned（缺失费目码）——四条数据线无遗漏、无错分；
- 财务四类费用字段映射：Excel 列 → FeeItem → build_order_payload form 键断言；
- 边界：空行过滤 / 缺列 missing_fields / 脏数据（提单号前后空格）清洗；
- 端到端 mock 回归：一次 create 完成 基础建档+费目自举+订单创建，二次上传
  全 skipped 且下游零调用（不依赖 golden 样本，CI 可跑）。

模板与费用断言全部使用测试值；建档/下单一律 mock（零网络）。
"""

from __future__ import annotations

from io import BytesIO

import pytest
import yaml
from openpyxl import Workbook

import app.orders.bill.master_data as md_module
import app.orders.bill.master_data_client as md_client_module
import app.orders.bill.template_store as ts_module
import app.orders.http_client as http_client_module
from app.orders.bill import build_result_async, group_canonical, parse_bill
from app.orders.bill.fee_bootstrap import run_fee_bootstrap_async
from app.orders.bill.fee_price_map import apply_price_map
from app.orders.bill.fee_registry import get_registry
from app.orders.bill.master_data import collect_candidates
from app.orders.bill.payload import build_order_payload
from helpers import FakeResponse, inject_price_map

pytestmark = pytest.mark.asyncio

# ---- 最小混合类别模板（单行表头 + fees.ranges 区块分界，仿赢辉家族） ----

# 业务列名（L2 期望列名集合；family_min_overlap 调低以便缺列边界用例仍可命中）
BIZ_HEADERS = ["序号", "客户名称", "门点", "司机", "车牌号", "提单号", "箱型箱量", "司机手机"]
# 费用列与区块分界合计列（ranges：区块 → 末尾合计列名；数据行合计列留空）
FEE_HEADERS = ["运费", "高速费", "待时费", "应收合计", "油费", "应付合计", "打劫费", "车辆成本合计"]


# 注：历史问题——parser._header_column_index 曾对 openpyxl 空单元格（None）
# str(None) 得 "None" 前缀，xlsx 双行模板业务列全部失配（已修复，回归用例见
# TestTwoRowRegression）；主用例仍用单行 + ranges 覆盖四通道归集。


def _mixed_template() -> dict:
    """构造最小混合类别模板配置（注入 template_store 缓存用）。"""
    headers = [*BIZ_HEADERS, *FEE_HEADERS]
    return {
        "template_id": "categorize_v1",
        "family": "categorize",
        "name": "混合类别最小模板（测试）",
        "match": {"fingerprints": [ts_module.compute_fingerprint(headers)], "family_min_overlap": 0.3},
        "header": {"row_anchor": "序号", "two_row": False},
        "data": {"row_filter": "seq_numeric"},
        "required": ["bl_no", "box_type_qty"],
        "columns": {
            "seq": "序号",
            "customer_name": "客户名称",
            "door_point": "门点",
            "driver_name": "司机",
            "plate_no": "车牌号",
            "bl_no": "提单号",
            "box_type_qty": "箱型箱量",
            "driver_phone": "司机手机",
        },
        "normalizers": {"box_type_qty": "box_parse"},
        # 四通道费用：应收→shou / 应付→pay / 车辆成本→cost（合计列分界）；
        # 高速费未映射→to_other
        "fees": {
            "channels": {"应收": "shou", "应付": "pay", "车辆成本": "cost"},
            "ranges": {"应收": "应收合计", "应付": "应付合计", "车辆成本": "车辆成本合计"},
            "mapping": {
                "应收.运费": "freight",
                "应收.待时费": "waiting",
                "应付.油费": "fuel",
                "车辆成本.打劫费": "hijack",
            },
            "unmapped_fee": "to_other",
            "fee_defaults": {"price_type": "1", "is_profit": "1", "dai_dian": "1"},
        },
    }


def build_mixed_bill_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    """最小混合账单：第 2 行列名行（与 helpers.build_bill_bytes 同布局）、第 3 行起数据。"""
    wb = Workbook()
    ws = wb.active
    for col, header in enumerate(headers, start=1):
        ws.cell(row=2, column=col, value=header)
    for r, row in enumerate(rows, start=3):
        for c, value in enumerate(row, start=1):
            if value is not None:
                ws.cell(row=r, column=c, value=value)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


MIXED_HEADERS = [*BIZ_HEADERS, *FEE_HEADERS]


def mixed_rows() -> list[list[object]]:
    """两行同提单号数据（一票两箱 + 四通道费用；运费跨行累加；合计列留空）。"""
    return [
        # 序号 客户  门点  司机  车牌      提单号        箱型    司机手机      运费  高速费 待时费 应收合计 油费 应付合计 打劫费 车辆成本合计
        [1, "客户甲", "门点A", "王师傅", "沪A12345", "OOLU12345678", "40HQ*2", "13800000000", 100.0, 6.0, 50.0, None, 80.0, None, 30.0, None],
        [2, None, None, None, None, "OOLU12345678", "20GP", None, 150.0, None, None, None, None, None, None, None],
    ]


@pytest.fixture()
def mixed_template(monkeypatch):
    """注入最小混合模板到 template_store 缓存（teardown 自动还原真实缓存）。"""
    monkeypatch.setattr(ts_module, "_TEMPLATE_CACHE", {"categorize_v1": _mixed_template()})


@pytest.fixture()
def mixed_bill(tmp_path, mixed_template) -> bytes:
    """最小混合类别 Excel bytes（模板命中路径）。"""
    return build_mixed_bill_bytes(MIXED_HEADERS, mixed_rows())


def parse_mixed(bill_bytes: bytes, tmp_path):
    """混合账单 bytes → (ParseOutput, list[CanonicalOrder])。"""
    path = tmp_path / "mixed.xlsx"
    path.write_bytes(bill_bytes)
    output = parse_bill(path)
    # L1 指纹命中（完整表头）或 L2 族级近似（缺列用例）均走配置驱动解析
    assert output.template_match is not None and output.template_match.level in ("L1", "L2")
    orders = group_canonical(output.canonical_rows, output.template_match.template, output.period)
    return output, orders


class TestFourCategoryAggregation:
    """四类归集整合：一条 Excel 数据线同时产出四类数据，无遗漏、无错分。"""

    async def test_business_information(self, mixed_bill, tmp_path):
        """业务信息：一行一票 → 两行同号各自成单（首行主字段+箱型，次行仅提单号+箱型）。"""
        _, orders = parse_mixed(mixed_bill, tmp_path)
        assert len(orders) == 2
        order = orders[0]
        assert order.bl_no == "OOLU12345678"
        assert [(g.b_type, g.box_num) for g in order.box_groups] == [("40HQ", 2)]
        assert order.customer_name == "客户甲"
        assert order.door_point == "门点A"
        assert order.driver_name == "王师傅"
        assert order.plate_no == "沪A12345"
        assert order.driver_phone == "13800000000"
        assert order.row_count == 1
        # 次行：仅提单号+箱型（其余列空，不继承首行值；客户缺失登记不阻塞）
        second = orders[1]
        assert second.bl_no == "OOLU12345678"
        assert [(g.b_type, g.box_num) for g in second.box_groups] == [("20GP", 1)]
        assert second.row_count == 1
        assert "customer_name" in second.missing_fields

    async def test_multi_plate_cleaned_into_remark(self, mixed_template, tmp_path):
        """多车牌：浮点尾巴清洗（9486.0→9486）按行生效；一行一票无跨行车牌 remark 段。"""
        rows = [
            [1, "客户甲", "门点A", "王师傅", "9486.0", "OOLU12345678", "40HQ*2", "13800000000",
             100.0, 6.0, 50.0, None, 80.0, None, 30.0, None],
            [2, None, None, None, "7399.0", "OOLU12345678", "20GP", None,
             150.0, None, None, None, None, None, None, None],
        ]
        _, orders = parse_mixed(build_mixed_bill_bytes(MIXED_HEADERS, rows), tmp_path)
        assert [o.plate_no for o in orders] == ["9486", "7399"]
        assert all(o.remark is None for o in orders)  # 单行不再拼接「车牌：」段

    async def test_financial_information_channels(self, mixed_bill, tmp_path):
        """财务信息：费用按通道归集（应收→shou/应付→pay/车辆成本→cost），
        一行一票各行独立，未映射费目归并 other。"""
        _, orders = parse_mixed(mixed_bill, tmp_path)
        fees = {(f.channel, f.code): f for f in orders[0].fees}
        # 首行应收：运费 100；待时费 50；高速费未映射 → to_other
        assert fees[("shou", "freight")].money == 100
        assert fees[("shou", "waiting")].money == 50
        assert fees[("shou", "other")].money == 6
        assert fees[("shou", "other")].note == "高速费"  # 原名进 note（payload 拼 ¥金额）
        # 应付：油费
        assert fees[("pay", "fuel")].money == 80
        # 车辆成本：打劫费（与成本共用 cost 通道）
        assert fees[("cost", "hijack")].money == 30
        # 无错分：业务字段不进入费用、费用不进入业务字段
        assert {f.channel for f in orders[0].fees} == {"shou", "pay", "cost"}
        # 次行应收：仅运费 150（不跨行累加）
        second = {(f.channel, f.code): f for f in orders[1].fees}
        assert second[("shou", "freight")].money == 150

    async def test_basic_information_candidates(self, mixed_bill, tmp_path):
        """基础信息：客户/工厂/司机三类候选无遗漏（订单侧字段 → 档案候选）。"""
        _, orders = parse_mixed(mixed_bill, tmp_path)
        cands = collect_candidates(orders)
        by_kind = {c.kind: c for c in cands}
        assert set(by_kind) == {"client", "factory", "driver"}  # 无遗漏、无错分
        assert by_kind["client"].display == "客户甲"
        assert by_kind["factory"].display == "门点A"
        assert by_kind["driver"].display == "王师傅/沪A12345"
        assert by_kind["driver"].phone == "13800000000"
        assert by_kind["driver"].plate == "沪A12345"

    async def test_fee_column_bootstrap_planned(self, mixed_bill, tmp_path, monkeypatch):
        """费用栏目：缺失费目码进自举计划（有 price_id 的费目不重复建）。"""
        inject_price_map(monkeypatch, {"other": None, "waiting": None})
        _, orders = parse_mixed(mixed_bill, tmp_path)
        planned = await run_fee_bootstrap_async(orders, create_order=False)
        assert planned["mode"] == "preview"
        assert planned["created"] == []  # preview 零副作用
        # 待时费/其它费 price_id null → 计划建档（按费用出现序：高速费先于待时费）；
        # 运费/油费/打劫费已有 id → 不建
        assert planned["planned"] == [
            {"code": "other", "tms_name": "其它费"},
            {"code": "waiting", "tms_name": "待时费"},
        ]

    async def test_record_count_conserved(self, mixed_bill, tmp_path):
        """记录数守恒：2 行数据 → 2 票订单（一行一票，row_count=1），四类输出同源。"""
        _, orders = parse_mixed(mixed_bill, tmp_path)
        assert len(orders) == 2 and all(o.row_count == 1 for o in orders)
        assert all(o.bl_no == "OOLU12345678" for o in orders)
        assert orders[0].fees  # 费用归集与业务归集共用同一数据行集合


class TestFieldMapping:
    """字段映射断言（重点：财务四类费用字段 Excel 列 → 接口入参）。"""

    async def test_four_channel_form_mapping(self, mixed_bill, tmp_path, monkeypatch):
        """Excel 费用列 → FeeItem → form 键（shou/pay/cost 通道 + 合计回写）。"""
        # 注入 waiting/other 无 id（2026-09-01 真实表已补值）：模拟自举前状态，
        # 预登记待建费目（模拟自举成功后），保证四通道全部可发射
        inject_price_map(monkeypatch, {"waiting": None, "other": None})
        reg = get_registry()
        reg.register("waiting", 90001, "待时费")
        reg.register("other", 90002, "其它费")
        _, orders = parse_mixed(mixed_bill, tmp_path)
        order = orders[0]
        apply_price_map(order.fees)
        form, _ = build_order_payload(order)
        # 应收 shou：运费 100（本行）/ 待时费 50 / 其它费 6
        assert form["shou[0][运费][money]"] == "100.00"
        assert form["shou[0][运费][price_id]"] == "820"
        assert form["shou[0][待时费][money]"] == "50.00"
        assert form["shou[0][待时费][price_id]"] == "90001"
        assert form["shou[0][其它费][money]"] == "6.00"
        assert form["shou[0][其它费][price_id]"] == "90002"
        # 应付 pay / 车辆成本 cost（含费目条目 driver_name 恒发键）
        assert form["pay[0][油费][money]"] == "80.00"
        assert form["pay[0][油费][price_id]"] == "1134"
        assert form["cost[0][打劫费][money]"] == "30.00"
        assert form["cost[0][打劫费][price_id]"] == "1135"
        assert form["cost[0][打劫费][driver_name]"] == ""
        # 通道级 note 恒发（to_other 原名 + ¥金额；无 other 项通道发空串）
        assert form["shou[0][note]"] == "高速费 ¥6.00"
        assert form["pay[0][note]"] == ""
        assert form["cost[0][note]"] == ""
        # 合计回写：driver[0] 应收/应付合计 + cost[0] 成本合计（首行 100+50+6）
        assert form["driver[0][get_ys_zj]"] == "156.00"
        assert form["driver[0][pay_yf_zj]"] == "80.00"
        assert form["cost[0][supplier_hj_zj]"] == "30.00"
        # 业务字段同步落点（合并提交同一 form）
        assert form["data[0][b_order_num]"] == "OOLU12345678"
        assert form["c_title"] == "客户甲"
        assert form["box[0][b_type]"] == "40HQ" and form["box[0][box_num]"] == "2"

    async def test_fee_defaults_and_price_type(self, mixed_bill, tmp_path):
        """费目条目六属性键：price_type/is_profit/dai_dian 取模板 fee_defaults。"""
        reg = get_registry()
        reg.register("waiting", 90001, "待时费")
        reg.register("other", 90002, "其它费")
        _, orders = parse_mixed(mixed_bill, tmp_path)
        apply_price_map(orders[0].fees)
        form, _ = build_order_payload(orders[0])
        prefix = "shou[0][运费]"
        assert form[f"{prefix}[price_type]"] == "1"
        assert form[f"{prefix}[is_profit]"] == "1"
        assert form[f"{prefix}[dai_dian]"] == "1"


class TestBoundaries:
    """边界：空行 / 缺列 / 脏数据（提单号前后空格）。"""

    async def test_empty_row_filtered(self, tmp_path, mixed_template):
        """全空行不产生数据记录（数据行数守恒）。"""
        rows = mixed_rows()
        rows.insert(1, [None] * len(MIXED_HEADERS))  # 两数据行之间插全空行
        bill = build_mixed_bill_bytes(MIXED_HEADERS, rows)
        _, orders = parse_mixed(bill, tmp_path)
        assert len(orders) == 2 and all(o.row_count == 1 for o in orders)

    async def test_missing_columns_flagged(self, tmp_path, mixed_template):
        """缺列（客户名称/箱型箱量/司机手机缺失）→ missing_fields 登记，不阻塞。"""
        headers = ["序号", "门点", "司机", "车牌号", "提单号", *FEE_HEADERS]
        rows = [
            [1, "门点A", "王师傅", "沪A12345", "OOLU12345678", 100.0, 6.0, 50.0, None, 80.0, None, 30.0, None],
        ]
        bill = build_mixed_bill_bytes(headers, rows)
        _, orders = parse_mixed(bill, tmp_path)
        order = orders[0]
        assert "box_groups" in order.missing_fields
        assert "customer_name" in order.missing_fields
        assert order.bl_no == "OOLU12345678"  # 提单号不受影响
        assert any(f.code == "freight" for f in order.fees)  # 费用列不受缺业务列影响

    async def test_dirty_bl_no_cleaned(self, tmp_path, mixed_template):
        """脏数据：提单号前后空格 → 清洗后同号成单（一行一票两单，清洗不丢单）。"""
        rows = mixed_rows()
        rows[0][5] = "  OOLU12345678  "
        bill = build_mixed_bill_bytes(MIXED_HEADERS, rows)
        _, orders = parse_mixed(bill, tmp_path)
        assert len(orders) == 2
        assert all(o.bl_no == "OOLU12345678" for o in orders)


# ---- two_row 双行表头回归（openpyxl 空区块单元格不得产生「None.列名」前缀） ----

# 费用区块宽度（与 FEE_HEADERS 顺序对应）：应收 4 列（…应收合计）、应付 2 列（…应付合计）、
# 车辆成本 2 列（…车辆成本合计）
_FEE_SECTION_WIDTHS = [("应收", 4), ("应付", 2), ("车辆成本", 2)]


def _two_row_template() -> dict:
    """two_row 模板：区块行（第 1 行）业务区留空、费用区合并写区块名。

    与单行模板同业务列/费目配置，仅表头布局不同（two_row + fee_boundary:
    section_header + channels/mapping 双行匹配）；指纹取列名行（第 2 行）。
    """
    tpl = _mixed_template()
    tpl["template_id"] = "categorize_two_row_v1"
    tpl["name"] = "混合类别双行表头最小模板（测试）"
    tpl["match"] = {
        "fingerprints": [ts_module.compute_fingerprint(MIXED_HEADERS)],
        "family_min_overlap": 0.3,
    }
    tpl["header"] = {"row_anchor": "序号", "two_row": True, "section_fill": "forward"}
    tpl["fee_boundary"] = "section_header"
    tpl["fees"] = {
        "channels": {"应收": "shou", "应付": "pay", "车辆成本": "cost"},
        "mapping": {
            "应收.运费": "freight",
            "应收.待时费": "waiting",
            "应付.油费": "fuel",
            "车辆成本.打劫费": "hijack",
        },
        "unmapped_fee": "to_other",
        "fee_defaults": {"price_type": "1", "is_profit": "1", "dai_dian": "1"},
    }
    return tpl


@pytest.fixture()
def two_row_template(monkeypatch):
    """注入 two_row 模板（区块行业务区留空、费用区合并区块名）。"""
    monkeypatch.setattr(
        ts_module, "_TEMPLATE_CACHE", {"categorize_two_row_v1": _two_row_template()}
    )


def build_two_row_bill_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    """双行表头账单：第 1 行区块行（业务区单元格留空 None、费用区合并写区块名）、
    第 2 行列名行、第 3 行起数据。

    业务区留空单元格 openpyxl 读为 None——回归 two_row 解析（修复前 str(None)
    得 "None" 使业务列全部失配）。
    """
    wb = Workbook()
    ws = wb.active
    col = len(BIZ_HEADERS) + 1
    for section, width in _FEE_SECTION_WIDTHS:
        ws.merge_cells(
            start_row=1, start_column=col, end_row=1, end_column=col + width - 1
        )
        ws.cell(row=1, column=col, value=section)
        col += width
    for c, header in enumerate(headers, start=1):
        ws.cell(row=2, column=c, value=header)
    for r, row in enumerate(rows, start=3):
        for c, value in enumerate(row, start=1):
            if value is not None:
                ws.cell(row=r, column=c, value=value)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestTwoRowRegression:
    """two_row 双行表头回归：openpyxl 空区块单元格（None）不得产生「None.列名」。

    回归对象：parser._header_column_index 对空单元格的 str(None) 处理（xlsx 特有，
    xlrd 空文本为 '' 不受影响）；同时覆盖费用区合并区块名的双行匹配路径。
    """

    async def test_business_columns_mapped(self, tmp_path, two_row_template):
        """业务区空区块单元格 → 业务列正常映射（修复前「None.客户名称」全失配）。"""
        bill = build_two_row_bill_bytes(MIXED_HEADERS, mixed_rows())
        output, orders = parse_mixed(bill, tmp_path)
        assert output.template_match.level == "L1"  # 列名行指纹命中（区块行不参与指纹）
        assert len(orders) == 2
        order = orders[0]
        assert order.bl_no == "OOLU12345678"
        assert order.customer_name == "客户甲"
        assert order.door_point == "门点A"
        assert order.driver_name == "王师傅"
        assert order.plate_no == "沪A12345"
        assert order.driver_phone == "13800000000"
        assert [(g.b_type, g.box_num) for g in order.box_groups] == [("40HQ", 2)]
        assert order.row_count == 1

    async def test_fee_channels_via_merged_sections(self, tmp_path, two_row_template):
        """费用区合并区块名 → 四通道费用正确归属（与单行模板同口径，各行独立）。"""
        bill = build_two_row_bill_bytes(MIXED_HEADERS, mixed_rows())
        _, orders = parse_mixed(bill, tmp_path)
        fees = {(f.channel, f.code): f for f in orders[0].fees}
        assert fees[("shou", "freight")].money == 100
        assert fees[("shou", "waiting")].money == 50
        assert fees[("shou", "other")].money == 6
        assert fees[("pay", "fuel")].money == 80
        assert fees[("cost", "hijack")].money == 30
        assert {f.channel for f in orders[0].fees} == {"shou", "pay", "cost"}
        # 次行应收：仅运费 150（不跨行累加）
        second_fees = {(f.channel, f.code): f for f in orders[1].fees}
        assert second_fees[("shou", "freight")].money == 150

    async def test_no_none_unmatched_headers(self, tmp_path, two_row_template):
        """未识别表头告警不含 "None" 假列（空区块/空表头单元格不再上报）。"""
        bill = build_two_row_bill_bytes(MIXED_HEADERS, mixed_rows())
        output, _ = parse_mixed(bill, tmp_path)
        assert all("none" not in h.lower() for h in output.unmatched_headers)
        # 表头行含空单元格（未映射列无表头名）→ 数据行有值也不得报 "None" 假列
        headers = [*BIZ_HEADERS[:3], None, *BIZ_HEADERS[4:], *FEE_HEADERS]
        rows = [
            # 司机列（表头为空）数据有值，修复前会以 "None" 假列上报告警
            [1, "客户乙", "门点B", "王师傅2", "沪B54321", "OOLU87654321", "40HQ*1", "13900000000", 100.0, None, None, None, None, None, None, None],
        ]
        bill = build_two_row_bill_bytes(headers, rows)
        output, orders = parse_mixed(bill, tmp_path)
        assert all("none" not in h.lower() for h in output.unmatched_headers)
        assert orders[0].bl_no == "OOLU87654321"  # 空表头列不影响其余业务列


# ---- 端到端 mock 回归（基础建档 → 费用栏目 → 订单创建 → 二次上传去重） ----


def _md_config_dict() -> dict:
    """注入用 master_data 配置（threshold=1 强制当批建档，端点为测试值）。"""
    return {
        "enabled": True,
        "threshold": 1,
        "sn_prefix": {"client": "CLT", "factory": "FAC", "truck": "TRK", "driver": "DRV"},
        "endpoints": {
            "client_create": "http://jxt.test/Create/Client",
            "factory_create": "http://jxt.test/Create/Factory",
            "bailor_create": "TODO",
            "truck_create": "http://jxt.test/Create/Truck",
            "driver_create": "http://jxt.test/Create/Driver",
            "price_create": "http://jxt.test/Create/Price",
        },
        "defaults": {
            "client": {"cg_id": "4", "cg_name": "同行", "sys_type": "1", "su_id": "15478"},
            "truck": {"section_id": "6987", "remind_id": "15478", "sys_type": "1", "type": "bill"},
            "driver": {"sinout": "自做", "sys_type": "1"},
            "factory": {"sys_type": "1"},
        },
    }


@pytest.fixture()
def md_config(tmp_path, monkeypatch):
    """注入 master_data 配置（临时文件 + 缓存重置）；teardown 恢复真实配置缓存。"""
    real_path = md_module._CONFIG_PATH
    path = tmp_path / "master_data.yaml"
    path.write_text(
        yaml.safe_dump({"master_data": _md_config_dict()}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(md_module, "_CONFIG_PATH", path)
    md_module.reload_config()
    yield
    md_module._CONFIG_PATH = real_path
    md_module.reload_config()


class TestE2EMock:
    """全流程 mock 端到端（CI 可跑，无 golden 依赖）：一次 create 触发四类链路。"""

    async def test_full_flow_and_second_upload_dedup(
        self, mixed_bill, tmp_path, md_config, monkeypatch, _no_real_archive_calls
    ):
        # 恢复真实 create_archives（conftest 全局 mock 是零网络兜底），httpx 层统一 mock
        monkeypatch.setattr(md_client_module, "create_archives_async", _no_real_archive_calls)
        # 注入 waiting/other 无 id：触发费目自举建档（2026-09-01 真实表已补值）
        inject_price_map(monkeypatch, {"waiting": None, "other": None})
        state = {"addwork": 0, "price": 0, "archives": []}
        price_ids = iter([88801, 88802])
        addwork_forms: list[dict] = []

        async def fake_post(url, *, payload=None, headers=None, name=None, payload_kind=None, timeout=None, **_kwargs):
            if "/Create/Price" in url:
                state["price"] += 1
                state["archives"].append(("price", payload))
                return FakeResponse({"code": "200", "msg": "添加成功", "data": {"price_id": next(price_ids)}})
            if "/Create/Client" in url:
                state["archives"].append(("client", payload))
                return FakeResponse({"code": "200", "msg": "添加成功", "data": {"client_id": "c1"}})
            if "/Create/Factory" in url:
                state["archives"].append(("factory", payload))
                return FakeResponse({"code": "200", "msg": "添加成功", "data": {"factory_id": "f1"}})
            if "/Create/Truck" in url:
                state["archives"].append(("truck", payload))
                return FakeResponse({"code": "200", "msg": "添加成功", "data": {"truck_id": "t1"}})
            if "/Create/Driver" in url:
                state["archives"].append(("driver", payload))
                return FakeResponse({"code": "200", "msg": "添加成功", "data": {"id": "d1"}})
            state["addwork"] += 1
            addwork_forms.append(payload)
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)

        # 第一次上传：四类链路全部触发（一行一票：两行 → 两单）。
        # 同号无箱号行按行序号兜底成键（#1/#2），两行各自录入（2026-08-31 拍板）
        result = await build_result_async(filename="mixed.xlsx", file_bytes=mixed_bill, create_order=True, sk="sk")
        assert result.summary == {
            "total": 2,
            "success": 2,
            "failed": 0,
            "skipped": 0,
            "created": 2,
            "success_sns": ["EX1", "EX1"],
            "failed_details": [],
        }
        # 基础建档（依赖序：客户 → 工厂 → 车辆 → 司机）
        archived = [a["kind"] for a in result.meta["master_data"]["archived"]]
        assert archived == ["client", "factory", "truck", "driver"]
        assert state["archives"] and all(f[0] == "price" for f in state["archives"][:2])
        # 费用栏目自举：other/waiting 建档成功并回填 price_id（按缺失出现序）
        bootstrap = result.meta["reconciliation"]["reports"]["fee_bootstrap"]
        assert bootstrap["created"] == [
            {"code": "other", "tms_name": "其它费", "price_id": 88801},
            {"code": "waiting", "tms_name": "待时费", "price_id": 88802},
        ]
        # 订单创建：AddWork 每单恰 1 次（行序号兜底键，两行各自录入），
        # 请求体含业务 + 四通道费用（首行）
        assert state["addwork"] == 2
        form = addwork_forms[0]
        assert form["data[0][b_order_num]"] == "OOLU12345678"
        assert form["shou[0][运费][money]"] == "100.00"
        assert form["shou[0][待时费][price_id]"] == "88802"
        assert form["shou[0][其它费][price_id]"] == "88801"
        assert form["pay[0][油费][money]"] == "80.00"
        assert form["cost[0][打劫费][money]"] == "30.00"
        assert form["driver[0][get_ys_zj]"] == "156.00"
        # 建档请求体：客户/工厂（依赖前置 client_id）/车辆/司机（带 truck_id）
        archive_kinds = [a[0] for a in state["archives"]]
        assert archive_kinds == ["price", "price", "client", "factory", "truck", "driver"]
        client_form = next(f for k, f in state["archives"] if k == "client")
        assert client_form["client_name"] == "客户甲"
        assert client_form["cg_id"] == "4"
        factory_form = next(f for k, f in state["archives"] if k == "factory")
        assert factory_form["client_id"] == "c1"
        driver_form = next(f for k, f in state["archives"] if k == "driver")
        assert driver_form["truck_id"] == "t1"

        # 同一文件二次上传：全 skipped（下游 0 次新增调用、无重复建档）
        from app.orders.bill.master_data_store import get_store

        counts_after_first = dict(get_store().snapshot())
        second = await build_result_async(filename="mixed.xlsx", file_bytes=mixed_bill, create_order=True, sk="sk")
        assert second.summary == {
            "total": 2,
            "success": 2,
            "failed": 0,
            "skipped": 2,
            "created": 0,
            "success_sns": ["EX1", "EX1"],
            "failed_details": [],
        }
        assert state["addwork"] == 2  # 不重复下单
        assert len(state["archives"]) == 6  # 不重复建档（自举/基础档案均零新增）
        assert get_store().snapshot() == counts_after_first  # 计数不被重导推高
