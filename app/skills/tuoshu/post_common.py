"""托书结果后处理的公共工具层：issue 管理、显式字段提取与候选识别。"""

from __future__ import annotations

import re
from typing import Any

from .normalize import (
    is_separator_row,
    normalize_label,
    parse_date_or_none,
    parse_html_table_rows,
    pipe_row_cells,
)

_MARKDOWN_PARAGRAPH_PREFIX_RE = re.compile(
    r"^_(?:p|l)\d+(?:: \(empty\))?_\s*", re.IGNORECASE
)


# LibreOffice 转 WPS 老版 .doc 时正文内容常落入文本框，转换器为每个文本框
# 生成结构行。标签→值游走时这些行不是候选值，必须跳过。
_CONVERTER_STRUCTURE_LINE_RE = re.compile(
    r"^(?:## Text boxes|### Text box \d+|### Embedded image OCR: .+|_source: .+)$",
    re.IGNORECASE,
)


_INLINE_LABEL_BOUNDARY_RE = re.compile(
    r"\s+(?=(?:TO|致|ATTN|FROM|FM|DATE|日期|提单号|主单号|船\s*公\s*司|承运人|船\s*期|"
    r"中转港(?:代码|（卸港）|\(卸港\))?|要求进港时间|件数|毛重|体积)\s*[：:])",
    re.IGNORECASE,
)


_BARE_COMPANY_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·&.-]{2,80}(?:有限责任公司|股份有限公司|有限公司|公司)"
)


# 「公司全称+单据类型」复合抬头的词尾，如 启胜…有限公司集卡委托书、
# 嘉兴新捷…有限公司车队装箱通知单。前缀组可迭代组合（车队+装箱+通知单）。
_HEADER_DOC_TITLE_SUFFIX_RE = re.compile(
    r"(?:门点|集卡|集装箱|拖车|派车|车队|运输|内装|做箱|装箱|装柜|订舱|配舱|出运|货物)*"
    r"(?:委托书|托书|委托单|通知单|通知书|通知|派车单)$"
)


_CARRIER_PREFIXES = {
    "ONEY": "ONE",
    "HLCU": "HLC",
    "HDMU": "HMM",
    "MAEU": "MSK",
    "MSKU": "MSK",
    "MRKU": "MSK",
    "MSCU": "MSC",
    "MEDU": "MSC",
    "COSU": "COSCO",
    "CSNC": "COSCO",
    "OOLU": "OOCL",
    "CMDU": "CMA",
    "APLU": "CMA",
    "EGLV": "EMC",
    "EMCU": "EMC",
    "YMLU": "YML",
    "YMJA": "YML",
    "KKLU": "KLINE",
    "SITC": "SITC",
    "SITG": "SITC",
    "WHLC": "WHL",
    "WHSU": "WHL",
    "ZIMU": "ZIM",
    "PABV": "PIL",
    "PILU": "PIL",
    "TSLU": "TSL",
    "RCLU": "RCL",
    "RCLB": "RCL",
    "KMTU": "HEUNG",
    "HEUN": "HEUNG",
    "SNKO": "KMTC",
    "KMDU": "KMTC",
    "PCIU": "PANCON",
    "MATS": "MATSON",
    "SUDU": "HAMSUD",
    "HJSC": "HJS",
    "KFLB": "KAWA",
    "KFLS": "KAWA",
    "KFLU": "KAWA",
    "PDLU": "PDL",
    "PELS": "PDL",
    "DNPO": "DONGJIN",
    "TGSH": "TGL",
}


_ADJACENT_FIELD_LABELS = frozenset(
    normalize_label(label)
    for label in (
        "承运人",
        "船公司",
        "船东",
        "CARRIER",
        "船期",
        "开航日期",
        "开航日",
        "ETD",
        "中转港",
        "中转港代码",
        "中转港（卸港）",
        "中转港(卸港)",
        "转运港",
        "转运港代码",
        "卸港",
        "目的港",
        "卸货港",
        "目的地",
        "交货地",
        "起运港",
        "启运港",
        "装货港",
        "港区",
        "码头",
        "要求进港时间",
        "要求进港",
    )
)


# 表格同行扫描时必须当作“字段标签”而非值的词：含相邻字段标签集与常见
# 流程/导航类标签（启胜集卡委托书等模板的出货流程栏）。命中即停止扫描，
# 避免把隔壁字段的标签当成取值。
_FIELD_LABEL_VOCAB = frozenset(
    normalize_label(label)
    for label in (
        "截单时间",
        "截单",
        "截关",
        "截关时间",
        "截报关",
        "截信息",
        "提单状态",
        "上船时间",
        "进港查询",
        "海关放行",
        "码放/配载",
        "出货流程",
        "装箱总数据",
        "宁波一代代码",
        "中文品名",
        "货物种类",
        "分类",
        "客户代码",
        "我司编号",
        "关单号",
        "箱型箱量",
        "船名航次",
        "联系人",
        "设备单",
        "查开港装",
        "BOOKING NO",
        "做箱时间",
        "做箱日期",
        "装箱时间",
        "装箱日期",
        "拆装箱日期",
        "装柜时间",
        "装柜日期",
        "备注",
        "件数",
        "毛重",
        "体积",
        "净重",
        "箱型",
        "箱量",
        "总箱量",
        "货名",
        "唛头",
        "集装箱号",
        "封号",
        "柜型",
        "提货方式",
        "送货地址",
        "委托件数",
        "委托毛重",
        "委托体积",
        "进仓编号",
        "通知日期",
        "开航日",
        "船公司",
        "中转代码",
        "目的港代码",
        "卸货港代码",
    )
) | _ADJACENT_FIELD_LABELS


# 常见字段标签词尾：前向扫描遇到未入词表的裸标签时按词尾兜底识别，
# 宁可停止扫描也不把隔壁字段的标签当值。
_FIELD_LABEL_SUFFIX_RE = re.compile(
    r"(?:代码|编号|港|时间|日期|号|量|数|重|体积|类型|名|方式|地址|状态)$"
)


def _looks_like_field_label(value: str) -> bool:
    """值是否形如字段标签（词表命中或常见标签词尾）。"""
    normalized = normalize_label(value)
    if normalized in _FIELD_LABEL_VOCAB:
        return True
    return bool(_FIELD_LABEL_SUFFIX_RE.search(normalized))


def _field_value(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _person_name_is_in_source(value: str, source_text: str) -> bool:
    if value in source_text:
        return True
    parts = [part.strip() for part in re.split(r"[/,，、;；|]+", value) if part.strip()]
    return bool(parts) and all(part in source_text for part in parts)


def _value_grounded_in_source(value: str, source_text: str) -> bool:
    """值能否在原文中找到逐字依据（忽略空白/连字符/<br>断开与大小写差异）。

    提单号等字母数字值不含连字符，原文中的 `-`/`<br>` 只能是换行/分段残留；
    比较时从两侧剔除，避免跨行拆开的真值被误判为无据。纯字母数字值要求
    原文中存在完全相等的独立 token，防止真值前缀/子串被误放行。
    """
    compact_value = re.sub(r"<br\s*/?>|[\s-]+", "", value).casefold()
    if not compact_value:
        return False
    compact_source = re.sub(r"<br\s*/?>|[\s-]+", "", source_text).casefold()
    if re.fullmatch(r"[a-z0-9]+", compact_value):
        # 字母数字值（提单号/编号）：原文独立 token 完全相等才算有据
        return compact_value in set(re.findall(r"[a-z0-9]+", compact_source))
    return compact_value in compact_source


def _carrier_from_mbl(mbl_no: Any) -> str | None:
    if not isinstance(mbl_no, str):
        return None
    normalized = re.sub(r"\s+", "", mbl_no).upper()
    for prefix in sorted(_CARRIER_PREFIXES, key=len, reverse=True):
        if normalized.startswith(prefix):
            return _CARRIER_PREFIXES[prefix]
    return None


def _extract_labeled_value(source_text: str, labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    value_pattern = r"([A-Za-z0-9][A-Za-z0-9._/-]*)"
    for separator in (r"\s*[：:]\s*", r"\s*\|\s*"):
        match = re.search(rf"(?:{label_pattern}){separator}{value_pattern}", source_text)
        if match:
            return match.group(1)
    return None


def _append_issue(
    issues: list[dict[str, Any]],
    *,
    code: str,
    field: str,
    message: str,
    source_values: list[str] | None = None,
    blocking: bool = True,
) -> None:
    for issue in issues:
        if _review_issue_key(issue) == (code, field):
            issue["field"] = field
            issue["blocking"] = bool(issue.get("blocking", True)) or blocking
            issue["message"] = message
            if source_values:
                existing_values = issue.get("source_values")
                merged_values = list(existing_values) if isinstance(existing_values, list) else []
                merged_values.extend(value for value in source_values if value not in merged_values)
                issue["source_values"] = merged_values
            else:
                issue.setdefault("source_values", [])
            return
    issues.append(
        {
            "code": code,
            "field": field,
            "message": message,
            "source_values": source_values or [],
            "blocking": blocking,
        }
    )


def _review_issue_key(issue: dict[str, Any]) -> tuple[Any, Any]:
    return issue.get("code"), issue.get("field")


def _merged_review_issue_field(left: Any, right: Any) -> str:
    if left == right and isinstance(left, str) and left:
        return left
    if isinstance(left, str) and isinstance(right, str):
        left_root = re.split(r"[.[]", left, maxsplit=1)[0]
        right_root = re.split(r"[.[]", right, maxsplit=1)[0]
        if left_root and left_root == right_root:
            return left_root
    return "multiple_fields"


def _deduplicate_review_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge issues by code, matching the public one-entry-per-code contract."""
    deduplicated: list[dict[str, Any]] = []
    for issue in issues:
        code = issue.get("code")
        if not isinstance(code, str) or not code:
            continue
        existing = next((item for item in deduplicated if item.get("code") == code), None)
        if existing is None:
            copied = dict(issue)
            copied["source_values"] = list(issue.get("source_values") or [])
            deduplicated.append(copied)
            continue
        existing["field"] = _merged_review_issue_field(
            existing.get("field"), issue.get("field")
        )
        existing["blocking"] = bool(existing.get("blocking", True)) or bool(
            issue.get("blocking", True)
        )
        values = existing.setdefault("source_values", [])
        for value in issue.get("source_values") or []:
            if value not in values:
                values.append(value)
    return deduplicated


def _remove_issue(issues: list[dict[str, Any]], *, code: str, field: str) -> None:
    issues[:] = [
        issue
        for issue in issues
        if not (issue.get("code") == code and issue.get("field") == field)
    ]


def _clean_labeled_value(value: str) -> str | None:
    cleaned = _INLINE_LABEL_BOUNDARY_RE.split(value, maxsplit=1)[0]
    cleaned = cleaned.strip().strip("*_` ").strip()
    # 表格单元格内换行被转换器转成 <br>：值首尾的 <br> 是标签与值的分隔残留，
    # 中间的 <br> 保留（可能是值本身的组成部分）
    cleaned = re.sub(r"^(?:\s*<br>\s*)+", "", cleaned)
    cleaned = re.sub(r"(?:\s*<br>\s*)+$", "", cleaned)
    return cleaned if cleaned and cleaned not in {"-", "/"} else None


def _label_pattern(label: str) -> str:
    # Chinese forms often insert spaces purely for visual alignment.
    return r"\s*".join(re.escape(char) for char in label if not char.isspace())


def _is_adjacent_field_label(value: str) -> bool:
    return normalize_label(value) in _ADJACENT_FIELD_LABELS


def _is_field_label_cell(value: str) -> bool:
    """判断单元格是否为字段标签：已知相邻字段标签，或以冒号结尾的标签样式。

    表头行（如 `| 中转港代码： | 交货地： |`）中标签后的下一个单元格
    仍是标签而不是值，此时应从下一行同列取值，避免把表头标签当值。
    """
    return _is_adjacent_field_label(value) or bool(re.search(r"[：:]\s*$", value.strip()))


def _extract_explicit_values(source_text: str, labels: tuple[str, ...]) -> list[str]:
    """Extract values only when a body label and value are directly adjacent."""
    label_pattern = "|".join(_label_pattern(label) for label in labels)
    pattern = re.compile(
        rf"(?:^|(?<=\s))(?:{label_pattern})\s*(?:[：:]\s*|\|\s*)([^|\n]+)",
        re.IGNORECASE,
    )
    values: list[str] = []
    expected_labels = {normalize_label(label) for label in labels}
    html_rows = parse_html_table_rows(source_text)
    for row_index, html_cells in enumerate(html_rows):
        for index, cell in enumerate(html_cells[:-1]):
            if normalize_label(cell) not in expected_labels:
                continue
            next_cell = html_cells[index + 1]
            value = None if _is_field_label_cell(next_cell) else _clean_labeled_value(next_cell)
            if not value:
                # 同行下一列是标签（表头行）或空单元格：值在下一行同列
                for next_row in html_rows[row_index + 1 :]:
                    if index < len(next_row):
                        value = _clean_labeled_value(next_row[index])
                    break
            if value and value not in values:
                values.append(value)
    for raw_line in source_text.splitlines():
        line = _MARKDOWN_PARAGRAPH_PREFIX_RE.sub("", raw_line.strip())
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        candidates = cells if len(cells) > 1 else [line]
        for index, candidate in enumerate(candidates):
            match = pattern.search(candidate)
            value = _clean_labeled_value(match.group(1)) if match else None
            if value is None and normalize_label(candidate) in expected_labels:
                if index + 1 < len(cells) and not _is_field_label_cell(cells[index + 1]):
                    value = _clean_labeled_value(cells[index + 1])
            if value and value not in values:
                values.append(value)
    return values


def _is_label_style_line(line: str) -> bool:
    """展平段落流中，含冒号的行视为标签/键值行而非候选值。"""
    return "：" in line or ":" in line


def _is_converter_structure_line(line: str) -> bool:
    """转换器生成的结构行（文本框标题、来源标记等），不携带文档内容。"""
    return bool(_CONVERTER_STRUCTURE_LINE_RE.match(line))


def _extract_separated_label_values(
    source_text: str, labels: tuple[str, ...], *, max_gap: int = 8
) -> list[str]:
    """从表格被展平的段落流中提取标签与值分离的候选值。

    textutil 等工具把老式 .doc 表格展平为 `_pN_` 段落流，标签带与值带
    分离：标签段后可能隔着多个空段（含 `(empty)` 段）才是值段。
    仅当标签后的第一个非空段不像标签行时，才把它当作值。
    """
    expected = {normalize_label(label) for label in labels}
    lines = [
        _MARKDOWN_PARAGRAPH_PREFIX_RE.sub("", raw.strip())
        for raw in source_text.splitlines()
    ]
    values: list[str] = []
    for index, line in enumerate(lines):
        text = line.strip()
        if not text or normalize_label(text.rstrip("：:")) not in expected:
            continue
        gap = 0
        for next_line in lines[index + 1 :]:
            next_text = next_line.strip()
            if not next_text or _is_converter_structure_line(next_text):
                gap += 1
                continue
            if gap > max_gap:
                break
            if _is_label_style_line(next_text) or normalize_label(
                next_text.rstrip("：:")
            ) in expected:
                break
            value = _clean_labeled_value(next_text)
            if value and value not in values:
                values.append(value)
            break
    return values


def _extract_table_column_values(source_text: str, labels: tuple[str, ...]) -> list[str]:
    """Read a value from the next row in a labeled Markdown table column."""
    expected = {normalize_label(label) for label in labels}
    rows: list[list[str]] = []
    values: list[str] = []

    def consume_table() -> None:
        for row_index, row in enumerate(rows[:-1]):
            for column_index, cell in enumerate(row):
                if normalize_label(cell) not in expected:
                    continue
                value = None
                # 同行向后扫描：跳过空单元格取第一个非空值；遇到字段标签
                # （词表命中或常见标签词尾）说明进入隔壁字段区，停止扫描
                for candidate in row[column_index + 1 :]:
                    if not candidate.strip():
                        continue
                    if _looks_like_field_label(candidate):
                        break
                    value = _clean_labeled_value(candidate)
                    break
                if not value:
                    # 同行无值（表头行或空值带）：取下一行同列
                    for next_row in rows[row_index + 1 :]:
                        if is_separator_row(next_row):
                            continue
                        if column_index < len(next_row):
                            value = _clean_labeled_value(next_row[column_index])
                        break
                if value and value not in values:
                    values.append(value)

    for raw_line in [*source_text.splitlines(), ""]:
        line = raw_line.strip()
        if line.startswith("|") and line.endswith("|"):
            rows.append(pipe_row_cells(line))
            continue
        if rows:
            consume_table()
            rows = []
    for raw_table in re.findall(
        r"<table\b[^>]*>(.*?)</table>", source_text, re.IGNORECASE | re.DOTALL
    ):
        rows = parse_html_table_rows(raw_table)
        consume_table()
    return values


def _extract_explicit_carriers(source_text: str) -> list[str]:
    labels = ("承运人", "船公司", "船东", "CARRIER")
    values = _extract_explicit_values(source_text, labels)
    values.extend(
        value
        for value in _extract_table_column_values(source_text, labels)
        if value not in values
    )
    return values


def _extract_fragmented_dates(source_text: str) -> list[str]:
    """Rejoin MinerU cells such as ``日期：20`` + ``21.5.28``."""
    values: list[str] = []
    label_pattern = re.compile(r"(?:日期|DATE)\s*[：:]?\s*(\d{2})$", re.IGNORECASE)
    remainder_pattern = re.compile(r"^(\d{2})[./-](\d{1,2})[./-](\d{1,2})$")
    for raw_line in source_text.splitlines():
        line = _MARKDOWN_PARAGRAPH_PREFIX_RE.sub("", raw_line.strip())
        cells = [cell.strip().strip("*_` ") for cell in line.strip("|").split("|")]
        for index, cell in enumerate(cells[:-1]):
            prefix = label_pattern.search(cell)
            remainder = remainder_pattern.fullmatch(cells[index + 1])
            if not prefix or not remainder:
                continue
            parsed = parse_date_or_none(
                f"{prefix.group(1)}{remainder.group(1)}.{remainder.group(2)}.{remainder.group(3)}",
                allow_time=False,
            )
            if parsed and parsed not in values:
                values.append(parsed)
    return values


def _header_company_candidates(source_text: str) -> list[str]:
    """Find bare company names in the compact document header area.

    纯公司名行直接匹配；「公司全称+单据类型」复合抬头（如
    ``启胜…有限公司集卡委托书``）剥离词尾后再按纯公司名匹配。
    """
    lines: list[str] = []
    for raw_line in source_text.splitlines():
        line = _MARKDOWN_PARAGRAPH_PREFIX_RE.sub("", raw_line.strip())
        line = line.lstrip("# ").strip().strip("*_` ")
        if line:
            lines.append(line)
        if len(lines) >= 6:
            break
    candidates: list[str] = []
    for line in lines:
        if _BARE_COMPANY_RE.fullmatch(line):
            candidates.append(line)
            continue
        stripped = _HEADER_DOC_TITLE_SUFFIX_RE.sub("", line).strip()
        # 剥离后必须仍是纯公司名，否则宁可丢弃也不猜
        if stripped != line and _BARE_COMPANY_RE.fullmatch(stripped):
            candidates.append(stripped)
    return list(dict.fromkeys(candidates))


def _customer_notice_heading_candidates(source_text: str) -> list[str]:
    """Read a customer prefix from compact headings such as ``海丰装箱通知``."""
    values: list[str] = []
    inspected = 0
    for raw_line in source_text.splitlines():
        line = _MARKDOWN_PARAGRAPH_PREFIX_RE.sub("", raw_line.strip())
        line = line.lstrip("# ").strip().strip("*_` ")
        if not line:
            continue
        inspected += 1
        match = re.fullmatch(r"([^|：:\n]{2,40}?)(?:装箱|做箱)通知(?:书)?", line)
        if match:
            values.append(match.group(1).strip())
        if inspected >= 8:
            break
    return list(dict.fromkeys(values))


def _recipient_row_extras(source_text: str) -> list[str]:
    labels = {"to", "致", "attn", "收件方", "收件人"}
    values: list[str] = []
    for raw_line in source_text.splitlines():
        line = raw_line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [cell.strip().strip("*_` ") for cell in line.strip("|").split("|")]
        for index, cell in enumerate(cells[:-2]):
            if cell.rstrip("：:").strip().lower() not in labels:
                continue
            for extra in cells[index + 2 :]:
                cleaned = _clean_labeled_value(extra)
                if cleaned and cleaned not in values:
                    values.append(cleaned)
    return values


def _append_top_level_remark(data: dict[str, Any], values: list[str]) -> None:
    parts = [part.strip() for part in re.split(r"[；;\n]+", data.get("remark") or "") if part.strip()]
    for value in values:
        if value not in parts:
            parts.append(value)
    data["remark"] = "；".join(parts) or None


def _remove_remark_clauses_containing(data: dict[str, Any], value: str) -> None:
    targets: list[dict[str, Any]] = [data]
    containers = data.get("containers")
    if isinstance(containers, list):
        targets.extend(container for container in containers if isinstance(container, dict))
    for target in targets:
        remark = target.get("remark")
        if not isinstance(remark, str) or value not in remark:
            continue
        clauses = [part.strip() for part in re.split(r"[；;\n]+", remark) if part.strip()]
        target["remark"] = "；".join(part for part in clauses if value not in part) or None
