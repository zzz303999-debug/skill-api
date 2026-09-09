"""模板配置驱动的列发现与映射（纯函数族，无 I/O）。

从 parser.py 拆出（2026-09）：表头列名匹配（列名/区块.列名/同名#N）、业务列
边界（fee_boundary 三模式）、费用列与对账锚点列发现（T9 新 fees schema）。
输入为归一化列名/区块名列表（list[str]）与模板配置，同一输入恒定输出。
"""

from __future__ import annotations

import re

from ..schema import IGNORED_HEADERS

# 表头文本归一化：去除全部空白后参与列名匹配（仅表头识别用，数据行不处理）
_HEADER_WHITESPACE_RE = re.compile(r"[\s\u3000]+")
# 「列名#N」：同名第 N 次出现（同名列消歧，如 车牌号#1 取首次）
_COL_ORDINAL_RE = re.compile(r"^(.*?)#(\d+)$")
# 双行表头列名：配置源列名「区块.列名」拆区块与列名
_SECTION_COL_RE = re.compile(r"^(.*?)\.(.+)$")
# 合计分界列（fee_boundary: total_columns 模式）：业务列右边界 = 首个合计列
_TOTAL_COLUMNS = ("应收合计", "应付合计", "成本合计")
# 双行表头家族的费用区块名（区块行非空单元格即区块名，费用区不参与业务列匹配）
_SECTION_NAMES = ("应收", "应付", "车辆成本", "公司成本", "成本")

# 对账锚点列关键字（T9）：含这些关键字的列不是费目（合计/小计/已收/未收/已付/未付/
# 利润），值保留为对账锚点；bill_total 取「合计/小计」列，其余仅排除不抽取
_ANCHOR_KEYWORDS = ("合计", "小计", "已收", "已付", "未收", "未付", "利润")
# 备注类列名（column_range 家族费用列发现时排除，避免备注被当费用列）
_NOTE_KEYWORDS = ("备注", "附言")
# 业务列关键词（2026-09-07 审查 W3）：动态收录排除——数字型业务列（junyu 箱量/
# 新式样件数毛重/手机号等）数据区含金额会被误收为 other+原名费用并经 B2 建档
# 污染价格表；收录判据（_parse_with_template/read_data_rows）统一排除；命中但
# 列名以「费」结尾的不判业务列（电话费/手机费是费用列名，2026-09-08 审查 W3
# 误杀修复——业务列名如手机号/联系电话不以「费」结尾，不受影响）
_NON_FEE_KEYWORDS = (
    "箱量", "件数", "毛重", "净重", "重量", "体积", "手机", "电话", "联系方式",
)


def normalize_header(text: str) -> str:
    """表头文本归一化：去除全部空白后参与列名匹配（仅表头识别用，数据行不处理）。"""
    return _HEADER_WHITESPACE_RE.sub("", text or "")


def business_end_col(
    template: dict, names: list[str], sections: list[str]
) -> int:
    """业务列右边界（fee_boundary 三种模式；本轮只圈定业务列范围，费用值不映射）。

    - section_header：费用区块（应收/应付/车辆成本/公司成本）起始列
    - total_columns：首个合计列（应收合计/应付合计/成本合计）
    - column_range/缺省：无分界，全部列（配置精确控制）
    """
    mode = template.get("fee_boundary", "column_range")
    if mode == "section_header":
        for col, section in enumerate(sections, start=1):
            if section in _SECTION_NAMES:
                return col
        return len(names) + 1
    if mode == "total_columns":
        for col, name in enumerate(names, start=1):
            if any(total in name for total in _TOTAL_COLUMNS):
                return col
        return len(names) + 1
    return len(names) + 1


def match_source_cols(
    spec, names: list[str], sections: list[str], business_end: int
) -> list[int]:
    """配置源列名规范 → 匹配到的列号列表（业务区内按列序）。

    支持：
    - 「列名」：归一化列名相等（单行家族）；
    - 「区块.列名」：区块与列名双匹配（two_row 家族费用列等带前缀列）；
    - 「列名#N」：同名第 N 次出现（同名列消歧，如 车牌号#1 取首次）。
    """
    if isinstance(spec, list):
        cols: list[int] = []
        for item in spec:
            cols.extend(match_source_cols(item, names, sections, business_end))
        return sorted(set(cols))
    spec = str(spec).strip()
    if not spec:
        return []
    m = _COL_ORDINAL_RE.match(spec)
    if m:
        col_name, ordinal = m.group(1), int(m.group(2))
        matched = [
            col
            for col, name in enumerate(names, start=1)
            if col < business_end and name == normalize_header(col_name)
        ]
        return [matched[ordinal - 1]] if ordinal <= len(matched) else []
    m = _SECTION_COL_RE.match(spec)
    if m:
        section, col_name = m.group(1), m.group(2)
        return [
            col
            for col in range(1, len(names) + 1)
            if col < business_end
            and sections[col - 1] == normalize_header(section)
            and names[col - 1] == normalize_header(col_name)
        ]
    normalized = normalize_header(spec)
    return [
        col
        for col, name in enumerate(names, start=1)
        if col < business_end and name == normalized
    ]


def is_anchor_column(name: str) -> bool:
    """对账锚点列判定：列名含 合计/小计/已收/未收/已付/未付/利润 关键字。"""
    return any(kw in name for kw in _ANCHOR_KEYWORDS)


def is_note_column(name: str) -> bool:
    """备注类列判定（column_range 家族费用列发现时排除）。"""
    return any(kw in name for kw in _NOTE_KEYWORDS)


def is_non_fee_header(name: str) -> bool:
    """业务列判定（收录排除）：列名含业务关键词且不以「费」结尾 → 非费用。

    以「费」结尾豁免（2026-09-08 审查 W3）：电话费/手机费/重量费等是真实
    费用列名而非业务列；业务列名（手机号/联系电话/箱量/毛重/件数）不以
    「费」结尾，仍按关键词命中排除。
    """
    return bool(name) and not name.endswith("费") and any(
        kw in name for kw in _NON_FEE_KEYWORDS
    )


def range_bounds(
    ranges_cfg: dict, names: list[str], field_cols: dict[str, list[int]]
) -> dict[str, tuple[int, int]]:
    """单行 total_columns 家族：区块 → (费用区起点列, 终点列)（排他）。

    区块费用区 = 上一分界列之后 到 本区块分界列（ranges 值 = 区块末尾合计列名）
    之前；首个区块起点 = 业务列（columns 匹配）最后一列之后。分界列缺失 → 区块跳过。
    """
    business_last = max((c for cols in field_cols.values() for c in cols), default=0)
    bounds: dict[str, tuple[int, int]] = {}
    prev_end = business_last + 1
    for section, anchor_name in ranges_cfg.items():
        end = next(
            (
                col
                for col, name in enumerate(names, start=1)
                if name == normalize_header(str(anchor_name))
            ),
            None,
        )
        if end is None:
            continue
        bounds[section] = (prev_end, end)
        prev_end = end + 1
    return bounds


def discover_fee_columns(
    fees_cfg: dict,
    names: list[str],
    sections: list[str],
    field_cols: dict[str, list[int]],
    row_anchor: str = "序号",
) -> tuple[dict[int, tuple[str, str]], dict[int, tuple[str, str]]]:
    """新 fees schema（T9）：费用列/锚点列发现，代码零费目名硬编码。

    返回 (费目列 {列号: (区块, 列名)}, 锚点列 {列号: (区块, 列名)})：
    - two_row（区块行非空）：区块 ∈ fees.channels 的列；锚点列 = 含锚点关键字列；
    - 单行 + fees.ranges（区块 → 末尾合计列名）：范围内列（长尾费目也抽取 → to_other），
      边界合计列本身是锚点列（对账锚点）；
    - 单行无 ranges（column_range 家族）：columns 未映射列中排除 row_anchor（序号）/
      备注/IGNORED 后的全部列（mapping 只负责码解析，长尾自动 to_other，保证对账恒等）；
      锚点列取 fees.anchors 显式声明（账单侧合计列语义家族差异大，配置为准）。
    """
    fee_cols: dict[int, tuple[str, str]] = {}
    anchor_cols: dict[int, tuple[str, str]] = {}
    anchor_row = normalize_header(row_anchor)
    if any(sections):
        channels = set(fees_cfg.get("channels") or {})
        for col, (section, name) in enumerate(zip(sections, names, strict=True), start=1):
            if not section or section not in channels or not name:
                continue
            # two_row 表头列名为「区块.列名」复合键，抽取侧还原纯列名
            # （区块参与匹配由 section 承担，费目名/字典/note 都须无前缀）
            if name.startswith(section + "."):
                name = name[len(section) + 1 :]
            if is_note_column(name):
                continue  # 备注类列（防御：区块内误排备注）
            if is_anchor_column(name):
                anchor_cols[col] = (section, name)
            else:
                fee_cols[col] = (section, name)
        return fee_cols, anchor_cols
    ranges = fees_cfg.get("ranges") or {}
    if ranges:
        bounds = range_bounds(ranges, names, field_cols)
        boundary_section = {normalize_header(str(v)): s for s, v in ranges.items()}
        for col, name in enumerate(names, start=1):
            if not name:
                continue
            section = next(
                (s for s, (lo, hi) in bounds.items() if lo <= col < hi), None
            )
            if section is None:
                # 边界合计列（ranges 值）本身是锚点，归属声明区块
                section = boundary_section.get(normalize_header(name))
                if section is not None:
                    anchor_cols[col] = (section, name)
                continue
            if is_note_column(name):
                continue  # 备注类列（如赢辉大表备注，非费目）
            if is_anchor_column(name):
                anchor_cols[col] = (section, name)
            else:
                fee_cols[col] = (section, name)
        return fee_cols, anchor_cols
    # column_range：columns 未映射列（排除序号/锚点/备注/IGNORED/模板 ignore_headers）
    # 即费用列；锚点列取 fees.anchors 显式声明（如 秋怡小计列；军羽小计列恒值无效则不配）
    mapped = {c for cols in field_cols.values() for c in cols}
    # 模板声明的非费目列（fees.ignore_headers，2026-09-03）：新式样费用区插入的
    # 业务列（如「箱量」）不参与自动发现——此前手机号列被误收为其它费致脏费用
    ignore_set = {
        normalize_header(str(h))
        for h in (fees_cfg.get("ignore_headers") or [])
        if str(h).strip()
    }
    for col, name in enumerate(names, start=1):
        if col in mapped or not name:
            continue
        if normalize_header(name) == anchor_row:
            continue  # 序号列（row_anchor）非费用
        if is_anchor_column(name):
            continue  # 合计/小计等锚点列非费目（锚点只取 fees.anchors 显式声明）
        if is_note_column(name) or name in IGNORED_HEADERS:
            continue
        if normalize_header(name) in ignore_set:
            continue  # 模板显式忽略列（非费目）
        fee_cols[col] = ("", name)
    anchors_cfg = fees_cfg.get("anchors") or {}
    for section, anchor_names in anchors_cfg.items():
        for anchor_name in anchor_names if isinstance(anchor_names, list) else [anchor_names]:
            for col, name in enumerate(names, start=1):
                if name == normalize_header(str(anchor_name)):
                    anchor_cols[col] = (str(section), name)
                    fee_cols.pop(col, None)
    return fee_cols, anchor_cols
