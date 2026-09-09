"""竞品账单「一行一票」旧链路（2026-08-31 业务拍板）：每条数据行独立为 BillOrder。

只做归集，不组装响应：行序直转 + 账单锚点对账（合计行/合计大写/总箱型箱量，
只报告不拦截，见 _collect_anchors/_build_reconciliation）。标准字段链路已拆至
canonical_aggregator.py；清洗口径经本模块公开函数共享（两通道一致）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..schema import (
    MISSING_BOX,
    MISSING_C_TITLE,
    MISSING_ORDER_NUM1,
    REASON_INVALID_FORMAT,
    REASON_NOT_FOUND,
    BillOrder,
    BillPeriod,
    BillRow,
)
from .cn_amount import parse_cn_upper_amount

# 数据行序号：纯数字（真实账单为 "1.0" 形式）
_SEQ_RE = re.compile(r"^\d+(\.\d+)?$")
# 提单号：清洗后 ≥8 位字母数字
_BL_NO_RE = re.compile(r"^[A-Za-z0-9]{8,}$")
# 浮点尾巴：纯数字 + .0+（xlrd/openpyxl 数字单元格）
_FLOAT_TAIL_RE = re.compile(r"^\d+\.0+$")
# 箱型：不做格式校验，非空即合法（2026-08-12 业务确认：真实账单含大量
# 非标表述，如 45HQ/大冷/飞翼车/2X20/12T/拼箱 等，均须正常归集）
# 月-日日期（账单内通常只有月-日，如 "9-1"）
_MONTH_DAY_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
# 箱型×数量锚点（账单「总箱型箱量」行）；
# 第一组捕获完整箱型（两位数字前缀 + 大写字母后缀），第二组为数量
_BOX_ANCHOR_RE = re.compile(r"(\d{2}[A-Z]+)\*(\d+)")


@dataclass
class AggregationOutput:
    """归集结果：订单列表 + 账单锚点对账信息（reconciliation，只报告不拦截）。"""

    orders: list[BillOrder]
    reconciliation: dict = field(default_factory=dict)


def _row_text(row: BillRow) -> str:
    """整行原文拼串（尾部锚点行探测用）。"""
    return " ".join(
        str(v) for v in row.model_dump().values()
        if v is not None and str(v).strip()
    )


def _collect_anchors(
    rows: list[BillRow],
) -> tuple[
    list[BillRow],
    dict[str, float] | None,
    dict[str, int] | None,
    float | None,
]:
    """过滤数据行并顺手收集对账锚点（不额外遍历；同类型锚点只取首个）。

    返回 (数据行, 费用总额锚点, 箱型数量锚点, 合计大写金额锚点)：seq 以
    「合计:」开头的行取各费用列总额；含「总箱型箱量」的行从整行原文解析
    箱型×数量；整行文本含「合计大写」的行解析大写金额（导出写死不经公式，
    第二锚点防 SUM 公式缓存旧值）；其余尾部行丢弃。
    """
    data_rows: list[BillRow] = []
    fee_total: dict[str, float] | None = None
    box_total: dict[str, int] | None = None
    uppercase_total: float | None = None
    for row in rows:
        seq = (row.seq or "").strip()
        if _SEQ_RE.match(seq) or (row.order_num1 or "").strip():
            data_rows.append(row)
            continue
        if seq.startswith("合计:") and fee_total is None:
            fee_total = {
                name: amount
                for name, amount in row.fees.items()
                if isinstance(amount, (int, float))
            }
            continue
        if "总箱型箱量" in seq and box_total is None:
            matches = _BOX_ANCHOR_RE.findall(_row_text(row))
            if matches:
                box_total = {box_type.upper(): int(count) for box_type, count in matches}
            continue
        if uppercase_total is None:
            row_text = _row_text(row)
            if "合计大写" in row_text:
                uppercase_total = parse_cn_upper_amount(row_text)
    return data_rows, fee_total, box_total, uppercase_total


def _build_reconciliation(
    orders: list[BillOrder],
    fee_total: dict[str, float] | None,
    box_total: dict[str, int] | None,
    uppercase_total: float | None,
) -> dict:
    """用账单锚点校验归集结果（只报告不拦截）。

    费用双锚点：合计行（fees 明细留痕，缓存值键名自解释）+ 合计大写金额
    （导出时写死不经公式）；任一锚点吻合 → fees_status=matched，都在且都不吻合
    → mismatch，都缺 → no_anchor。
    箱型 diff = bill_total - aggregated。
    status 汇总：任一 mismatch → mismatch；无 mismatch 且至少一个 matched → matched；
    全 no_anchor → no_anchor。
    """
    aggregated_fees: dict[str, float] = {}
    aggregated_boxes: dict[str, int] = {}
    for order in orders:
        for entry in order.order_data.get("shou", []):
            for name, spec in entry.items():
                aggregated_fees[name] = aggregated_fees.get(name, 0.0) + spec["money"]
        for box in order.order_data["box"]:
            btype = box["b_type"]
            aggregated_boxes[btype] = aggregated_boxes.get(btype, 0) + box["box_num"]

    # 费用对账：合计行口径留痕（缓存值命名自解释）；合计大写作为第二锚点（容差 0.01）
    fees: dict | None = None
    row_ok = False
    if fee_total:
        fees = {}
        row_ok = True
        for name, bill_amount in fee_total.items():
            aggregated = round(aggregated_fees.get(name, 0.0), 2)
            diff = round(aggregated - bill_amount, 2)
            fees[name] = {
                "aggregated": aggregated,
                "bill_total_cached": bill_amount,
                "bill_total_note": "合计行 SUM 公式缓存旧值（导出未重算），仅供留痕，不参与判定",
                "diff": diff,
            }
            if abs(diff) > 0.01:
                row_ok = False
    elif aggregated_fees:
        # 无合计行：按归集费用逐项留痕，锚点值为 null 并注明原因
        fees = {
            name: {
                "aggregated": round(amount, 2),
                "bill_total_cached": None,
                "bill_total_note": "账单无合计行",
                "diff": None,
            }
            for name, amount in aggregated_fees.items()
        }
    uppercase_entry: dict | None = None
    upper_ok = False
    if uppercase_total is not None:
        aggregated_total = round(sum(aggregated_fees.values()), 2)
        uppercase_entry = {
            "aggregated": aggregated_total,
            "bill_total": uppercase_total,
            "diff": round(aggregated_total - uppercase_total, 2),
        }
        upper_ok = abs(uppercase_entry["diff"]) <= 0.01
    if fee_total is None and uppercase_total is None:
        fees_status = "no_anchor"
    else:
        fees_status = "matched" if (row_ok or upper_ok) else "mismatch"

    # 箱型对账：逻辑不变
    boxes: dict | None = None
    box_ok = False
    if box_total:
        boxes = {}
        box_ok = True
        for btype, bill_count in box_total.items():
            aggregated = aggregated_boxes.get(btype, 0)
            diff = bill_count - aggregated
            boxes[btype] = {
                "aggregated": aggregated,
                "bill_total": bill_count,
                "diff": diff,
            }
            if diff != 0:
                box_ok = False
        boxes_status = "matched" if box_ok else "mismatch"
    else:
        boxes_status = "no_anchor"

    # 状态汇总 + note
    if fees_status == "mismatch" or boxes_status == "mismatch":
        status = "mismatch"
    elif fees_status == "matched" or boxes_status == "matched":
        status = "matched"
    else:
        status = "no_anchor"
    note = None
    if fee_total and uppercase_total is not None and not row_ok and upper_ok:
        note = "合计行疑似公式缓存旧值（导出未重算），大写金额与归集一致，归集结果可信"

    return {
        "status": status,
        "fees_status": fees_status,
        "boxes_status": boxes_status,
        "fees": fees,
        "fees_uppercase_total": uppercase_entry,
        "boxes": boxes,
        "note": note,
    }


def clean_order_num(raw: str | None) -> tuple[str | None, str | None]:
    """提单号清洗：去首尾/内部空格与连字符、去浮点 .0 尾巴；返回 (清洗值, None) 或 (None, 原因)。

    清洗后须 ≥8 位字母数字且非纯字母；原文为空 → REASON_NOT_FOUND；
    清洗后非法 → REASON_INVALID_FORMAT。
    """
    if raw is None or not raw.strip():
        return None, REASON_NOT_FOUND
    cleaned = raw.strip().replace(" ", "").replace("-", "")
    if _FLOAT_TAIL_RE.match(cleaned):
        cleaned = cleaned.split(".")[0]
    if _BL_NO_RE.match(cleaned) and not cleaned.isalpha():
        return cleaned, None
    return None, REASON_INVALID_FORMAT


def _nonempty(value) -> str | None:
    """非空字段值（None/空白 → None；一行一票后单行直取，无组内遍历）。"""
    if value is not None and str(value).strip():
        return value
    return None


def _text_values(row: BillRow, *attrs: str) -> dict[str, str]:
    """非空文本字段收集（None/空白省略；键序 = attrs 顺序）。"""
    return {
        a: v
        for a in attrs
        if (v := _nonempty(getattr(row, a)))
    }


def _strip_float_tail(text: str) -> str:
    """Excel 数字单元格浮点尾巴（9486.0 → 9486）。"""
    return text.split(".")[0] if _FLOAT_TAIL_RE.match(text) else text


def clean_plate_no(value) -> str | None:
    """车牌清洗：去浮点尾巴（9486.0 → 9486），其余原样（公开导出，两通道同口径）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _strip_float_tail(text)


def clean_group_key(value) -> str | None:
    """归集键清洗：去浮点尾巴（提单号/业务编号被 Excel 存成数字）（公开导出，两通道同口径）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _strip_float_tail(text)


def _box_entries(row: BillRow) -> tuple[list[dict], bool]:
    """box 归集（一行一票，一行一箱型）：非空即一条 box_num=1；返回 (条目, 是否有箱型原文)。

    箱型不做格式校验（业务确认 2026-08-12）：非空即合法，含标准箱型与
    大冷/飞翼车/2X20/拼箱 等非标表述；无原文才视为缺失。
    """
    raw = (row.b_type or "").strip().upper()
    if not raw:
        return [], False
    return [{"b_type": raw, "box_num": 1}], True


def _pick_month_day(row: BillRow) -> str | None:
    """b_date 候选：做箱时间列优先，日期列回退
    「日期」列多为结算/对账日，做箱时间须以「做箱时间」列值为准）。"""
    for value in (row.b_date_time, row.b_date):
        if value and _MONTH_DAY_RE.match(value.strip()):
            return value.strip()
    return None


def _fill_year(md: str, period: BillPeriod) -> str | None:
    """月-日补年份：取使完整日期落在结算区间内的年份（跨年取最早满足的年份）。

    规则见接口文档 §2.4：如区间 2018-01~2019-12 时 "12-7"→2018-12-07、"1-5"→2019-01-05；
    period 缺失或无法落在区间内 → None（b_date 非必填，不进 missing_fields）。
    """
    match = _MONTH_DAY_RE.match(md)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if period is None or not period.start or not period.end:
        return None
    try:
        start = date.fromisoformat(period.start)
        end = date.fromisoformat(period.end)
    except ValueError:
        return None
    for year in range(start.year, end.year + 1):
        try:
            full = date(year, month, day)
        except ValueError:
            continue
        if start <= full <= end:
            return full.isoformat()
    return None


def _fee_entries(row: BillRow) -> tuple[list[dict], float]:
    """行级费用条目：按行 fees 键序遍历，金额为数字才建条目；合计入 get_ys_zj。

    费用项以模板声明为源（2026-09-04 动态化，不再按代码常量遍历）；同名
    费用跨行累加由一行一票（每行独立成单）消解。
    """
    entries: list[dict] = []
    total = 0.0
    for name, amount in row.fees.items():
        if isinstance(amount, (int, float)):
            amount = round(amount, 2)
            entries.append({name: {"money": amount}})
            total += amount
    return entries, round(total, 2)


def _build_c_note(row: BillRow) -> str | None:
    """c_note 拼接（§5.1 顺序）：客户编号→业务类型→箱号→车队→备注→应付备注，
    各段非空才拼、段间 "；" 分隔；备注/应付备注直拼无前缀。

    一行一票后单行至多一车牌/一日期，历史「多车牌并入备注」段已随多行合并
    移除（防丢失口径 2026-08-26 由去重键提单号+箱号承接）。
    """
    segments: list[str] = []
    if v := _nonempty(row.c_sn):
        segments.append("客户编号：" + v)
    if v := _nonempty(row.biz_type):
        segments.append("业务类型：" + v)
    if v := _nonempty(row.container_no):
        segments.append("箱号：" + v)
    if v := _nonempty(row.fleet):
        segments.append("车队：" + v)
    if v := _nonempty(row.remark):
        segments.append(v)
    if v := _nonempty(row.payable_remark):
        segments.append(v)
    return "；".join(segments) if segments else None


def _order_from_row(row: BillRow, period: BillPeriod) -> BillOrder:
    """单行 → BillOrder（一行一票，2026-08-31 业务拍板；每行独立成单）。"""
    bl_no, bl_reason = clean_order_num(row.order_num1)

    # 非必填文本段统一收集（None/空白省略；下方按段挑选，键序 = 既有响应键序）
    texts = _text_values(
        row,
        "c_title", "c_name", "c_phone", "c_sn", "factory_name",
        "factory_bei", "b_wharf", "b_get_address", "b_back_address",
        "d_name", "d_phone",
    )
    c_title = texts.get("c_title")  # 必填判空（missing_fields/BillOrder 共用）
    d_num = clean_plate_no(_nonempty(row.d_num))
    # 箱号取本行原文（去空白），行序号清洗浮点尾巴——二者供去重组合键使用
    raw_container = _nonempty(row.container_no)
    container_no = raw_container.strip() if raw_container else None
    row_seq = clean_group_key(row.seq)

    # box：非空即一条（不做格式校验）；无箱型原文才视为缺失
    box_entries, has_valid_box = _box_entries(row)

    # b_date：做箱时间列优先、日期列回退（2026-09-03 用户拍板），月-日补年份
    md = _pick_month_day(row)
    b_date = _fill_year(md, period) if md else None
    month = b_date[:7] if b_date else None

    # 费用：行级金额为一条 shou，合计入 get_ys_zj
    shou, get_ys_zj = _fee_entries(row)

    c_note = _build_c_note(row)

    # 必填缺失标记：order_num1 / c_title / box
    missing_fields: list[str] = []
    missing_reasons: dict[str, str] = {}
    if bl_no is None:
        missing_fields.append(MISSING_ORDER_NUM1)
        missing_reasons[MISSING_ORDER_NUM1] = bl_reason
    if c_title is None:
        missing_fields.append(MISSING_C_TITLE)
        missing_reasons[MISSING_C_TITLE] = REASON_NOT_FOUND
    if not has_valid_box:
        missing_fields.append(MISSING_BOX)
        missing_reasons[MISSING_BOX] = REASON_NOT_FOUND

    # order_data：必填缺失显式置 None/[]，不填空串/0；非必填空值省略键；
    # data 恒为 1 条货物明细（2026-08-26 实测修正，原因见 data 键注释）
    order_data: dict = {
        "order_num1": bl_no,
        "type": 1,
        "c_title": c_title,
        # data 收敛 1 条（2026-08-26 实测修正）：TMS 按 data 条数展开明细并把
        # 费用重复计入（N 条同 b_order_num → 费用总额翻倍）；对齐标准通道
        # data[0] 语义，柜级信息由 box[]/driver[0] 承载。
        "data": [{"b_order_num": bl_no}],
        "box": box_entries,
        "driver": [{"pay_yf_zj": 0.0}],
    }
    # 非必填空值省略（键序勿动：与既有响应键序一致）
    order_data.update(
        (k, texts[k])
        for k in ("c_sn", "c_name", "c_phone", "factory_name", "factory_bei", "b_wharf")
        if k in texts
    )
    if month:
        order_data["month"] = month
    if c_note:
        order_data["c_note"] = c_note
    if shou:
        order_data["shou"] = shou
    driver = order_data["driver"][0]
    if b_date:
        driver["b_date"] = b_date
    driver.update(
        (k, texts[k])
        for k in ("b_get_address", "b_back_address", "d_name", "d_phone")
        if k in texts
    )
    if d_num:
        driver["d_num"] = d_num
    if shou:
        driver["get_ys_zj"] = get_ys_zj

    return BillOrder(
        order_num1=bl_no,
        c_title=c_title,
        container_no=container_no,
        row_seq=row_seq,
        container_count=sum(b["box_num"] for b in box_entries),
        row_count=1,
        missing_fields=missing_fields,
        missing_reasons=missing_reasons,
        order_data=order_data,
        create_result=None,
    )


def group_orders(rows: list[BillRow], period: BillPeriod) -> AggregationOutput:
    """一行一票：每条数据行独立成单，不再按提单号合并同号多行。

    2026-08-31 业务拍板（TMS 允许同提单号多条订单，去重键改为提单号+箱号）。
    返回 AggregationOutput（orders + reconciliation 账单锚点对账，只报告不拦截）；
    输出保持账单行序；空/非法提单号行同样独立成单并标记缺失。
    """
    data_rows, fee_total, box_total, uppercase_total = _collect_anchors(rows)
    orders = [_order_from_row(row, period) for row in data_rows]
    return AggregationOutput(
        orders=orders,
        reconciliation=_build_reconciliation(orders, fee_total, box_total, uppercase_total),
    )
