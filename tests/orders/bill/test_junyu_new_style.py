"""junyu_v1 新式样回归（2026-09-03 生产事故修复）。

事故：新式样账单（19 列）在费用区插入「箱量/手机号/车牌」业务列——费用列自动
发现（column_range：columns 未映射列即费用列）误收手机号列（整列 13245694123
数值存储）与箱量列 → 每单其它费 13 亿脏费用入库；且车牌/手机号缺失导致司机
建档 skip_archive（TMS AddCarDriver 必填 num/phone）。

修复（junyu_v1.yaml）：columns 补 driver_phone: 手机号、plate_no: 车牌 +
normalizers.driver_phone: strip_float_tail（去数值尾巴）+ fees.ignore_headers
排除「箱量」冗余列（parser 费用发现支持模板级 ignore）。

本文件用构造式账单（与事故文件同列布局）零 golden 依赖回归；老式样由既有
golden/families 用例覆盖（全量回归验证无回归）。
"""

from __future__ import annotations

import app.orders.bill.template_store as store
from app.orders.bill import group_canonical, parse_bill
from helpers import build_bill_bytes

# 事故文件（2016 军羽全年 (2).xls）19 列新式样表头
NEW_STYLE_HEADERS = {
    "A": "序号",
    "B": "客户名称",
    "C": "客户编号",
    "D": "门点",
    "E": "箱型箱量",
    "F": "箱量",
    "G": "提单号",
    "H": "箱号",
    "I": "做箱时间",
    "J": "港区",
    "K": "司机",
    "L": "手机号",
    "M": "车牌",
    "N": "运费",
    "O": "打/改单费",
    "P": "其它费",
    "Q": "税金",
    "R": "小计",
    "S": "应收备注",
}
NEW_STYLE_ROWS = [
    {
        "A": 1, "B": "钜腾", "D": "星源航天", "E": "40HQ", "F": 1,
        "G": "OOLU12345678", "H": "TCLU4000001", "J": "洋山", "K": "朱伯成",
        "L": 13245694123, "M": "沪AS12345", "N": 3300, "P": 3700, "R": 7000,
    }
]


def _parse_new_style(tmp_path):
    store.reload_templates()  # 模板 yaml 改动后重载（对齐既有模板用例惯例）
    path = tmp_path / "junyu-new-style.xlsx"
    path.write_bytes(build_bill_bytes(NEW_STYLE_HEADERS, NEW_STYLE_ROWS))
    out = parse_bill(path)
    canon = group_canonical(out.canonical_rows, out.template_match.template, out.period)
    return canon[0]


def test_new_style_fees_clean_no_phone_or_qty_leak(tmp_path):
    """新式样：手机号/箱量列不再进费用——其它费仅含真其它费（事故回归）。"""
    order = _parse_new_style(tmp_path)
    fees = {f.code: float(f.money or 0) for f in (order.fees or [])}
    assert fees.get("freight") == 3300.0
    assert fees.get("other") == 3700.0
    assert all(float(f.money or 0) < 1e7 for f in (order.fees or [])), "不得再出现手机号量级脏费用"


def test_new_style_driver_fields_mapped(tmp_path):
    """新式样：手机号/车牌进字段（司机建档依赖）；手机号数值去浮点尾巴。"""
    order = _parse_new_style(tmp_path)
    assert order.driver_name == "朱伯成"
    assert order.driver_phone == "13245694123"  # strip_float_tail：不带 .0
    assert order.plate_no == "沪AS12345"


def test_ignore_headers_in_fee_discovery(tmp_path):
    """模板级 ignore_headers 排除生效：去掉声明后箱量列会被费用发现（机制对照）。"""
    store.reload_templates()
    # 读模板内嵌字典直接调用费用发现（私有函数同包测试惯例），验证 ignore 集生效
    from app.orders.bill.columns import discover_fee_columns

    names = ["序号", "箱量", "运费", "其它费", "应收备注"]
    fees_cfg = {"channels": {"应收": "shou"}, "ignore_headers": ["箱量"]}
    fee_cols, _anchors = discover_fee_columns(
        fees_cfg, names, [], {}, row_anchor="序号"
    )
    # 箱量被排除；运费/其它费/备注列不被误列（备注关键字排除）——断言排除后的列集
    assert {names[c - 1] for c in fee_cols} == {"运费", "其它费"}

    # 对照：无 ignore_headers 声明 → 箱量列会被自动发现为费用列（证明机制必要性）
    fee_cols2, _ = discover_fee_columns(
        {"channels": {"应收": "shou"}}, names, [], {}, row_anchor="序号"
    )
    assert {names[c - 1] for c in fee_cols2} == {"箱量", "运费", "其它费"}
