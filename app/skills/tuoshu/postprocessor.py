"""托书结果的确定性订单映射与人工复核校验。"""

from __future__ import annotations

import html
import re
from datetime import date
from typing import Any

from .normalizer import (
    is_known_container_type,
    normalize_container_type,
    normalize_review_issues,
)

_PERSON_FIELDS = ("sender", "sender_contact", "factory.contact")
_TRANSIT_LOOKUP_VALUES = (
    "进港代码请参照设备交接单",
    "设备交接单为准",
    "见设备交接单",
    "见设备单",
    "见设备",
    "见 EIR",
    "见设",
)
_CONFLICT_MARKERS = ("数据冲突", "数值冲突", "另有记录", "另一组", "两组数据", "待人工确认")
_MEASUREMENT_LABELS = {
    "packages": ("件数", "包装件数", "packages"),
    "volume_cbm": ("体积", "volume", "cbm"),
}
_ALWAYS_BLOCKING_CODES = {
    "deterministic_ai_conflict",
    "template_mismatch",
    "mineru_failed",
    "mineru_low_confidence",
    "confirmed_ocr_artifact",
    "ungrounded_text",
    "sender_contact_not_from_from_field",
    "carrier_prefix_mismatch",
    "conflicting_container_data",
    "missing_container_measurements",
    "conflicting_transit_port",
    "missing_factory_name",
    "missing_shipper_company",
    "hbl_misclassified_as_mbl",
    "hbl_no_not_verbatim",
    "person_name_not_verbatim",
    "person_name_unverified",
    "conflicting_shipper_company",
    "shipper_company_not_from_explicit_field",
    "invalid_container_no",
    "invalid_seal_no",
    "container_no_not_verbatim",
    "seal_no_not_verbatim",
    "carrier_by_mbl",
    "carrier_by_vessel",
    "carrier_source_unverified",
}
_GROUNDING_WRAPPERS = ("另有记录", "主值", "待人工确认")
_CONFIRMED_OCR_ARTIFACT_PATTERNS = (
    (re.compile(r"\s*作业\s*资水\s*[！!]?"), "作业资水"),
)
_CONTAINER_NO_RE = re.compile(r"^[A-Z]{4}\d{7}$")
_SEAL_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./-]{3,19}$")
_MARKDOWN_PARAGRAPH_PREFIX_RE = re.compile(
    r"^_(?:p|l)\d+(?:: \(empty\))?_\s*", re.IGNORECASE
)
_INLINE_LABEL_BOUNDARY_RE = re.compile(
    r"\s+(?=(?:TO|致|ATTN|FROM|FM|DATE|日期)\s*[：:])", re.IGNORECASE
)
_BARE_COMPANY_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·&.-]{2,80}(?:有限责任公司|股份有限公司|有限公司|公司)"
)
_REVIEW_ISSUE_PRIORITY = {
    "missing_container_measurements": 10,
    "missing_shipper_company": 20,
    "missing_factory_name": 30,
    "carrier_by_mbl": 40,
    "carrier_by_vessel": 40,
    "carrier_source_unverified": 40,
}
_PACKAGE_UNIT_ALIASES = {
    "CTN": "CTNS",
    "CTNS": "CTNS",
    "CARTON": "CARTONS",
    "CARTONS": "CARTONS",
    "PKG": "PKGS",
    "PKGS": "PKGS",
    "PACKAGE": "PACKAGES",
    "PACKAGES": "PACKAGES",
    "PC": "PCS",
    "PCS": "PCS",
}
_CONTAINER_TYPE_TOKEN = (
    r"\d{2}\s*['’]?\s*"
    r"(?:GENERAL|OPENTOP|OPEN\s*TOP|TANK|FLAT|REF|NOR|GP|DV|DC|HC|HQ|RF|RH|OT|FR|TK|SD|H)"
)
_CONTAINER_TYPE_QTY_PATTERNS = (
    re.compile(
        rf"(?P<type>{_CONTAINER_TYPE_TOKEN})\s*(?:[×xX*])\s*(?P<qty>\d+)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<qty>\d+)\s*(?:[×xX*])\s*(?P<type>{_CONTAINER_TYPE_TOKEN})",
        re.IGNORECASE,
    ),
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


def _restore_indexed_container_remarks(data: dict[str, Any]) -> None:
    remark = data.get("remark")
    containers = data.get("containers")
    if not isinstance(remark, str) or not isinstance(containers, list):
        return
    pattern = re.compile(r"柜\s*(\d+)\s*(?:的)?\s*(?:备注)?\s*[：:]\s*([^；\n|]+)")
    for match in pattern.finditer(remark):
        index = int(match.group(1)) - 1
        if not 0 <= index < len(containers):
            continue
        container = containers[index]
        if isinstance(container, dict) and not container.get("remark"):
            container["remark"] = match.group(2).strip()


def _extract_labeled_value(source_text: str, labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    value_pattern = r"([A-Za-z0-9][A-Za-z0-9._/-]*)"
    for separator in (r"\s*[：:]\s*", r"\s*\|\s*"):
        match = re.search(rf"(?:{label_pattern}){separator}{value_pattern}", source_text)
        if match:
            return match.group(1)
    return None


def _has_explicit_master_bill(source_text: str) -> bool:
    return bool(
        re.search(
            r"(?:主提单号|主单号|(?<![子分])提单号)\s*(?:[：:]|\|)",
            source_text,
        )
    )


def _restore_explicit_hbl(
    data: dict[str, Any], source_text: str, issues: list[dict[str, Any]]
) -> None:
    explicit_hbl = _extract_labeled_value(
        source_text,
        ("子提单号", "子单号", "分提单号", "分单号", "HBL NO", "HB/L"),
    )
    current_hbl = data.get("hbl_no")
    if explicit_hbl:
        if isinstance(current_hbl, str) and current_hbl != explicit_hbl:
            _append_issue(
                issues,
                code="hbl_no_not_verbatim",
                field="hbl_no",
                message="子提单号与原文明示值不一致，已按原文修正，需人工确认",
                source_values=[current_hbl, explicit_hbl],
            )
        data["hbl_no"] = explicit_hbl

        current_mbl = data.get("mbl_no")
        if current_mbl == explicit_hbl and not _has_explicit_master_bill(source_text):
            data["mbl_no"] = None
            _append_issue(
                issues,
                code="hbl_misclassified_as_mbl",
                field="mbl_no",
                message="子提单号被误填为主提单号，已清空错误回退，需人工确认",
                source_values=[explicit_hbl],
            )
    elif isinstance(current_hbl, str) and current_hbl and current_hbl not in source_text:
        data["hbl_no"] = None
        _append_issue(
            issues,
            code="hbl_no_not_verbatim",
            field="hbl_no",
            message="子提单号未在原文逐字出现，已清空并需人工确认",
            source_values=[current_hbl],
        )


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


def _deduplicate_review_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge semantically identical issues without hiding distinct affected fields."""
    deduplicated: list[dict[str, Any]] = []
    for issue in issues:
        code = issue.get("code")
        if not isinstance(code, str) or not code:
            continue
        key = _review_issue_key(issue)
        existing = next((item for item in deduplicated if _review_issue_key(item) == key), None)
        if existing is None:
            copied = dict(issue)
            copied["source_values"] = list(issue.get("source_values") or [])
            deduplicated.append(copied)
            continue
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


def _extract_explicit_values(source_text: str, labels: tuple[str, ...]) -> list[str]:
    """Extract values only when a body label and value are directly adjacent."""
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(
        rf"(?:^|(?<=\s))(?:{label_pattern})\s*(?:[：:]\s*|\|\s*)([^|\n]+)",
        re.IGNORECASE,
    )
    values: list[str] = []
    decoded_source = html.unescape(source_text)
    html_cells = [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", decoded_source, re.IGNORECASE | re.DOTALL)
    ]
    expected_labels = {label.lower() for label in labels}
    for index, cell in enumerate(html_cells[:-1]):
        if cell.rstrip("：:").strip().lower() not in expected_labels:
            continue
        value = _clean_labeled_value(html_cells[index + 1])
        if value and value not in values:
            values.append(value)
    for raw_line in source_text.splitlines():
        line = _MARKDOWN_PARAGRAPH_PREFIX_RE.sub("", raw_line.strip())
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        candidates = cells if len(cells) > 1 else [line]
        for index, candidate in enumerate(candidates):
            match = pattern.search(candidate)
            value = _clean_labeled_value(match.group(1)) if match else None
            if value is None and candidate.rstrip("：:").strip().lower() in expected_labels:
                value = _clean_labeled_value(cells[index + 1]) if index + 1 < len(cells) else None
            if value and value not in values:
                values.append(value)
    return values


def _extract_table_column_values(source_text: str, labels: tuple[str, ...]) -> list[str]:
    """Read a value from the next row in a labeled Markdown table column."""
    expected = {label.lower() for label in labels}
    rows: list[list[str]] = []
    values: list[str] = []

    def consume_table() -> None:
        for row_index, row in enumerate(rows[:-1]):
            for column_index, cell in enumerate(row):
                if cell.rstrip("：:").strip().lower() not in expected:
                    continue
                for next_row in rows[row_index + 1 :]:
                    if all(re.fullmatch(r":?-{3,}:?", part.replace(" ", "")) for part in next_row):
                        continue
                    if column_index < len(next_row):
                        value = _clean_labeled_value(next_row[column_index])
                        if value and value not in values:
                            values.append(value)
                    break

    for raw_line in [*source_text.splitlines(), ""]:
        line = raw_line.strip()
        if line.startswith("|") and line.endswith("|"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
            continue
        if rows:
            consume_table()
            rows = []
    return values


def _parse_explicit_date(value: str, *, allow_time: bool) -> str | None:
    suffix = r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?" if allow_time else ""
    match = re.search(
        rf"(\d{{4}})\s*(?:[./-]\s*|年\s*)(\d{{1,2}})\s*"
        rf"(?:[./-]\s*|月\s*)(\d{{1,2}})(?:\s*日)?{suffix}",
        value,
    )
    if not match:
        return None
    year, month, day = (int(match.group(index)) for index in range(1, 4))
    try:
        date_value = date(year, month, day).isoformat()
        if not allow_time or match.group(4) is None:
            return date_value
        hour = int(match.group(4))
        minute = int(match.group(5))
        second = int(match.group(6) or 0)
        if hour > 23 or minute > 59 or second > 59:
            return None
        return f"{date_value}T{hour:02d}:{minute:02d}:{second:02d}"
    except (TypeError, ValueError):
        return None


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
            parsed = _parse_explicit_date(
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


def _restore_explicit_header_fields(data: dict[str, Any], source_text: str) -> None:
    recipients = _extract_explicit_values(
        source_text, ("TO", "致", "ATTN", "收件方", "收件人")
    )
    if len(recipients) == 1:
        data["recipient"] = recipients[0]
        _remove_remark_clauses_containing(data, recipients[0])
        _append_top_level_remark(data, _recipient_row_extras(source_text))

    doc_dates = {
        parsed
        for value in _extract_explicit_values(source_text, ("日期", "DATE"))
        if (parsed := _parse_explicit_date(value, allow_time=False))
    }
    doc_dates.update(_extract_fragmented_dates(source_text))
    if len(doc_dates) == 1:
        data["doc_date"] = doc_dates.pop()

    customer_refs = list(
        dict.fromkeys(
            match.group(1)
            for match in re.finditer(
                r"(?:客户订单号|箱单注明订单号)\s*[：:]?\s*([A-Za-z0-9][A-Za-z0-9._/-]*)",
                source_text,
                re.IGNORECASE,
            )
        )
    )
    if len(customer_refs) == 1:
        data["customer_ref"] = customer_refs[0]

    explicit_agents = _extract_explicit_values(
        source_text, ("委托公司", "我方公司", "发件方", "SHIPPER AGENT")
    )
    agent_candidates = explicit_agents or _header_company_candidates(source_text)
    if len(agent_candidates) == 1:
        data["shipper_agent"] = agent_candidates[0]
        _remove_remark_clauses_containing(data, agent_candidates[0])

    loading_values = _extract_explicit_values(
        source_text,
        ("做箱时间", "做箱日期", "装箱时间", "装箱日期", "拆装箱日期", "装柜时间", "装柜日期"),
    )
    loading_values.extend(
        value for value in _extract_table_column_values(
            source_text,
            (
                "时间",
                "做箱时间",
                "做箱日期",
                "装箱时间",
                "装箱日期",
                "拆装箱日期",
                "装柜时间",
                "装柜日期",
            ),
        )
        if value not in loading_values
    )
    loading_times = {
        parsed
        for value in loading_values
        if (parsed := _parse_explicit_date(value, allow_time=True))
    }
    if len(loading_times) == 1:
        data["loading_time"] = loading_times.pop()

    etd_values = _extract_explicit_values(
        source_text, ("船期", "开航日期", "开航日", "ETD")
    )
    etd_values.extend(
        value
        for value in _extract_table_column_values(
            source_text, ("船期", "开航日期", "开航日", "ETD")
        )
        if value not in etd_values
    )
    etd_dates = {
        parsed
        for value in etd_values
        if (parsed := _parse_explicit_date(value, allow_time=False))
    }
    if len(etd_dates) == 1:
        data["etd"] = etd_dates.pop()


def _restore_numbered_notice_remark(data: dict[str, Any], source_text: str) -> None:
    lines = [
        _MARKDOWN_PARAGRAPH_PREFIX_RE.sub("", raw_line.strip()).strip()
        for raw_line in source_text.splitlines()
    ]
    try:
        notice_index = next(index for index, line in enumerate(lines) if line.startswith("请注意"))
    except StopIteration:
        return

    numbered: list[str] = []
    for line in lines[notice_index + 1 :]:
        if not line:
            continue
        if line.startswith("谢谢"):
            break
        if re.match(r"^\d+[、.]", line):
            numbered.append(line)
        elif numbered:
            numbered[-1] += line

    if not numbered:
        return
    pickup = re.search(r"提箱码\s*[：:]?\s*([^\n|]+)", source_text)
    parts = [f"提箱码：{pickup.group(1).strip()}"] if pickup else []
    parts.extend(numbered)
    data["remark"] = "；".join(parts)


def _validate_sender_contact(
    data: dict[str, Any], source_text: str, issues: list[dict[str, Any]]
) -> None:
    explicit = [
        value
        for value in _extract_explicit_values(source_text, ("FROM", "FM"))
        if not _BARE_COMPANY_RE.fullmatch(value)
    ]
    current = data.get("sender_contact")
    current = current.strip() if isinstance(current, str) and current.strip() else None
    if len(explicit) == 1:
        data["sender_contact"] = explicit[0]
        return
    data["sender_contact"] = None
    if current:
        _append_issue(
            issues,
            code="sender_contact_not_from_from_field",
            field="sender_contact",
            message="发货联系人并非来自 FROM/FM 联系人栏，已清空并需人工复核",
            source_values=[current],
        )


def _validate_shipper_company(
    data: dict[str, Any], source_text: str | None, issues: list[dict[str, Any]]
) -> None:
    labels = ("托运人公司", "托运人", "发货人公司", "发货公司", "发货人", "SHIPPER")
    explicit_values = _extract_explicit_values(source_text, labels) if source_text else []
    current = data.get("shipper_company")
    current = current.strip() if isinstance(current, str) and current.strip() else None

    if len(explicit_values) == 1:
        explicit = explicit_values[0]
        data["shipper_company"] = explicit
        _remove_issue(issues, code="missing_shipper_company", field="shipper_company")
        if current and current != explicit:
            _append_issue(
                issues,
                code="shipper_company_not_from_explicit_field",
                field="shipper_company",
                message="托运人公司未取自正文明确栏位，已按正文修正，需人工确认",
                source_values=[current, explicit],
            )
        return

    data["shipper_company"] = None
    if len(explicit_values) > 1:
        _append_issue(
            issues,
            code="conflicting_shipper_company",
            field="shipper_company",
            message="正文存在多个托运人公司候选值，已清空并需人工确认",
            source_values=explicit_values,
        )
    elif current:
        _append_issue(
            issues,
            code="shipper_company_not_from_explicit_field",
            field="shipper_company",
            message="托运人公司并非来自正文明确的发货人/托运人栏位，已清空",
            source_values=[current],
        )


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


def _normalize_grounding_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    value = re.sub(
        r"(?m)^_(?:p|l)\d+(?:: \(empty\))?_\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[\s：:,，。.!！；;、|_*`\-—（）()\[\]{}]+", "", value).lower()


def _text_is_grounded(value: str, source_text: str | None) -> bool:
    if not source_text:
        return False
    normalized_source = _normalize_grounding_text(source_text)
    normalized_value = _normalize_grounding_text(value)
    if normalized_value and normalized_value in normalized_source:
        return True
    payload = value
    for wrapper in _GROUNDING_WRAPPERS:
        payload = payload.replace(wrapper, "")
    normalized_payload = _normalize_grounding_text(payload)
    return bool(normalized_payload and normalized_payload in normalized_source)


def _ground_remark_value(
    target: dict[str, Any],
    *,
    field: str,
    source_text: str | None,
    issues: list[dict[str, Any]],
) -> None:
    value = target.get("remark")
    if not isinstance(value, str) or not value.strip():
        return
    grounded: list[str] = []
    rejected: list[str] = []
    for clause in (part.strip() for part in re.split(r"[；;\n]+", value)):
        if not clause:
            continue
        cleaned_clause = re.sub(
            r"^(?:备注说明|备注|REMARKS?)\s*[：:]\s*",
            "",
            clause,
            flags=re.IGNORECASE,
        ).strip()
        if _text_is_grounded(cleaned_clause, source_text):
            grounded.append(cleaned_clause)
        else:
            rejected.append(cleaned_clause or clause)
    target["remark"] = "；".join(grounded) or None
    if rejected:
        _append_issue(
            issues,
            code="ungrounded_text",
            field=field,
            message="自由文本未在 parser 原文中找到依据，已剔除并需人工复核",
            source_values=rejected,
        )


def _ground_free_text_fields(
    data: dict[str, Any], source_text: str | None, issues: list[dict[str, Any]]
) -> None:
    _ground_remark_value(data, field="remark", source_text=source_text, issues=issues)
    containers = data.get("containers")
    if not isinstance(containers, list):
        return
    for index, container in enumerate(containers):
        if isinstance(container, dict):
            _ground_remark_value(
                container,
                field=f"containers[{index}].remark",
                source_text=source_text,
                issues=issues,
            )


def _remove_confirmed_ocr_artifacts(
    data: dict[str, Any], issues: list[dict[str, Any]]
) -> None:
    """Quarantine OCR fragments that have been visually confirmed as absent."""
    targets: list[tuple[dict[str, Any], str]] = [(data, "remark")]
    containers = data.get("containers")
    if isinstance(containers, list):
        targets.extend(
            (container, f"containers[{index}].remark")
            for index, container in enumerate(containers)
            if isinstance(container, dict)
        )

    for target, field in targets:
        value = target.get("remark")
        if not isinstance(value, str) or not value.strip():
            continue
        cleaned = value
        removed: list[str] = []
        for pattern, artifact in _CONFIRMED_OCR_ARTIFACT_PATTERNS:
            cleaned, count = pattern.subn("", cleaned)
            if count and artifact not in removed:
                removed.append(artifact)
        if not removed:
            continue
        cleaned = re.sub(r"\s+([，,。；;！!])", r"\1", cleaned).strip()
        cleaned = re.sub(r"[；;]\s*$", "", cleaned).strip()
        target["remark"] = cleaned or None
        _append_issue(
            issues,
            code="confirmed_ocr_artifact",
            field=field,
            message="已确认的 OCR 污染片段已从自由文本中剔除，需对照原图复核",
            source_values=removed,
        )


def _validate_container_identifiers(
    data: dict[str, Any], source_text: str | None, issues: list[dict[str, Any]]
) -> None:
    containers = data.get("containers")
    if not isinstance(containers, list):
        return
    rules = {
        "container_no": (_CONTAINER_NO_RE, "箱号必须为 4 个大写字母加 7 位数字"),
        "seal_no": (_SEAL_NO_RE, "封号包含空格或非法字符，或长度不合法"),
    }
    for index, container in enumerate(containers):
        if not isinstance(container, dict):
            continue
        for field, (pattern, format_message) in rules.items():
            value = container.get(field)
            if not isinstance(value, str) or not value:
                continue
            code: str | None = None
            message: str | None = None
            if not pattern.fullmatch(value):
                code = f"invalid_{field}"
                message = format_message
            elif source_text is not None and value not in source_text:
                code = f"{field}_not_verbatim"
                message = "该编号未在正文中逐字出现，已清空并需人工确认"
            if code is None or message is None:
                continue
            container[field] = None
            _remove_remark_clauses_containing(data, value)
            _append_issue(
                issues,
                code=code,
                field=f"containers[{index}].{field}",
                message=message,
                source_values=[value],
            )


def _sanitize_container_measurements(
    data: dict[str, Any], issues: list[dict[str, Any]]
) -> None:
    containers = data.get("containers")
    if not isinstance(containers, list):
        return
    for index, container in enumerate(containers):
        if not isinstance(container, dict):
            continue
        invalid: list[str] = []
        for field in ("packages", "gross_weight_kg", "volume_cbm"):
            value = container.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
                invalid.append(f"{field}={value}")
                container[field] = None
        if invalid:
            _append_issue(
                issues,
                code="missing_container_measurements",
                field=f"containers[{index}]",
                message="集装箱件数、毛重或体积包含非正数，已清空并需人工确认",
                source_values=invalid,
            )


def _restore_packages_unit(data: dict[str, Any], source_text: str | None) -> None:
    """Retain an explicit package unit even when the package count is blank."""
    if not source_text:
        return
    containers = data.get("containers")
    if not isinstance(containers, list):
        return
    missing = [
        container
        for container in containers
        if isinstance(container, dict) and not container.get("packages_unit")
    ]
    if len(missing) != 1:
        return
    units = {
        _PACKAGE_UNIT_ALIASES[match.group(1).upper()]
        for match in re.finditer(
            r"\b(CTNS?|CARTONS?|PKGS?|PACKAGES?|PCS?)\b",
            source_text,
            re.IGNORECASE,
        )
    }
    if len(units) == 1:
        missing[0]["packages_unit"] = units.pop()


def _container_type_qty_specs(value: str) -> list[tuple[str, int]]:
    specs: list[tuple[str, int]] = []
    for pattern in _CONTAINER_TYPE_QTY_PATTERNS:
        for match in pattern.finditer(value):
            spec = (
                normalize_container_type(match.group("type")),
                int(match.group("qty")),
            )
            if spec not in specs:
                specs.append(spec)
    return specs


def _is_summary_detail_container_conflict(issue: dict[str, Any]) -> bool:
    if issue.get("code") != "conflicting_container_data":
        return False
    message = str(issue.get("message") or "")
    return any(label in message for label in ("总箱量", "箱型", "箱量"))


def _prefer_explicit_detail_container(
    data: dict[str, Any], source_text: str | None, issues: list[dict[str, Any]]
) -> None:
    """Use one explicit cargo-detail ``箱型`` value over stale header totals."""
    if not source_text:
        return
    if not any(_is_summary_detail_container_conflict(issue) for issue in issues):
        return

    detail_specs = list(
        dict.fromkeys(
            spec
            for value in _extract_explicit_values(source_text, ("箱型",))
            for spec in _container_type_qty_specs(value)
        )
    )
    summary_specs = list(
        dict.fromkeys(
            spec
            for value in _extract_explicit_values(source_text, ("总箱量",))
            for spec in _container_type_qty_specs(value)
        )
    )
    if (
        len(detail_specs) != 1
        or len(summary_specs) <= 1
        or detail_specs[0] not in summary_specs
    ):
        return

    containers = data.get("containers")
    if not isinstance(containers, list) or not containers:
        return
    detail_type, detail_qty = detail_specs[0]
    matching = [
        container
        for container in containers
        if isinstance(container, dict)
        and normalize_container_type(str(container.get("type") or "")) == detail_type
    ]
    if len(matching) == 1:
        selected = matching[0]
    elif len(containers) == 1 and isinstance(containers[0], dict):
        selected = containers[0]
    else:
        return

    selected["type"] = detail_type
    selected["qty"] = detail_qty
    data["containers"] = [selected]
    issues[:] = [
        issue for issue in issues if not _is_summary_detail_container_conflict(issue)
    ]


def _source_container_types(source_text: str | None) -> list[str]:
    if not source_text:
        return []
    decoded = html.unescape(source_text)
    pattern = re.compile(
        r"(?<![A-Za-z0-9])"
        r"\d{2}\s*['’]?\s*"
        r"(?:GENERAL|OPENTOP|OPEN\s*TOP|TANK|FLAT|REF|NOR|GP|DV|DC|HC|HQ|RF|RH|OT|FR|TK|SD|H)"
        r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    values: list[str] = []
    for match in pattern.finditer(decoded):
        value = normalize_container_type(match.group(0))
        if value not in values:
            values.append(value)
    return values


def _preserve_container_types(
    data: dict[str, Any], source_text: str | None, issues: list[dict[str, Any]]
) -> None:
    containers = data.get("containers")
    if not isinstance(containers, list):
        return
    source_types = _source_container_types(source_text)
    expected_types: list[str | None]
    if len(source_types) == 1:
        expected_types = [source_types[0]] * len(containers)
    elif len(source_types) == len(containers):
        expected_types = list(source_types)
    else:
        expected_types = [None] * len(containers)
    for index, container in enumerate(containers):
        if not isinstance(container, dict):
            continue
        value = container.get("type")
        if not isinstance(value, str) or not value.strip():
            continue
        preserved = normalize_container_type(value)
        expected = expected_types[index]
        if expected and preserved != expected:
            _append_issue(
                issues,
                code="container_type_not_verbatim",
                field=f"containers[{index}].type",
                message="箱型与原文不一致，已按原文恢复",
                source_values=[preserved, expected],
                blocking=False,
            )
            preserved = expected
        container["type"] = preserved
        if not is_known_container_type(preserved):
            _append_issue(
                issues,
                code="unknown_container_type",
                field=f"containers[{index}].type",
                message="箱型不在已知识别表中，已逐字保留原文并提示人工确认",
                source_values=[preserved],
                blocking=False,
            )


def _validate_carrier_source(
    data: dict[str, Any], source_text: str | None, issues: list[dict[str, Any]]
) -> None:
    inferred_codes = {"carrier_by_mbl", "carrier_by_vessel", "carrier_source_unverified"}
    carrier = data.get("carrier")
    if not isinstance(carrier, str) or not carrier.strip():
        issues[:] = [issue for issue in issues if issue.get("code") not in inferred_codes]
        return
    carrier = carrier.strip().upper()
    data["carrier"] = carrier

    if source_text is None:
        issues[:] = [
            issue
            for issue in issues
            if issue.get("code") not in inferred_codes - {"carrier_source_unverified"}
        ]
        _append_issue(
            issues,
            code="carrier_source_unverified",
            field="carrier",
            message="视觉输入无法确定承运人是否为原文明示值，需人工确认",
            source_values=[carrier],
        )
        return

    explicit = _extract_explicit_values(source_text, ("承运人", "船公司", "CARRIER", "船东"))
    if explicit:
        issues[:] = [issue for issue in issues if issue.get("code") not in inferred_codes]
        return

    expected_carrier = _carrier_from_mbl(data.get("mbl_no"))
    vessel = data.get("vessel")
    if expected_carrier:
        code = "carrier_by_mbl"
        message = "承运人并非原文明示值，而是由主提单号前缀推断，需人工确认"
        evidence = [str(data.get("mbl_no")), carrier]
    elif isinstance(vessel, str) and vessel.strip():
        code = "carrier_by_vessel"
        message = "承运人并非原文明示值，而是由船名推断，需人工确认"
        evidence = [vessel.strip(), carrier]
    else:
        code = "carrier_source_unverified"
        message = "承运人没有原文明示栏位或可核验来源，需人工确认"
        evidence = [carrier]
    issues[:] = [
        issue for issue in issues if issue.get("code") not in inferred_codes - {code}
    ]
    _append_issue(
        issues,
        code=code,
        field="carrier",
        message=message,
        source_values=evidence,
    )


def _normalize_port_fields(data: dict[str, Any], source_text: str | None) -> None:
    for field in ("pol", "pod"):
        value = data.get(field)
        if not isinstance(value, str):
            continue
        normalized = re.sub(r"\s+", " ", value.strip().upper())
        if source_text and not re.search(r"\([^)]*\)|,", normalized):
            modifier = re.search(
                rf"(?<![A-Za-z]){re.escape(normalized)}\s*"
                r"(\(\s*[A-Za-z][A-Za-z .'-]*\s*\)(?:\s*,\s*[A-Za-z][A-Za-z .'-]*)?"
                r"|,\s*[A-Za-z][A-Za-z .'-]*)",
                source_text,
                re.IGNORECASE,
            )
            if modifier:
                normalized += modifier.group(1).upper()
        state_suffix = re.fullmatch(r"(.+?)\s*\(\s*([A-Z]{2})\s*\)", normalized)
        if state_suffix:
            normalized = f"{state_suffix.group(1).strip()}, {state_suffix.group(2)}"
        normalized = re.sub(r"\s*,\s*", ", ", normalized)
        data[field] = normalized


def _inherit_single_container_mbl(data: dict[str, Any]) -> None:
    containers = data.get("containers")
    mbl_no = data.get("mbl_no")
    if (
        isinstance(containers, list)
        and len(containers) == 1
        and isinstance(containers[0], dict)
        and isinstance(mbl_no, str)
        and mbl_no
        and not containers[0].get("mbl_no")
    ):
        containers[0]["mbl_no"] = mbl_no


def _source_mentions_measurements(source_text: str, fields: list[str]) -> bool:
    """Return whether the source exposes the measurements we are validating."""
    lowered = source_text.lower()
    return any(
        any(label.lower() in lowered for label in _MEASUREMENT_LABELS[field])
        for field in fields
    )


def _restore_missing_container_measurements(
    data: dict[str, Any], source_text: str | None, issues: list[dict[str, Any]]
) -> None:
    """Flag a container whose explicitly supplied measurements are both absent.

    Missing values remain ``null`` in the extraction JSON.  The issue is generated
    here so a renderer never has to infer a second set of review rules.
    """
    containers = data.get("containers")
    if not isinstance(containers, list):
        return
    for index, container in enumerate(containers):
        if not isinstance(container, dict):
            continue
        missing = [
            field for field in ("packages", "volume_cbm") if container.get(field) is None
        ]
        if len(missing) != 2:
            continue
        # Text documents must actually expose a measurement label.  For vision
        # input source_text is unavailable, so an empty pair is itself unverified.
        if source_text is not None and not _source_mentions_measurements(source_text, missing):
            continue
        _append_issue(
            issues,
            code="missing_container_measurements",
            field=f"containers[{index}]",
            message="集装箱件数和体积为空，需人工确认",
        )


def _build_order_note(data: dict[str, Any]) -> str | None:
    po_numbers: list[str] = []
    containers = data.get("containers")
    if isinstance(containers, list):
        for container in containers:
            if not isinstance(container, dict):
                continue
            po_no = container.get("po_no")
            if isinstance(po_no, str) and po_no and po_no not in po_numbers:
                po_numbers.append(po_no)

    parts: list[str] = []
    remark = data.get("remark")
    if isinstance(remark, str) and remark.strip():
        parts.append(remark.strip())
    if po_numbers and not all(po_no in (remark or "") for po_no in po_numbers):
        parts.append(f"PO号：{'、'.join(po_numbers)}")
    return "；".join(parts) or None


def finalize_extraction(data: dict[str, Any], *, source_text: str | None) -> dict[str, Any]:
    """补充订单映射，并把不可安全自动下单的情况转成结构化问题。"""
    raw_issues = data.get("review_issues")
    issues = [dict(issue) for issue in normalize_review_issues(raw_issues)]
    issues = _deduplicate_review_issues(issues)
    for issue in issues:
        if issue.get("code") in _ALWAYS_BLOCKING_CODES:
            issue["blocking"] = True

    if source_text:
        data["raw_text_snippet"] = source_text[:200]
        _restore_explicit_header_fields(data, source_text)
        _restore_numbered_notice_remark(data, source_text)
        _validate_sender_contact(data, source_text, issues)
        for field in _PERSON_FIELDS:
            value = _field_value(data, field)
            if isinstance(value, str) and value.strip() and not _person_name_is_in_source(
                value.strip(), source_text
            ):
                _append_issue(
                    issues,
                    code="person_name_not_verbatim",
                    field=field,
                    message="人名未在原文中逐字出现，需对照原文件确认",
                    source_values=[value],
                )

        lookup_value = next((value for value in _TRANSIT_LOOKUP_VALUES if value in source_text), None)
        transit_port = data.get("transit_port")
        if lookup_value and not transit_port:
            data["transit_port"] = lookup_value
        elif lookup_value and isinstance(transit_port, str) and transit_port != lookup_value:
            _append_issue(
                issues,
                code="conflicting_transit_port",
                field="transit_port",
                message="中转港同时出现具体值和待查描述，需人工确认",
                source_values=[transit_port, lookup_value],
            )
    else:
        data["raw_text_snippet"] = None
        for field in _PERSON_FIELDS:
            value = _field_value(data, field)
            if isinstance(value, str) and value.strip():
                _append_issue(
                    issues,
                    code="person_name_unverified",
                    field=field,
                    message="视觉输入中的人名无法与转换原文逐字核对，需人工确认",
                    source_values=[value],
                )

    if source_text:
        _restore_explicit_hbl(data, source_text, issues)

    _validate_shipper_company(data, source_text, issues)
    _validate_container_identifiers(data, source_text, issues)
    _prefer_explicit_detail_container(data, source_text, issues)
    _preserve_container_types(data, source_text, issues)
    _sanitize_container_measurements(data, issues)
    _restore_packages_unit(data, source_text)
    _normalize_port_fields(data, source_text)
    _inherit_single_container_mbl(data)

    expected_carrier = _carrier_from_mbl(data.get("mbl_no"))
    carrier = data.get("carrier")
    if expected_carrier and not carrier:
        data["carrier"] = expected_carrier
    elif expected_carrier and isinstance(carrier, str) and carrier.upper() != expected_carrier:
        data["carrier"] = expected_carrier
        _append_issue(
            issues,
            code="carrier_prefix_mismatch",
            field="carrier",
            message="承运人与主提单号前缀冲突，已按主提单号前缀修正，需人工确认",
            source_values=[carrier, expected_carrier],
        )
    _validate_carrier_source(data, source_text, issues)

    _restore_indexed_container_remarks(data)
    _remove_confirmed_ocr_artifacts(data, issues)
    _ground_free_text_fields(data, source_text, issues)

    containers = data.get("containers")
    if isinstance(containers, list):
        for index, container in enumerate(containers):
            if not isinstance(container, dict):
                continue
            remark = container.get("remark")
            if isinstance(remark, str) and any(marker in remark for marker in _CONFLICT_MARKERS):
                _append_issue(
                    issues,
                    code="conflicting_container_data",
                    field=f"containers[{index}]",
                    message="同一柜存在多组件数、毛重或体积，需人工裁决",
                    source_values=[remark],
                )
        _restore_missing_container_measurements(data, source_text, issues)

    shipper_company = data.get("shipper_company")
    if not isinstance(shipper_company, str) or not shipper_company.strip():
        shipper_company = None
        _append_issue(
            issues,
            code="missing_shipper_company",
            field="shipper_company",
            message="缺少托运人公司，订单必填字段 c_title 需人工确认",
        )

    factory = data.get("factory")
    factory_name = factory.get("name") if isinstance(factory, dict) else None
    if not isinstance(factory_name, str) or not factory_name.strip():
        factory_name = None
        _append_issue(
            issues,
            code="missing_factory_name",
            field="factory.name",
            message="缺少工厂门点简称，订单字段 factory_name 需人工确认",
        )

    issues = _deduplicate_review_issues(issues)
    issues.sort(
        key=lambda issue: (
            _REVIEW_ISSUE_PRIORITY.get(str(issue.get("code")), 50),
            str(issue.get("code", "")),
            str(issue.get("field", "")),
        )
    )
    data["review_issues"] = issues
    data["ready_for_order"] = not any(issue.get("blocking", True) for issue in issues)
    data["order_mapping"] = {
        "c_sn": data.get("internal_ref"),
        "mbl_no": data.get("mbl_no"),
        "hbl_no": data.get("hbl_no"),
        "c_title": shipper_company,
        "factory_name": factory_name,
        "c_note": _build_order_note(data),
    }
    return data
