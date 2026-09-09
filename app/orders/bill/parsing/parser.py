"""竞品账单 Excel 文件解析：表头识别、数据行读取、结算区间识别。

支持 .xlsx（openpyxl，data_only 读取缓存值）与 .xls（xlrd），通过
_SheetView 统一两个引擎的单元格访问（合并单元格填充、值保真语义一致），
按文件头 magic bytes 分发（parse_bill）。

- 表头行：前 MAX_HEADER_SCAN_ROWS 行内定位；流程分三级：
  1. 模板指纹命中（builtin 内置模板 / 已固化 AI 模板）→ 直接用模板列映射，不调 AI
  2. 未命中 → AI 表头映射（异构/变体模板；校验闸门不过抛 400 header_mapping_rejected）
  3. LLM 不可用/响应非法 → 回退现有精确匹配（找不到表头照旧 400）
- 数据行：表头下方所有非空行 → list[BillRow]，单元格值保真转换
  （数字保留浮点尾巴、日期转完整时间串，清洗留给归集步骤）。
- 结算区间：抬头区含「结算」的文本中提取两个 YYYY-MM-DD；
  识别不到返回空 BillPeriod（不报错）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import BadRequestError

from ..fees.fee_name_map import canonicalize_fee, canonicalize_fee_name, is_new_fee_schema
from ..schema import (
    HEADER_ALIASES,
    HEADER_COLUMN_MAP,
    IGNORED_HEADERS,
    MAX_HEADER_SCAN_ROWS,
    REQUIRED_HEADERS,
    BillPeriod,
    BillRow,
)
from . import template_store
from .columns import (
    business_end_col,
    discover_fee_columns,
    is_anchor_column,
    is_non_fee_header,
    is_note_column,
    match_source_cols,
    normalize_header,
)
from .legacy_template import compute_legacy_fingerprint, load_template
from .normalizers import NORMALIZER_REGISTRY, to_number
from .period import extract_bill_period, resolve_year_hint
from .sheet_view import _SheetView  # noqa: F401（read_data_rows 等签名引用）

# 支持的扩展名 → 期望的内容格式（与 _detect_format 的返回值对应）
SUPPORTED_EXTS: dict[str, str] = {".xls": "xls", ".xlsx": "xlsx", ".xlsm": "xlsx"}

# 内容格式 magic bytes：xlsx 为 zip（PK\x03\x04），xls 为 OLE2（D0 CF 11 E0）
_XLSX_MAGIC = b"PK\x03\x04"
_XLS_MAGIC = b"\xd0\xcf\x11\xe0"




@dataclass
class ParseOutput:
    """解析结果：数据行、结算区间、引擎、未识别列告警清单、模板信息。

    unmatched_headers：表头列未被映射（含 IGNORED_HEADERS 之外）且数据区
    至少有一个非空单元格的列表头原文（按列从左到右），供调用方显式处理，
    不静默丢弃；全空列（装饰列/间距列）不告警。AI 识别到的白名单外费用
    以降级 ignore 列形式并入（"new_fee:费用名"）。
    template：本次使用的模板 {fingerprint, source, name}；回退精确匹配时为 None。
    new_template：AI 新映射生成的待固化模板（对账 matched 后由服务层保存）。
    template_match：template_store 识别命中结果（YAML 模板库 L1/L2），含配置；
    未命中（AI 映射/回退精确匹配）时为 None。
    canonical_rows：模板驱动解析且列为标准字段（CanonicalOrder 字段名）时的
    行字典列表（目标字段 → 原文/归一化值），供新归集管线使用；既有流程语义
    （BillRow 字段模板）为 None，行在 rows（list[BillRow]）。
    """

    rows: list[BillRow]
    period: BillPeriod
    engine: str
    unmatched_headers: list[str] = field(default_factory=list)
    template: dict | None = None
    new_fees: list[str] = field(default_factory=list)
    new_template: Any | None = None
    template_match: Any | None = None
    canonical_rows: list[dict] | None = None


def _column_lookup() -> tuple[dict[str, str], dict[str, str]]:
    """归一化表头 → (BillRow 字段名, 费用标准名) 两个查找表（含列名变体）。

    费用名集 = 模板库旧式 fees 声明（2026-09-04 动态化：不再依赖代码常量，
    jinxin_v1 模板加费目即自动扩展——兜底链与 jinxin 同语义）。
    """
    data = dict(HEADER_COLUMN_MAP)
    fees = {name: name for name in template_store.collect_legacy_fee_names()}
    fees.update(HEADER_ALIASES)
    return data, fees


def _header_text(value: object) -> str:
    """表头单元格文本：去首尾空白后参与列名匹配（仅表头识别用，数据行不 strip）。"""
    return str(value).strip() if value is not None else ""








def find_header_row(view: _SheetView) -> int:
    """定位同时含「客户编号」「提单号」的表头行，找不到抛 BadRequestError。"""
    seen: set[str] = set()
    scan_rows = min(view.nrows, MAX_HEADER_SCAN_ROWS)
    for row in range(1, scan_rows + 1):
        headers = [
            normalize_header(_header_text(view.merged_cell(row, col)))
            for col in range(1, view.ncols + 1)
        ]
        seen.update(headers)
        if all(req in headers for req in REQUIRED_HEADERS):
            return row
    missing = [req for req in REQUIRED_HEADERS if req not in seen]
    raise BadRequestError(
        "no valid bill header row found within the first rows",
        details={
            "required_headers": list(REQUIRED_HEADERS),
            "missing_headers": missing,
            "scanned_rows": scan_rows,
        },
    )


def _to_money(value: object) -> float | None:
    """费用单元格转金额（T9 口径）：数字直转；文本按行拆分逐段解析累加
    （合并单元格多值如 '1300\r\n2200' = 两柜各自费用）；复用 to_number 支持
    千分位/货币符号/负号；任一段解析失败（含中文大写）→ None（该格记 warning）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    parts = [p.strip() for p in re.split(r"[\r\n]+", text) if p.strip()]
    if len(parts) == 1:
        return to_number(parts[0])
    total = 0.0
    for part in parts:
        money = to_number(part)
        if money is None:
            return None
        total += money
    return total


def _header_columns(
    view: _SheetView,
    header_row: int,
    data_lookup: dict[str, str] | None = None,
    fee_lookup: dict[str, str] | None = None,
    ignored_cols: set[int] | None = None,
) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    """表头行 → (数据列映射, 费用列映射, 潜在未匹配列 {列号: 原始表头文本})。

    未匹配 = 不在 lookup（默认 HEADER_COLUMN_MAP / 费用列 / 别名表；可注入
    模板/AI 映射），且不在 IGNORED_HEADERS（「当前状态」「已收/付金额」等
    有意忽略列不告警）；AI 明确 ignore 的列（ignored_cols）同样不告警。
    是否上报还取决于数据区是否有非空单元格（全空装饰列不报），见 read_data_rows。
    """
    if data_lookup is None or fee_lookup is None:
        data_lookup, fee_lookup = _column_lookup()
    data_cols: dict[int, str] = {}
    fee_cols: dict[int, str] = {}
    unmatched: dict[int, str] = {}
    for col in range(1, view.ncols + 1):
        raw_text = _header_text(view.merged_cell(header_row, col))
        text = normalize_header(raw_text)
        if text in data_lookup:
            data_cols[col] = data_lookup[text]
        elif text in fee_lookup:
            fee_cols[col] = fee_lookup[text]
        elif raw_text and text not in IGNORED_HEADERS and col not in (ignored_cols or set()):
            unmatched[col] = raw_text
    return data_cols, fee_cols, unmatched


def _dynamic_fee_cols(
    view: _SheetView,
    header_row: int,
    unmatched_cols: dict[int, str],
) -> tuple[dict[int, str], dict[int, str]]:
    """未知列动态费用收录判定（2026-09-04 用户拍板：费用项不写死、不要求先
    声明模板——账单新增费用列自动收录）。

    判据：未知列数据区出现任一可转金额（_to_money）的单元格 → 整列按费用
    收录，列名（去空白归一）作费用名，不进 unmatched；纯文本未知列（如月份、
    备注类）保持未识别上报，避免文本列误收。返回 (动态费用列, 保留上报列)。
    """
    dynamic: dict[int, str] = {}
    kept: dict[int, str] = {}
    for col, raw_text in unmatched_cols.items():
        name = normalize_header(raw_text)
        if (
            not name
            or is_anchor_column(name)
            or is_note_column(name)
            or is_non_fee_header(name)
        ):
            kept[col] = raw_text
            continue
        for row in range(header_row + 1, view.nrows + 1):
            raw = view.cell(row, col)
            if raw is None:
                continue
            if _to_money(raw) is not None:
                dynamic[col] = name
                break
        else:
            kept[col] = raw_text
    return dynamic, kept


def read_data_rows(
    view: _SheetView,
    header_row: int,
    *,
    data_lookup: dict[str, str] | None = None,
    fee_lookup: dict[str, str] | None = None,
    ignored_cols: set[int] | None = None,
) -> tuple[list[BillRow], list[str]]:
    """读取表头下方所有非空数据行 → (list[BillRow], 未识别列清单)。

    表头行按归一化列名匹配（默认 HEADER_COLUMN_MAP + 费用列 + 别名；可注入
    模板/AI 列名映射）；数据区直接读单元格原生值，合并单元格只有左上角有值
    （真实账单尾部「合计:」行为整行合并，其余字段应保持空，不做左上角值填充）。
    未知列动态收录（2026-09-04）：数据区含金额的未知列自动转费用列，金额进
    费用、非数字原文保留（与声明费用列同口径）；未收录列仅在其数据区至少有
    一个非空单元格时上报（全空装饰列/间距列不报；AI 明确 ignore 的列不报）。
    B1/B1'（忽略列金额判据二次收录，2026-09-07 小王费实证）已迁移收口至
    _parse_with_template（2026-09-08）：本路径仅服务旧指纹库/精确回退（无 AI
    ignored 输入），忽略列收录/两段 new_fee 兑底上报统一在模板路径承载。
    """
    data_cols, fee_cols, unmatched_cols = _header_columns(
        view, header_row, data_lookup, fee_lookup, ignored_cols
    )
    dynamic_fee_cols, unmatched_cols = _dynamic_fee_cols(
        view, header_row, unmatched_cols
    )
    fee_cols = {**fee_cols, **dynamic_fee_cols}
    unmatched_counts = {col: 0 for col in unmatched_cols}

    rows: list[BillRow] = []
    for row in range(header_row + 1, view.nrows + 1):
        kwargs: dict[str, object] = {}
        fees: dict[str, float] = {}
        has_value = False
        for col in range(1, view.ncols + 1):
            # 数据区不填充合并单元格：合并区域只有左上角有值，其余视为空
            raw = view.cell(row, col)
            if raw is None:
                continue
            text = raw if isinstance(raw, str) else str(raw)
            if not text.strip():
                continue
            has_value = True
            if col in data_cols:
                kwargs[data_cols[col]] = text
            elif col in fee_cols:
                money = _to_money(raw)
                if money is not None:
                    fees[fee_cols[col]] = money
                else:
                    # 非数字费用原文保留（如「合计大写」行中文大写金额），
                    # 供归集对账提取第二锚点；累加侧按数字过滤
                    fees[fee_cols[col]] = text
            elif col in unmatched_counts:
                unmatched_counts[col] += 1
        if has_value:
            kwargs["fees"] = fees
            rows.append(BillRow(**kwargs))
    unmatched = [unmatched_cols[col] for col in unmatched_cols if unmatched_counts[col] > 0]
    return rows, unmatched


# 序号列数值判定（row_filter: seq_numeric 口径）
_SEQ_NUMERIC_RE = re.compile(r"^\d+(\.\d+)?$")


# BillRow 字段名集合：模板 columns 目标字段 ⊆ 该集合 → 既有流程语义（构造 BillRow）
_BILLROW_FIELDS = set(BillRow.model_fields)


# 结算区间/年份提示识别已下沉 period.py（P4-S1）：extract_bill_period /
# resolve_year_hint 自此模块引用


def _apply_normalizer(
    field: str, func_name: str, value, year_hint: tuple[int, int] | None
):
    """按配置引用应用归一化函数；返回归一化值（解析失败返回 None，缺失语义）。

    特判：vessel_voyage_split 拆出的二元组按目标字段取对应项（vessel 取船名、
    voyage 取航次）；date_flex 注入 year_hint（M-D 补年份）。
    """
    func = NORMALIZER_REGISTRY.get(func_name)
    if func is None:
        return value
    if func_name == "vessel_voyage_split":
        vessel, voyage = func(value)
        return vessel if field == "vessel" else voyage
    if func_name == "date_flex":
        return func(value, year_hint)
    return func(value)


def _header_column_index(
    view: _SheetView, header_row: int, two_row: bool
) -> tuple[list[str], list[str]]:
    """表头行列名索引 → (列名(归一化), 区块名(归一化，单行全空))。

    双行表头：header_row-1 为区块行（section_fill: forward 即 merged_cell
    合并右填充），列名 = 区块非空时「区块.列名」；单行表头区块全空。

    空单元格统一走 _header_text（openpyxl None 与 xlrd 空文本 '' 同为空），
    否则 str(None) 得 "None" 会把业务区空区块列误标为「None.列名」导致失配。
    """
    section_row = header_row - 1 if two_row and header_row > 1 else 0
    sections: list[str] = []
    names: list[str] = []
    for col in range(1, view.ncols + 1):
        section = (
            _header_text(view.merged_cell(section_row, col)) if section_row else ""
        )
        name = _header_text(view.merged_cell(header_row, col))
        sections.append(normalize_header(section))
        if section:
            names.append(f"{normalize_header(section)}.{normalize_header(name)}")
        else:
            names.append(normalize_header(name))
    return names, sections


@dataclass
class RowParseLayout:
    """模板配置展开后的行解析只读布局（P4-S2 收口 15+ 局部变量）。

    unmatched_hits/pending_two_row_fees 为可变累计（行循环消费）；dynamic_fee_cols
    由 _discover_dynamic_fee_cols 在布局定稿前填充。
    """

    header_row: int
    is_billrow: bool
    stop_on: list[str]
    row_filter: str
    seq_col: int | None
    anchor_name: str
    normalizers: dict
    year_hint: tuple[int, int] | None
    field_cols: dict[str, list[int]]
    fee_cols: dict[int, str]
    new_fee_cols: dict[int, tuple[str, str]]
    anchor_cols: dict[int, tuple[str, str]]
    channels: dict
    default_section: str
    fees_cfg: dict
    dynamic_fee_cols: dict[int, str]
    unmatched_raw: dict[int, str]
    unmatched_hits: dict[int, int]
    pending_two_row_fees: list[str]


@dataclass
class RowFees:
    """单行费用/锚点抽取结果（P4-S2）：四收集器收口。"""

    fee_items: list[dict]
    anchors: dict[str, dict[str, float]]
    fee_failures: list[dict]
    fee_skipped: list[dict]


def _layout_from_template(
    view: _SheetView, match, filename: str
) -> RowParseLayout:
    """模板配置展开为行解析布局（P4-S2 自 _parse_with_template 准备段抽出）。

    columns/fees 段 → 列号映射（field_cols/fee_cols/new_fee_cols/anchor_cols）、
    行过滤参数、未识别列清单；动态费用收录候选（B1/B1'）同在此完成
    （_discover_dynamic_fee_cols），行循环前定稿。
    """
    template = match.template
    header_row = match.header_row
    header_cfg = template.get("header", {}) or {}
    two_row = bool(header_cfg.get("two_row"))
    names, sections = _header_column_index(view, header_row, two_row)
    business_end = business_end_col(template, names, sections)

    # columns 配置 → 目标字段 → 源列号（保持配置书写顺序）
    columns = template.get("columns", {}) or {}
    field_cols: dict[str, list[int]] = {}
    for target_field, spec in columns.items():
        cols = match_source_cols(spec, names, sections, business_end)
        if cols:
            field_cols[target_field] = cols

    # fees 段：新 schema（T10 费用通道配置）→ 费用列/锚点列发现 + 行级费用抽取；
    # 旧 schema（内置模板迁移：费用名 → 源列名）→ BillRow.fees 抽取（语义不变）
    fees_cfg = template.get("fees", {}) or {}
    fee_cols: dict[int, str] = {}
    new_fee_cols: dict[int, tuple[str, str]] = {}
    anchor_cols: dict[int, tuple[str, str]] = {}
    channels: dict = {}
    if is_new_fee_schema(fees_cfg):
        row_anchor = normalize_header(str(header_cfg.get("row_anchor") or "序号"))
        new_fee_cols, anchor_cols = discover_fee_columns(
            fees_cfg, names, sections, field_cols, row_anchor
        )
        channels = fees_cfg.get("channels") or {}
    else:
        for fee_name, spec in fees_cfg.items():
            for col in match_source_cols(spec, names, sections, business_end):
                fee_cols[col] = fee_name

    # 未识别列（非 IGNORED、非映射、数据区非空才上报）；unmatched 存原始表头文本；
    # AI 明确忽略的列（L3 候选配置 _ignored_cols）不告警
    ignored_cols = set(template.get("_ignored_cols") or [])
    mapped_cols_flat = (
        {c for cols in field_cols.values() for c in cols}
        | set(fee_cols)
        | set(new_fee_cols)
        | set(anchor_cols)
    )
    unmatched_raw: dict[int, str] = {
        col: _header_text(view.merged_cell(header_row, col))
        for col, name in enumerate(names, start=1)
        if (
            col not in mapped_cols_flat
            and col not in ignored_cols
            and name
            and name not in IGNORED_HEADERS
        )
    }
    # 序号列（row_filter: seq_numeric 口径）：row_anchor 列，columns 有 seq 时优先
    anchor_name = normalize_header(str(header_cfg.get("row_anchor") or "序号"))
    seq_cols = [col for col, name in enumerate(names, start=1) if name == anchor_name]
    if "seq" in field_cols:
        seq_cols = field_cols["seq"]
    seq_col = seq_cols[0] if seq_cols else None

    stop_on = [str(s) for s in (template.get("data", {}) or {}).get("stop_on") or []]
    row_filter = (template.get("data", {}) or {}).get("row_filter", "seq_numeric")
    normalizers = template.get("normalizers", {}) or {}
    year_hint = resolve_year_hint(template, filename, view, header_row)

    layout = RowParseLayout(
        header_row=header_row,
        is_billrow=set(columns) <= _BILLROW_FIELDS,
        stop_on=stop_on,
        row_filter=row_filter,
        seq_col=seq_col,
        anchor_name=anchor_name,
        normalizers=normalizers,
        year_hint=year_hint,
        field_cols=field_cols,
        fee_cols=fee_cols,
        new_fee_cols=new_fee_cols,
        anchor_cols=anchor_cols,
        channels=channels,
        default_section=next(iter(channels), ""),
        fees_cfg=fees_cfg,
        dynamic_fee_cols={},
        unmatched_raw=unmatched_raw,
        unmatched_hits={},
        pending_two_row_fees=[],
    )
    # 动态费用收录候选（B1/B1'）在布局最后完成：可能并进 fee_cols（BillRow）
    # 或 dynamic_fee_cols（canonical），须在行循环前定稿
    _discover_dynamic_fee_cols(view, header_row, template, layout, sections)
    layout.unmatched_hits = {col: 0 for col in layout.unmatched_raw}
    return layout


def _discover_dynamic_fee_cols(
    view: _SheetView, header_row: int, template: dict, layout: RowParseLayout, sections
) -> None:
    """未知列动态费用收录候选（B1/B1'，2026-09-07 用户拍板）。

    数据区含金额的未知列自动转费用列（列名去空白归一作费用名），金额进费用、
    非数字原文保留，与声明费用列同口径；纯文本未知列保持上报。
    B1'：两家族统一收录——此前仅 BillRow 语义模板生效，canonical 家族（AI
    映射/L1）未知金额列被沉默吞掉（生产实证：L3 认出运费列却只上报不解析，
    费用管理无建档）。canonical 收录列行级走 _fees，code 经别名字典（命中→
    标准码；未命中→other+原名，建档由 fee_bootstrap B2 兑底）。
    边界：仅单行表头（column_range 家族，sections 空）收录——two_row 区块表头
    的费用区由区块结构严格界定（_discover_fee_columns two_row 分支），区块外
    表尾列（抬头费/港杂费等杂项段）语义无保证，保持上报不收录（yahao golden
    实证：区块外收录打破对账恒等）。
    防误收：锚点特征（_is_anchor_column：合计/小计/已收/未收/已付/未付/利润，
    与 _ANCHOR_KEYWORDS 同口径）、备注类列、命中 IGNORED_HEADERS 的列、
    row_anchor 序号列（junyu 新式样序号列值 1 被误收事故回归：序号是行键
    非金额）、模板级 fees.ignore_headers（junyu 箱量列）均不收；命中
    IGNORED_HEADERS 的列已在 unmatched_raw 构造时排除，锚点/备注在此统一排除。
    收录源扩展（小王费实证）：_ignored_cols（AI 降级 ignore 固化列）同样过
    金额判据——否则 L3 白名单外费用列降级固化后依然被沉默吞掉。
    """
    is_billrow = layout.is_billrow
    fees_cfg = layout.fees_cfg
    anchor_name = layout.anchor_name
    unmatched_raw = layout.unmatched_raw
    ignore_names = {
        normalize_header(str(n)) for n in (fees_cfg.get("ignore_headers") or [])
    }
    # 已映射列（含费用/锚点列；动态收录前定稿）不重复入候选
    mapped_cols_flat = (
        {c for cols in layout.field_cols.values() for c in cols}
        | set(layout.fee_cols)
        | set(layout.new_fee_cols)
        | set(layout.anchor_cols)
    )
    candidates: dict[int, str] = dict(unmatched_raw)
    for _col in set(template.get("_ignored_cols") or []):
        if _col in mapped_cols_flat or _col in candidates:
            continue
        _text = _header_text(view.merged_cell(header_row, _col))
        if _text:
            candidates[_col] = _text
    if candidates and not any(sections):
        for col, raw_text in list(candidates.items()):
            name = normalize_header(raw_text)
            if (
                not name
                or name == anchor_name
                or is_anchor_column(name)
                or is_note_column(name)
                or is_non_fee_header(name)
                or name in IGNORED_HEADERS
                or name in ignore_names
            ):
                continue
            for row in range(header_row + 1, view.nrows + 1):
                raw = view.cell(row, col)
                if raw is not None and _to_money(raw) is not None:
                    if is_billrow:
                        layout.fee_cols[col] = name
                    else:
                        layout.dynamic_fee_cols[col] = name
                    unmatched_raw.pop(col, None)
                    break
    elif candidates:
        # two_row 区块表头（2026-09-07 审查 W3）：区块外列语义无保证不收录
        # （区块外收录打破对账恒等），但金额型候选（含 AI 降级 ignore 的费用列）
        # 不能沉默——以上报兑底（new_fee: 前缀，与 L3 白名单外同口径），提示
        # 人工确认后固化模板 fees 段；防误收判据与单行收录同口径。
        for col, raw_text in candidates.items():
            name = normalize_header(raw_text)
            if (
                not name
                or name == anchor_name
                or is_anchor_column(name)
                or is_note_column(name)
                or is_non_fee_header(name)
                or name in IGNORED_HEADERS
                or name in ignore_names
            ):
                continue
            for row in range(header_row + 1, view.nrows + 1):
                raw = view.cell(row, col)
                if raw is not None and _to_money(raw) is not None:
                    layout.pending_two_row_fees.append(name)
                    break


def _parse_with_template(
    view: _SheetView, match, engine: str, filename: str
) -> ParseOutput:
    """配置驱动解析（L1/L2 命中后）：表头定位/区块/列映射/行过滤/归一化全由
    模板配置声明，代码对目标字段名零假设。

    输出分流：目标 ⊆ BillRow 字段（内置模板迁移语义）→ rows（原文保真，费用进
    fees）；标准字段 → canonical_rows（归一化后）。明细见 _layout_from_template/
    _collect_row_fees/_append_parsed_row（P4-S2 拆分）。
    """
    layout = _layout_from_template(view, match, filename)
    raw_rows: list[BillRow] = [] if layout.is_billrow else []
    canonical_rows: list[dict] = [] if not layout.is_billrow else []

    for row in range(layout.header_row + 1, view.nrows + 1):
        # stop_on：任一列值含终止词 → 停止（合计/制单人页脚等）
        if layout.stop_on:
            row_texts = [
                str(view.cell(row, col)).strip()
                for col in range(1, view.ncols + 1)
                if view.cell(row, col) is not None
            ]
            if any(any(s in t for s in layout.stop_on) for t in row_texts):
                break
        # 未识别列计数（数据区非空才上报）
        for col in layout.unmatched_hits:
            raw = view.cell(row, col)
            if raw is not None and str(raw).strip():
                layout.unmatched_hits[col] += 1
        # row_filter: seq_numeric——序号列须为数值才是数据行；
        # BillRow 语义模板（内置迁移）不生效：尾部合计/箱量/大写行是归集对账锚点，
        # 须保留（与迁移前「全部非空行」一致，过滤发生在归集步骤）
        if (
            layout.row_filter == "seq_numeric"
            and layout.seq_col is not None
            and not layout.is_billrow
        ):
            seq_raw = view.cell(row, layout.seq_col)
            if seq_raw is None or not _SEQ_NUMERIC_RE.match(str(seq_raw).strip()):
                continue

        values: dict[str, object] = {}
        fees_map: dict[str, float | str] = {}
        for target_field, cols in layout.field_cols.items():
            for col in cols:
                raw = view.cell(row, col)
                if raw is None:
                    continue
                text = str(raw).strip()
                if not text:
                    continue
                # 多源列取最后一个非空（保持既有覆盖语义，空值不覆盖）
                values[target_field] = text
        for col, fee_name in layout.fee_cols.items():
            raw = view.cell(row, col)
            if raw is None:
                continue
            if str(raw).strip():
                money = _to_money(raw)
                fees_map[fee_name] = money if money is not None else str(raw)

        fees = _collect_row_fees(view, row, layout)
        if not values and not fees_map and not fees.fee_items:
            if layout.is_billrow:
                # 与迁移前 read_data_rows 一致：仅未映射列有值的行也保留（字段为空）
                has_any = any(
                    view.cell(row, col) is not None and str(view.cell(row, col)).strip()
                    for col in range(1, view.ncols + 1)
                )
                if not has_any:
                    continue
            else:
                continue

        _append_parsed_row(values, fees_map, fees, layout, raw_rows, canonical_rows)

    unmatched = [
        layout.unmatched_raw[col]
        for col in range(1, view.ncols + 1)
        if col in layout.unmatched_hits and layout.unmatched_hits[col] > 0
    ]
    # two_row 兑底上报（2026-09-07 审查 W3）：区块外金额列不收录但不沉默
    if layout.pending_two_row_fees:
        unmatched = [
            *unmatched,
            *(f"new_fee:{name}" for name in layout.pending_two_row_fees),
        ]
    period = extract_bill_period(view, layout.header_row)
    template_meta = {
        "fingerprint": match.fingerprint,
        "source": "builtin" if layout.is_billrow else "template",
        "name": match.template.get("name", match.template_id),
    }
    return ParseOutput(
        rows=raw_rows,
        period=period,
        engine=engine,
        unmatched_headers=unmatched,
        template=template_meta,
        template_match=match,
        canonical_rows=canonical_rows if not layout.is_billrow else None,
    )


def _collect_row_fees(
    view: _SheetView, row: int, layout: RowParseLayout
) -> RowFees:
    """单行费用/锚点抽取（P4-S2 自 _parse_with_template 主循环抽出）。

    新 fees schema 费用列 → 行级费用项（随归集按键汇总）：金额解析失败/空值/0
    不生成记录，失败记 warning 进对账报告；锚点列值保留；B1' 动态收录列同口径。
    """
    fee_items: list[dict] = []
    anchors: dict[str, dict[str, float]] = {}
    fee_failures: list[dict] = []
    fee_skipped: list[dict] = []
    if layout.new_fee_cols:
        # column_range 家族单行表头无区块行，section 默认取唯一启用区块
        # （保证「应收.费目」复合键映射与通道归属正确）
        for col, (section, name) in layout.new_fee_cols.items():
            section = section or layout.default_section
            raw = view.cell(row, col)
            if raw is None or not str(raw).strip():
                continue
            money = _to_money(raw)
            if money is None:
                fee_failures.append({"section": section, "name": name, "raw": str(raw)})
                continue
            if money == 0:
                continue  # 空值与 0 均不生成费用记录（账单侧合计仍按列值参与对账）
            meta = canonicalize_fee(section, name, layout.fees_cfg)
            if not meta["code"]:
                fee_skipped.append({"section": section, "name": name, "money": money})
                continue
            # T27a 负向扣减项：negative=true → 金额取负录入（两位小数字符串如 "-50.00"）；
            # 账单语义：应付合计 = Σ正项 − 扣除费列值（列值本身带符号：+486 扣减、-25 加回），
            # 故取 `-money`（相反数）而非 -abs：负值列取负后为 +（加回），恒等式自然成立；
            # negative_policy=skip_report 已在 canonicalize_fee 降级 import=False（不录入仅对账）
            if meta.get("negative"):
                money = -money
            fee_items.append(
                {
                    "section": section,
                    "name": name,
                    "channel": layout.channels.get(section) or "shou",
                    "code": meta["code"],
                    "import": meta["import"],
                    "reconcile": meta["reconcile"],
                    "negative": bool(meta.get("negative")),
                    "money": money,
                }
            )
    # B1'：canonical 家族动态收录列的行级抽取（金额→_fees；code 走别名字典，
    # 未命中归 other+原名——归集侧 _merge_fee_note 合并，建档由 B2 兑底）
    if layout.dynamic_fee_cols:
        for col, name in layout.dynamic_fee_cols.items():
            raw = view.cell(row, col)
            if raw is None or not str(raw).strip():
                continue
            money = _to_money(raw)
            if money is None or money == 0:
                continue  # 空值/0 不生成记录（与新 fees schema 同口径）
            code, _ = canonicalize_fee_name(name)
            fee_items.append(
                {
                    "section": "",
                    "name": name,
                    "channel": "shou",
                    "code": code,
                    "import": True,
                    "reconcile": True,
                    "negative": False,
                    "money": money,
                }
            )
    for col, (section, name) in layout.anchor_cols.items():
        raw = view.cell(row, col)
        if raw is None or not str(raw).strip():
            continue
        money = _to_money(raw)
        if money is None:
            continue
        anchors.setdefault(section, {})[name] = money
    return RowFees(
        fee_items=fee_items,
        anchors=anchors,
        fee_failures=fee_failures,
        fee_skipped=fee_skipped,
    )


def _append_parsed_row(
    values: dict[str, object],
    fees_map: dict[str, float | str],
    fees: RowFees,
    layout: RowParseLayout,
    raw_rows: list,
    canonical_rows: list,
) -> None:
    """行结果落桶（P4-S2 自 _parse_with_template 主循环抽出）：BillRow 直构 /
    canonical 归一化 + 行级私有键（_fees/_anchors/_fee_failures/_fee_skipped）。"""
    if layout.is_billrow:
        kwargs = dict(values)
        if fees_map:
            kwargs["fees"] = fees_map
        raw_rows.append(BillRow(**kwargs))
        return
    normalized: dict[str, object] = {}
    for target_field, value in values.items():
        func_name = layout.normalizers.get(target_field)
        if func_name:
            value = _apply_normalizer(
                target_field, func_name, value, layout.year_hint
            )
        if value is not None and value != "":
            normalized[target_field] = value
    # 行级费用私有键（归集侧按 group_key 聚合；下划线前缀不进响应）
    if fees.fee_items:
        normalized["_fees"] = fees.fee_items
    if fees.anchors:
        normalized["_anchors"] = fees.anchors
    if fees.fee_failures:
        normalized["_fee_failures"] = fees.fee_failures
    if fees.fee_skipped:
        normalized["_fee_skipped"] = fees.fee_skipped
    canonical_rows.append(normalized)


def _match_template_and_parse(
    view: _SheetView, engine: str, filename: str
) -> ParseOutput | None:
    """L1/L2 模板识别解析段（纯 CPU，无 LLM 调用）：命中 → 配置驱动解析。

    YAML 模板库（L1 指纹/L2 族级近似）与旧指纹库两级识别；未命中返回 None
    （交 L3：生产两段式编排走
    open_and_identify 信号）。同步/两段两入口共用，命中路径零额外开销。
    """
    match = template_store.identify(view)
    if match is not None:
        return _parse_with_template(view, match, engine, filename)
    scan_rows = min(view.nrows, MAX_HEADER_SCAN_ROWS)
    # 1) 指纹命中模板库（builtin 内置模板 / 已固化 AI 模板）→ 直接模板列映射，不调 AI
    for row in range(1, scan_rows + 1):
        headers = [
            normalize_header(_header_text(view.merged_cell(row, col)))
            for col in range(1, view.ncols + 1)
        ]
        template = load_template(compute_legacy_fingerprint(headers))
        if template is None:
            continue
        rows, unmatched = read_data_rows(
            view,
            row,
            data_lookup=dict(template.column_map),
            fee_lookup=dict(template.fee_map),
        )
        period = extract_bill_period(view, row)
        return ParseOutput(
            rows=rows,
            period=period,
            engine=engine,
            unmatched_headers=unmatched,
            template={
                "fingerprint": template.fingerprint,
                "source": template.source,
                "name": template.name,
            },
        )
    return None


def _parse_with_ai_result(
    view: _SheetView, engine: str, filename: str, ai
) -> ParseOutput:
    """L3 AI 映射解析段（纯 CPU）：AiHeaderResult → 临时模板配置 → 配置驱动解析。

    同步 _parse_sheet 与两段式 parse_ai_header 共用；与 YAML 模板同一条
    解析代码路径。"""
    headers = [
        normalize_header(_header_text(view.merged_cell(ai.header_row, col)))
        for col in range(1, view.ncols + 1)
    ]
    fingerprint = template_store.compute_fingerprint(headers)
    # L3 映射 → 临时模板配置（columns 段 + two_row/fee_boundary）→ 复用配置驱动解析：
    # 与 YAML 模板同一条代码路径；费用列本轮不映射（AI 白名单费用降级上报）
    candidate = template_store.build_template_config(
        fingerprint=fingerprint,
        header_row=ai.header_row,
        two_row=ai.two_row,
        fee_boundary=ai.fee_boundary,
        column_map=ai.column_map,
        ignored_cols=ai.ignored_cols,
    )
    match = template_store.TemplateMatch(
        template=candidate,
        header_row=ai.header_row,
        level="L3",
        fingerprint=fingerprint,
        template_id=candidate["template_id"],
    )
    out = _parse_with_template(view, match, engine, filename)
    # 费用列上报（new_fee: 前缀，2026-09-04 审查修复）：L3 候选模板不携带费用
    # 段（build_template_config 仅 columns），白名单内命中的费用列同样不落地——
    # 若只报白名单外，落入 33 费目池的新家族费目会静默丢失；统一以 new_fee:
    # 上报，提示人工确认后在固化模板 fees 段补映射
    pending_fee_names = [
        *ai.new_fees,  # 白名单外（原有语义）
        *(
            name for name in ai.fee_map.values() if name not in ai.new_fees
        ),  # 白名单内命中（本轮补报）
    ]
    out.unmatched_headers = [
        *out.unmatched_headers,
        *(f"new_fee:{name}" for name in pending_fee_names),
    ]
    out.new_fees = pending_fee_names
    # 候选模板配置：预览界面展示映射结果，人工确认后固化为 templates/{family}_v1.yaml
    out.new_template = candidate
    return out


def _parse_exact(view: _SheetView, engine: str) -> ParseOutput:
    """现状精确匹配路径（LLM 不可用回退）：找不到表头照旧 400，无模板信息。"""
    header_row = find_header_row(view)
    rows, unmatched = read_data_rows(view, header_row)
    period = extract_bill_period(view, header_row)
    return ParseOutput(rows=rows, period=period, engine=engine, unmatched_headers=unmatched)


def _parse_sheet(view: _SheetView, engine: str, filename: str = "") -> ParseOutput:
    """统一解析核心（纯 CPU，parse_bill 入口）：L1/L2 模板识别 → 未命中
    精确匹配回退（找不到表头照旧 400）。

    L3 AI 表头映射只走生产两段式编排（open_and_identify + achat_json +
    parse_ai_header，见 _parse_stage_async）；同步 parse_bill 为测试/纯 CPU
    语义基准，不再内联 LLM（2026-09 第二波同步链清理）。"""
    out = _match_template_and_parse(view, engine, filename)
    if out is not None:
        return out
    return _parse_exact(view, engine)


















# ---- 两段式阶段 API（生产编排：service._parse_stage_async；CPU 段均经 ----
# ---- to_thread 执行，LLM 网络段在编排层真异步）--------------------








