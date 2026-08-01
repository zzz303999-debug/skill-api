"""托书结果后处理的公共工具层：issue 管理、显式字段提取与候选识别。"""

from __future__ import annotations

import re
from typing import Any

from .normalizer import (
    is_separator_row,
    normalize_label,
    parse_date_or_none,
    parse_html_table_rows,
    pipe_row_cells,
)

_MARKDOWN_PARAGRAPH_PREFIX_RE = re.compile(
    r"^_(?:p|l)\d+(?:: \(empty\))?_\s*", re.IGNORECASE
)


_INLINE_LABEL_BOUNDARY_RE = re.compile(
    r"\s+(?=(?:TO|致|ATTN|FROM|FM|DATE|日期|提单号|主单号|船\s*公\s*司|承运人|船\s*期|"
    r"中转港(?:代码|（卸港）|\(卸港\))?|要求进港时间|件数|毛重|体积)\s*[：:])",
    re.IGNORECASE,
)


_BARE_COMPANY_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·&.-]{2,80}(?:有限责任公司|股份有限公司|有限公司|公司)"
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
        "卸港",
        "港区",
        "码头",
        "要求进港时间",
        "要求进港",
    )
)


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
    return cleaned if cleaned and cleaned not in {"-", "/"} else None


def _label_pattern(label: str) -> str:
    # Chinese forms often insert spaces purely for visual alignment.
    return r"\s*".join(re.escape(char) for char in label if not char.isspace())


def _is_adjacent_field_label(value: str) -> bool:
    return normalize_label(value) in _ADJACENT_FIELD_LABELS


def _extract_explicit_values(source_text: str, labels: tuple[str, ...]) -> list[str]:
    """Extract values only when a body label and value are directly adjacent."""
    label_pattern = "|".join(_label_pattern(label) for label in labels)
    pattern = re.compile(
        rf"(?:^|(?<=\s))(?:{label_pattern})\s*(?:[：:]\s*|\|\s*)([^|\n]+)",
        re.IGNORECASE,
    )
    values: list[str] = []
    expected_labels = {normalize_label(label) for label in labels}
    for html_cells in parse_html_table_rows(source_text):
        for index, cell in enumerate(html_cells[:-1]):
            if normalize_label(cell) not in expected_labels:
                continue
            next_cell = html_cells[index + 1]
            value = (
                None
                if _is_adjacent_field_label(next_cell)
                else _clean_labeled_value(next_cell)
            )
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
                if index + 1 < len(cells) and not _is_adjacent_field_label(cells[index + 1]):
                    value = _clean_labeled_value(cells[index + 1])
            if value and value not in values:
                values.append(value)
    return values


def _is_label_style_line(line: str) -> bool:
    """展平段落流中，含冒号的行视为标签/键值行而非候选值。"""
    return "：" in line or ":" in line


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
            if not next_text:
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
                if column_index + 1 < len(row) and not _is_adjacent_field_label(
                    row[column_index + 1]
                ):
                    value = _clean_labeled_value(row[column_index + 1])
                    if value and value not in values:
                        values.append(value)
                    continue
                for next_row in rows[row_index + 1 :]:
                    if is_separator_row(next_row):
                        continue
                    if column_index < len(next_row):
                        value = _clean_labeled_value(next_row[column_index])
                        if value and value not in values:
                            values.append(value)
                    break

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
    """Find bare company names in the compact document header area."""
    lines: list[str] = []
    for raw_line in source_text.splitlines():
        line = _MARKDOWN_PARAGRAPH_PREFIX_RE.sub("", raw_line.strip())
        line = line.lstrip("# ").strip().strip("*_` ")
        if line:
            lines.append(line)
        if len(lines) >= 6:
            break
    return list(dict.fromkeys(line for line in lines if _BARE_COMPANY_RE.fullmatch(line)))


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
