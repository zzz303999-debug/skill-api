"""竞品账单「一行一票」：每条数据行独立为 BillOrder（2026-08-31 业务拍板）。

对齐《竞品账单导入接口文档》一行一票口径。只做归集，不组装响应：
- 尾部非数据行（seq 非数字）过滤，顺手收集对账锚点（合计行/合计大写行/总箱型箱量行）
- 提单号清洗（去空格/连字符/浮点尾巴）与合法性校验
- 每行自成一组，不再按提单号合并同号多行（TMS 允许同提单号多条订单）
- box 同型累加（行内多箱型）、driver 取本行、费用本行金额、c_note 本行拼接、必填缺失标记
- reconciliation：用账单自带锚点校验归集结果（只报告不拦截）；费用双锚点
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .schema import (
    MISSING_BOX,
    MISSING_C_TITLE,
    MISSING_ORDER_NUM1,
    REASON_INVALID_FORMAT,
    REASON_NOT_FOUND,
    RECEIVABLE_FEE_COLUMNS,
    BillOrder,
    BillPeriod,
    BillRow,
    BoxGroup,
    CanonicalOrder,
    ContainerInfo,
    FeeItem,
    FeeReconcile,
)

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

# 中文大写金额（账单「合计大写」行）：数字/单位字符集与金额段正则。
# 合计大写由账单导出时写死、不经公式，可用作费用总额第二锚点。
_CN_UPPER_DIGITS = {
    "零": 0,
    "壹": 1,
    "贰": 2,
    "叁": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
}
_CN_UPPER_DEC_UNITS = {"拾": 10, "佰": 100, "仟": 1000}
_CN_UPPER_BIG_UNITS = {"万": 10000, "亿": 100000000}
_CN_MONEY_UNITS = ("元", "角", "分")
_CN_UPPER_SEGMENT_RE = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元角分整]+")


def _parse_cn_integer(s: str) -> float:
    """中文大写整数段式解析（拾佰仟万亿），空串 → 0；「拾」等省壹写法按 1 计。"""
    if not s:
        return 0.0
    total = 0.0
    section = 0.0
    num = 0
    for ch in s:
        if ch in _CN_UPPER_DIGITS:
            num = _CN_UPPER_DIGITS[ch]
        elif ch in _CN_UPPER_DEC_UNITS:
            section += (num if num else 1) * _CN_UPPER_DEC_UNITS[ch]
            num = 0
        elif ch in _CN_UPPER_BIG_UNITS:
            # 万/亿前数字缺失（num=0 且本段无累计）才按省壹补 1，如「万」=1 万；
            # 「贰拾万」num=0 但 section=20，不补（否则误加 1 万）
            section = (section + (num if (num or section) else 1)) * _CN_UPPER_BIG_UNITS[ch]
            total += section
            section = 0.0
            num = 0
    return total + section + num


def _parse_cn_upper_amount(text: str | None) -> float | None:
    """解析中文大写金额 → float；无大写金额段 → None。

    支持 零壹贰叁肆伍陆柒捌玖、拾佰仟万亿、元角分、整：
    「壹佰壹拾壹万贰仟肆佰零伍元整」→ 1112405.0；「壹元贰角叁分」→ 1.23；「伍角」→ 0.5。
    """
    if not text:
        return None
    segment = next(
        (s for s in _CN_UPPER_SEGMENT_RE.findall(text) if any(u in s for u in _CN_MONEY_UNITS)),
        None,
    )
    if segment is None:
        return None
    # 段内须含数字或整数单位（拾佰仟万亿），仅货币单位（如孤「元」）不构成金额
    if not any(
        ch in _CN_UPPER_DIGITS or ch in _CN_UPPER_DEC_UNITS or ch in _CN_UPPER_BIG_UNITS
        for ch in segment
    ):
        return None
    if "元" in segment:
        int_part, _, tail = segment.partition("元")
    else:
        int_part, tail = "", segment
    amount = _parse_cn_integer(int_part)
    if "角" in tail:
        jiao_part, _, tail = tail.partition("角")
        amount += _parse_cn_integer(jiao_part) * 0.1
    if "分" in tail:
        fen_part, _, _ = tail.partition("分")
        amount += _parse_cn_integer(fen_part) * 0.01
    return round(amount, 2)


@dataclass
class AggregationOutput:
    """归集结果：订单列表 + 账单锚点对账信息（reconciliation，只报告不拦截）。"""

    orders: list[BillOrder]
    reconciliation: dict = field(default_factory=dict)


def _collect_anchors(
    rows: list[BillRow],
) -> tuple[
    list[BillRow],
    dict[str, float] | None,
    dict[str, int] | None,
    float | None,
]:
    """过滤数据行并顺手收集对账锚点（不额外遍历）。

    返回 (数据行, 费用总额锚点, 箱型数量锚点, 合计大写金额锚点)：
    - seq 为「合计:」开头的行 → 各费用列数值即账单口径费用总额（fees 已按列名入字典）
    - seq 含「总箱型箱量」的行 → 从整行所有字段原文解析 箱型×数量（合并填充后
      其他列也可能有文本）；正则含 45 尺等真实行业箱型
    - 整行文本含「合计大写」的行 → 提取中文大写金额段解析为费用总额第二锚点
      （导出时写死不经公式，不受 SUM 公式缓存旧值影响）
    - 「制单人」等其余尾部行继续丢弃；同类型锚点只取首个
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
            parts = [
                str(value)
                for value in row.model_dump().values()
                if value is not None and str(value).strip()
            ]
            matches = _BOX_ANCHOR_RE.findall(" ".join(parts))
            if matches:
                box_total = {box_type.upper(): int(count) for box_type, count in matches}
            continue
        if uppercase_total is None:
            parts = [
                str(value)
                for value in row.model_dump().values()
                if value is not None and str(value).strip()
            ]
            row_text = " ".join(parts)
            if "合计大写" in row_text:
                uppercase_total = _parse_cn_upper_amount(row_text)
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


def _first_nonempty(rows: list[BillRow], field: str) -> str | None:
    """组内第一条非空字段值（按 seq 序）。"""
    for row in rows:
        value = getattr(row, field)
        if value is not None and str(value).strip():
            return value
    return None


def _unique_values(rows: list[BillRow], field: str) -> list[str]:
    """组内非空去重值（保持出现顺序），用于 c_note 段拼接。"""
    seen: list[str] = []
    for row in rows:
        value = getattr(row, field)
        if value is not None and str(value).strip() and value not in seen:
            seen.append(value)
    return seen


def _clean_plate_no(value) -> str | None:
    """车牌清洗：Excel 数字单元格浮点尾巴（9486.0 → 9486），其余原样。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _FLOAT_TAIL_RE.match(text):
        return text.split(".")[0]
    return text


def _unique_plate_values(rows: list[BillRow]) -> list[str]:
    """组内非空车牌去重（清洗浮点尾巴后，保持出现顺序），供 c_note 段拼接。"""
    seen: list[str] = []
    for row in rows:
        value = _clean_plate_no(row.d_num)
        if value and value not in seen:
            seen.append(value)
    return seen


def _box_entries(rows: list[BillRow]) -> tuple[list[dict], bool]:
    """box 归集：同箱型累加，按首次出现顺序；返回 (条目, 是否有箱型原文)。

    箱型不做格式校验（业务确认 2026-08-12）：非空即合法，含标准箱型与
    大冷/飞翼车/2X20/拼箱 等非标表述；无原文才视为缺失。
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    any_type = False
    for row in rows:
        raw = (row.b_type or "").strip().upper()
        if not raw:
            continue
        any_type = True
        if raw not in counts:
            counts[raw] = 0
            order.append(raw)
        counts[raw] += 1
    entries = [{"b_type": t, "box_num": counts[t]} for t in order]
    return entries, any_type


def _pick_month_day(row: BillRow) -> str | None:
    """b_date 候选：日期列优先，做箱时间列回退（#12 默认决策，待业务确认）。"""
    for value in (row.b_date, row.b_date_time):
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


def _fee_entries(rows: list[BillRow]) -> tuple[list[dict], float]:
    """费用累加：按 RECEIVABLE_FEE_COLUMNS 顺序，同名费用多行金额累加。

    仅该费用名存在数字来源（至少一行金额非 None）才建条目；合计入 get_ys_zj。
    """
    entries: list[dict] = []
    total = 0.0
    for name in RECEIVABLE_FEE_COLUMNS:
        amounts = [v for v in (row.fees.get(name) for row in rows) if isinstance(v, (int, float))]
        if amounts:
            amount = round(sum(amounts), 2)
            entries.append({name: {"money": amount}})
            total += amount
    return entries, round(total, 2)


def _build_c_note(rows: list[BillRow], other_dates: list[str]) -> str | None:
    """c_note 拼接（§5.1 顺序）：客户编号→业务类型→箱号→车队→车牌（仅多车牌）→
    备注→应付备注→其他做箱日期。

    各段组内非空去重值按序 "," 连接，段间 "；" 分隔，空段跳过；备注/应付备注直拼无前缀。
    """
    segments: list[str] = []
    c_sns = _unique_values(rows, "c_sn")
    if c_sns:
        segments.append("客户编号：" + ",".join(c_sns))
    biz_types = _unique_values(rows, "biz_type")
    if biz_types:
        segments.append("业务类型：" + ",".join(biz_types))
    container_nos = _unique_values(rows, "container_no")
    if container_nos:
        segments.append("箱号：" + ",".join(container_nos))
    fleets = _unique_values(rows, "fleet")
    if fleets:
        segments.append("车队：" + ",".join(fleets))
    # 多车牌段（2026-08-26）：driver[0][d_num] 单值只能记首行车牌，组内多个
    # 不同车牌（清洗浮点尾巴后）并入备注防丢失；单车牌不追加（备注保持原样）
    plate_nos = _unique_plate_values(rows)
    if len(plate_nos) > 1:
        segments.append("车牌：" + ",".join(plate_nos))
    remarks = _unique_values(rows, "remark")
    if remarks:
        segments.append(",".join(remarks))
    payables = _unique_values(rows, "payable_remark")
    if payables:
        segments.append(",".join(payables))
    if other_dates:
        segments.append("其他做箱日期：" + ",".join(other_dates))
    return "；".join(segments) if segments else None


def _build_order(group: list[BillRow], period: BillPeriod) -> BillOrder:
    """单行 → BillOrder（一行一票；group 恒含 1 行）。"""
    ordered = sorted(
        group,
        key=lambda r: (
            float(r.seq) if _SEQ_RE.match((r.seq or "").strip()) else float("inf"),
            r.seq or "",
        ),
    )
    bl_no, bl_reason = clean_order_num(ordered[0].order_num1)

    c_title = _first_nonempty(ordered, "c_title")
    c_name = _first_nonempty(ordered, "c_name")
    c_phone = _first_nonempty(ordered, "c_phone")
    c_sn = _first_nonempty(ordered, "c_sn")
    factory_name = _first_nonempty(ordered, "factory_name")
    factory_bei = _first_nonempty(ordered, "factory_bei")
    b_wharf = _first_nonempty(ordered, "b_wharf")
    b_get_address = _first_nonempty(ordered, "b_get_address")
    b_back_address = _first_nonempty(ordered, "b_back_address")
    d_name = _first_nonempty(ordered, "d_name")
    d_phone = _first_nonempty(ordered, "d_phone")
    d_num = _clean_plate_no(_first_nonempty(ordered, "d_num"))
    # 箱号取本行原文（去空白），行序号清洗浮点尾巴——二者供去重组合键使用
    raw_container = _first_nonempty(ordered, "container_no")
    container_no = raw_container.strip() if raw_container else None
    row_seq = _clean_group_key(ordered[0].seq)

    # box：同箱型累加；无箱型原文才视为缺失（不做格式校验）
    box_entries, has_valid_box = _box_entries(ordered)

    # b_date：日期列优先、做箱时间回退，月-日补年份；组内多日期其余并入 c_note
    md_list: list[str] = []
    for row in ordered:
        md = _pick_month_day(row)
        if md and md not in md_list:
            md_list.append(md)
    b_date = _fill_year(md_list[0], period) if md_list else None
    other_dates = md_list[1:] if len(md_list) > 1 else []
    month = b_date[:7] if b_date else None

    # 费用：同名累加为一条 shou，合计入 get_ys_zj
    shou, get_ys_zj = _fee_entries(ordered)

    c_note = _build_c_note(ordered, other_dates)

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

    # order_data：必填项缺失显式置 None/[]，不填空串/0；非必填空值省略键
    # 注意：data 恒为 1 条货物明细（2026-08-26 实测修正：N 条相同 b_order_num 导致
    # TMS 按明细重复计入费用总额；row_count 保留原始行数，柜级信息由 box[]/driver[0] 承载）
    order_data: dict = {
        "order_num1": bl_no,
        "type": 1,
        "c_title": c_title,
        # data 收敛为 1 条货物明细（2026-08-26 实测修正）：TMS 按 data 条数展开
        # 订单明细并把费用挂到每条明细——N 条相同 b_order_num 导致费用总额
        # （如运费 6450）被每条重复计入；对齐标准通道 data[0] 货物明细语义
        # （payload.py 只发 data[0]，TMS 实测可下单）。柜级差异信息（箱型/车牌）
        # 分别由 box[] 聚合与 driver[0] 承载，data 无需逐行展开。
        "data": [{"b_order_num": bl_no}],
        "box": box_entries,
        "driver": [{"pay_yf_zj": 0.0}],
    }
    for key, value in (
        ("c_sn", c_sn),
        ("c_name", c_name),
        ("c_phone", c_phone),
        ("factory_name", factory_name),
        ("factory_bei", factory_bei),
        ("b_wharf", b_wharf),
        ("month", month),
        ("c_note", c_note),
    ):
        if value:
            order_data[key] = value
    if shou:
        order_data["shou"] = shou
    driver = order_data["driver"][0]
    if b_date:
        driver["b_date"] = b_date
    if b_get_address:
        driver["b_get_address"] = b_get_address
    if b_back_address:
        driver["b_back_address"] = b_back_address
    if d_name:
        driver["d_name"] = d_name
    if d_phone:
        driver["d_phone"] = d_phone
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
        row_count=len(ordered),
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
    orders = [_build_order([row], period) for row in data_rows]
    return AggregationOutput(
        orders=orders,
        reconciliation=_build_reconciliation(orders, fee_total, box_total, uppercase_total),
    )


# ---- 标准字段归集（TMS 通道，模板配置驱动；见《字段映射表》§4） ----

# 单行组取值字段（CanonicalOrder 标量字段，剔除聚合/元信息；
# bl_no 保留在单值集内——构造时单独清洗浮点尾巴后作为提单号）
_SINGLE_FIELDS: tuple[str, ...] = tuple(
    name
    for name in CanonicalOrder.model_fields
    if name
    not in {
        "box_groups",
        "containers",
        "month",
        "missing_fields",
        "source_template",
        "row_count",
        "fees",
        "fee_reconcile",
        # 行序号不入单值集：行 dict 键名为 seq，构造时单独清洗赋值（去重键兜底段）
        "row_seq",
    }
)


def _clean_group_key(value) -> str | None:
    """归集键清洗：去浮点尾巴（提单号/业务编号被 Excel 存成数字）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _FLOAT_TAIL_RE.match(text):
        return text.split(".")[0]
    return text


def _box_type_of(row: dict) -> str | None:
    """行内箱型：box_type_qty 归一化结果首项（非标箱型同此）。"""
    items = row.get("box_type_qty")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0].get("type")
    return None


def _aggregate_box_groups(rows: list[dict]) -> list[BoxGroup]:
    """同组多行/多箱型聚合：box_type_qty 逐行解析结果按箱型累加数量（保出现序）。"""
    counts: dict[str, int] = {}
    order: list[str] = []
    for row in rows:
        items = row.get("box_type_qty")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            box_type = str(item.get("type") or "").strip()
            if not box_type:
                continue
            qty = int(item.get("qty") or 1)
            if box_type not in counts:
                counts[box_type] = 0
                order.append(box_type)
            counts[box_type] += qty
    return [BoxGroup(b_type=t, box_num=counts[t]) for t in order]


def _aggregate_containers(rows: list[dict]) -> list[ContainerInfo]:
    """同组多行箱信息聚合：每行一条（箱号/箱型/封条号），保持行序。"""
    containers: list[ContainerInfo] = []
    for row in rows:
        container_no = str(row.get("container_no") or "").strip()
        seal_no = str(row.get("seal_no") or "").strip()
        if not container_no and not seal_no:
            continue
        containers.append(
            ContainerInfo(
                container_no=container_no or None,
                box_type=_box_type_of(row),
                seal_no=seal_no or None,
            )
        )
    return containers


def _pick_month(row: dict) -> str | None:
    """账期 YYYY-MM：order_date 优先，work_date 回退。"""
    for key in ("order_date", "work_date"):
        value = row.get(key)
        if value and len(str(value)) >= 7:
            return str(value)[:7]
    return None


def group_canonical(
    rows: list[dict],
    template: dict,
    period: BillPeriod | None = None,
) -> list[CanonicalOrder]:
    """标准字段行一行一票 → CanonicalOrder 列表（TMS 通道）。

    2026-08-31 业务拍板：每条数据行独立成单，不再按模板 group_key 归集
    （配置废弃不再读取）；TMS 允许同提单号多条订单。每行的箱信息聚合为
    containers/box_groups、费用为行级金额；必填（bl_no/box_groups）缺失
    登记 missing_fields，不阻塞。输出保持账单行序。
    """
    return [_build_canonical([row], template, period) for row in rows]


def _aggregate_fees(
    group: list[dict], template: dict
) -> tuple[list[FeeItem], dict[str, FeeReconcile]]:
    """行级费用聚合（T10/T14）：按 (通道, 标准费目码) 累加 → FeeItem + 通道对账。

    - import:false 项（税金等）excluded=True（不录入仅对账，金额进排除项合计）；
    - to_other（code=other）原名去重进 note（payload 拼「原名 ¥金额」列表）；
    - 对账恒等：bill_total（账单锚点列「合计/小计」Σ，含排除项）− recorded_total
      = excluded_total，容差 0.01；超差 ok=False 进对账报告（只报告不拦截）。
    """
    fees_cfg = template.get("fees", {}) or {}
    channels = fees_cfg.get("channels") or {}
    default_channel = next(iter(channels.values()), "shou")

    by_key: dict[tuple[str, str], FeeItem] = {}
    reconcile: dict[str, FeeReconcile] = {}
    for row in group:
        for item in row.get("_fees") or []:
            ch = str(item.get("channel") or "shou")
            code = str(item.get("code") or "other")
            money = Decimal(str(round(float(item.get("money") or 0), 2)))
            importable = bool(item.get("import", True))
            rec = reconcile.setdefault(ch, FeeReconcile())
            key = (ch, code)
            fee = by_key.get(key)
            if fee is None:
                fee = FeeItem(
                    channel=ch,
                    code=code,
                    money=Decimal("0"),
                    note=None,
                    excluded=not importable,
                )
                by_key[key] = fee
            fee.money += money
            if code == "other":
                _merge_fee_note(fee, item.get("name"))
            if importable:
                rec.recorded_total += money
            else:
                rec.excluded_total += money
        for section, anchor_map in (row.get("_anchors") or {}).items():
            ch = channels.get(section) or default_channel
            rec = reconcile.setdefault(ch, FeeReconcile())
            for name, money in anchor_map.items():
                # bill_total 只取「合计/小计」列（已收/未收等状态列不参与）
                if "合计" in name or "小计" in name:
                    rec.bill_total += Decimal(str(money))
    for rec in reconcile.values():
        rec.diff = rec.bill_total - rec.recorded_total - rec.excluded_total
        # 无锚点（bill_total=0，账单侧无有效合计列）不算 mismatch——与金科信
        # no_anchor 语义一致（只报告不拦截，缺锚点不误报）；有锚点才判恒等
        rec.ok = rec.bill_total == 0 or abs(rec.diff) <= Decimal("0.01")
    return list(by_key.values()), reconcile


def _merge_fee_note(fee: FeeItem, name) -> None:
    """to_other 原名去重拼接（保留出现顺序）。"""
    if not name:
        return
    parts = fee.note.split(",") if fee.note else []
    if name not in parts:
        parts.append(name)
    fee.note = ",".join(parts)


def _build_canonical(
    group: list[dict], template: dict, period: BillPeriod | None
) -> CanonicalOrder:
    """单行 → CanonicalOrder（一行一票；group 恒含 1 行）。"""
    template_id = template.get("template_id", "")
    # 单值字段：组内首行非空（行序即账单出现顺序）
    values: dict[str, object] = {}
    for field_name in _SINGLE_FIELDS:
        for row in group:
            value = row.get(field_name)
            if value is not None and str(value).strip():
                values[field_name] = value
                break
    bl_no = _clean_group_key(values.get("bl_no"))
    box_groups = _aggregate_box_groups(group)
    containers = _aggregate_containers(group)
    month = values.get("month")
    if not month and group:
        month = _pick_month(group[0])
    fees, fee_reconcile = _aggregate_fees(group, template)

    # 车牌清洗（Excel 数字单元格浮点尾巴 9486.0 → 9486，与旧链路同口径）；
    # 组内多个不同车牌（driver 单值只能记首行）并入 remark 防丢失（2026-08-26）
    if values.get("plate_no"):
        values["plate_no"] = _clean_plate_no(values["plate_no"])
    plate_nos = [
        p
        for p in (_clean_plate_no(row.get("plate_no")) for row in group)
        if p
    ]
    unique_plates: list[str] = []
    for p in plate_nos:
        if p not in unique_plates:
            unique_plates.append(p)
    if len(unique_plates) > 1:
        suffix = "车牌：" + ",".join(unique_plates)
        remark = values.get("remark")
        values["remark"] = f"{remark}；{suffix}" if remark else suffix

    order = CanonicalOrder(
        bl_no=bl_no or values.get("bl_no"),
        box_groups=box_groups,
        containers=containers,
        month=month,
        fees=fees,
        fee_reconcile=fee_reconcile,
        source_template=template_id,
        # 行序号（清洗浮点尾巴）：去重键的行序号兜底段（无箱号时）
        row_seq=_clean_group_key(group[0].get("seq")) if group else None,
        row_count=len(group),
        **{k: v for k, v in values.items() if k != "bl_no"},
    )
    # 费用通道默认值（payload 发射用）：模板 fees.fee_defaults 烘焙进私有属性
    order._fee_defaults = (template.get("fees", {}) or {}).get("fee_defaults") or {}
    # 必填缺失登记（配置 required 优先，缺省 bl_no/box_groups）
    if not order.bl_no:
        order.add_missing("bl_no")
    if not order.box_groups:
        order.add_missing("box_groups")
    if not order.customer_name:
        order.add_missing("customer_name")
    return order
