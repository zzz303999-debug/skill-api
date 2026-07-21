"""托书结果的确定性订单映射与人工复核校验。"""

from __future__ import annotations

import re
from typing import Any

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
}
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
        if issue.get("code") == code and issue.get("field") == field:
            issue["blocking"] = blocking
            if source_values:
                issue["source_values"] = source_values
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
    issues = [dict(issue) for issue in raw_issues if isinstance(issue, dict)] if isinstance(
        raw_issues, list
    ) else []
    for issue in issues:
        if issue.get("code") in _ALWAYS_BLOCKING_CODES:
            issue["blocking"] = True

    if source_text:
        data["raw_text_snippet"] = source_text[:200]
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

    _restore_indexed_container_remarks(data)

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
