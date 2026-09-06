"""托书结果的确定性订单映射与人工复核校验（编排层）。"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..checks.common import (
    _FIELD_LABEL_VOCAB,
    _MARKDOWN_PARAGRAPH_PREFIX_RE,
    _append_issue,
    _append_top_level_remark,
    _carrier_from_mbl,
    _customer_notice_heading_candidates,
    _deduplicate_review_issues,
    _extract_explicit_carriers,
    _extract_explicit_values,
    _extract_fragmented_dates,
    _extract_separated_label_values,
    _extract_table_column_values,
    _field_value,
    _header_company_candidates,
    _looks_like_field_label,
    _person_name_is_in_source,
    _recipient_row_extras,
    _remove_issue,
    _remove_remark_clauses_containing,
    _value_grounded_in_source,
)
from ..checks.validators import (
    _build_order_note,
    _ground_free_text_fields,
    _inherit_single_container_mbl,
    _normalize_port_fields,
    _prefer_explicit_detail_container,
    _preserve_container_types,
    _reject_conflicting_mbl_no,
    _remove_confirmed_ocr_artifacts,
    _remove_empty_remark_clauses,
    _restore_explicit_hbl,
    _restore_indexed_container_remarks,
    _restore_mbl_no_from_source,
    _restore_missing_container_measurements,
    _restore_packages_unit,
    _sanitize_bill_numbers,
    _sanitize_container_measurements,
    _sanitize_schema_dates,
    _validate_carrier_source,
    _validate_container_identifiers,
    _validate_sender_contact,
    _validate_shipper_company,
)
from ..normalize import (
    normalize_label,
    normalize_review_issues,
    parse_date_or_none,
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


# 中转港值的裸简写（不在 _TRANSIT_LOOKUP_VALUES 全局兜底里，避免原文任意
# 位置出现该词就触发 lookup 污染；仅在 transit 候选过滤时按待查语义处理）
_BARE_TRANSIT_LOOKUP = frozenset({"设备单"})


_ALWAYS_BLOCKING_CODES = {
    "deterministic_ai_conflict",
    "template_mismatch",
    "mineru_failed",
    "mineru_low_confidence",
    "confirmed_ocr_artifact",
    "vision_image_too_large",
    "scanned_pdf_ocr_unverified",
    "ungrounded_text",
    "sender_contact_not_from_from_field",
    "carrier_prefix_mismatch",
    "conflicting_container_data",
    "conflicting_mbl_no",
    "missing_container_measurements",
    "conflicting_transit_port",
    "missing_mbl_no",
    "missing_container_type",
    "missing_address",
    "missing_customer",
    "customer_conflicting_candidates",
    "customer_ungrounded",
    "mbl_no_not_verbatim",
    "container_type_ungrounded",
    "doc_ole_fallback_used",
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
    "mbl_no_by_format",
    "invalid_date_format",
    "invalid_mbl_no",
    "invalid_hbl_no",
}


_CONFLICT_MARKERS = ("数据冲突", "数值冲突", "另有记录", "另一组", "两组数据", "待人工确认")


_REVIEW_ISSUE_PRIORITY = {
    "missing_mbl_no": 10,
    "missing_container_measurements": 10,
    "missing_address": 30,
    "missing_container_type": 30,
    "carrier_by_mbl": 40,
    "carrier_by_vessel": 40,
    "carrier_source_unverified": 40,
}


# review_issues 白名单：代码可生成/认可的 code 全集。LLM 自创 code
# （如 missing_container_packages/missing_gross_weight/missing_volume）一律清除。
_KNOWN_REVIEW_ISSUE_CODES = _ALWAYS_BLOCKING_CODES | {
    "missing_loading_time",
    "unknown_container_type",
    "container_type_not_verbatim",
    "vision_cross_check_skipped",
    "etd_year_missing",
    "vision_only_unverified",
    "conversion_coverage_incomplete",
    "formula_value_unavailable",
    "embedded_image_unprocessed",
    "embedded_image_requires_review",
    "notice_remark_restored",
    "legacy_xls_formula_unverified",
    "document_value_unclear",
    "customer_unverified",
    "customer_cross_chain",
    "mbl_no_unverified",
}


def _filter_known_review_issues(
    issues: list[dict[str, Any]], containers: Any
) -> list[dict[str, Any]]:
    """清除 LLM 自创 code，并把 `containers[]` 占位字段归一化。"""
    result: list[dict[str, Any]] = []
    for issue in issues:
        code = issue.get("code")
        if code not in _KNOWN_REVIEW_ISSUE_CODES:
            continue
        issue = dict(issue)
        field = issue.get("field")
        if isinstance(field, str) and field.startswith("containers["):
            rest = field[len("containers["):]
            if rest.startswith("]"):
                suffix = rest[1:]
                if isinstance(containers, list) and len(containers) == 1:
                    issue["field"] = f"containers[0]{suffix}"
                else:
                    issue["field"] = f"containers{suffix}"
        result.append(issue)
    return result


def _restore_explicit_header_fields(
    data: dict[str, Any],
    source_text: str,
    issues: list[dict[str, Any]] | None = None,
    reference_year: int | None = None,
    explicit_carriers: list[str] | None = None,
) -> None:
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
        if (parsed := parse_date_or_none(value, allow_time=False))
    }
    doc_dates.update(_extract_fragmented_dates(source_text))
    if len(doc_dates) == 1:
        data["doc_date"] = doc_dates.pop()

    if explicit_carriers is None:
        explicit_carriers = _extract_explicit_carriers(source_text)
    if explicit_carriers:
        # The source label is authoritative, including names such as ``EMC CPS``.
        data["carrier"] = explicit_carriers[0]
        target_issues = issues if issues is not None else data.get("review_issues")
        if isinstance(target_issues, list):
            target_issues[:] = [
                issue
                for issue in target_issues
                if not (
                    issue.get("code") == "deterministic_ai_conflict"
                    and issue.get("field") == "carrier"
                )
            ]

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

    fm_values = _extract_explicit_values(source_text, ("FM",))
    fm_values.extend(
        value
        for value in _extract_separated_label_values(source_text, ("FM",))
        if value not in fm_values
    )
    explicit_customers = _extract_explicit_values(source_text, ("客户", "客户名称", "客户简称"))
    notice_heading_values = _customer_notice_heading_candidates(source_text)
    header_values = _header_company_candidates(source_text)
    customer_candidates = (
        fm_values or explicit_customers or notice_heading_values or header_values
    )
    if len(customer_candidates) == 1:
        data["customer"] = customer_candidates[0]
        # 采用首条非空链时，其余链若也有候选：透出为非阻断诊断，不静默丢弃
        ignored_cross_chain = [
            *explicit_customers,
            *notice_heading_values,
            *header_values,
        ]
        ignored_cross_chain = [
            value
            for value in ignored_cross_chain
            if value != customer_candidates[0]
        ]
        if ignored_cross_chain:
            _append_issue(
                issues if issues is not None else data.setdefault("review_issues", []),
                code="customer_cross_chain",
                field="customer",
                message="其他候选链也识别到不同客户值，已按优先级取首条链，建议人工抽检",
                blocking=False,
                source_values=list(dict.fromkeys(ignored_cross_chain)),
            )
    elif len(customer_candidates) > 1:
        # 同一链内多个确定性候选：无法唯一裁决，置空待人工填入，不猜
        data["customer"] = None
        _append_issue(
            issues if issues is not None else data.setdefault("review_issues", []),
            code="customer_conflicting_candidates",
            field="customer",
            message="客户确定性候选有多个且无法唯一确定，已置空待人工填入",
            source_values=customer_candidates,
        )
    elif isinstance(data.get("customer"), str) and data["customer"].strip():
        # 四条确定性链均未命中但模型给出了值：验证或置空，不采信无据值
        model_customer = data["customer"].strip()
        if source_text and _value_grounded_in_source(model_customer, source_text):
            _append_issue(
                issues if issues is not None else data.setdefault("review_issues", []),
                code="customer_unverified",
                field="customer",
                message="客户值仅由模型输出（原文可找到依据），FM/客户栏/通知抬头/正文抬头四条确定性链均未命中，建议人工抽检",
                blocking=False,
                source_values=[model_customer],
            )
        elif source_text:
            data["customer"] = None
            _append_issue(
                issues if issues is not None else data.setdefault("review_issues", []),
                code="customer_ungrounded",
                field="customer",
                message="客户值未在原文中找到依据，已置空待人工填入",
                source_values=[model_customer],
            )
        else:
            # 纯视觉输入无文本可核对：保留值但必须提示不可验证
            _append_issue(
                issues if issues is not None else data.setdefault("review_issues", []),
                code="customer_unverified",
                field="customer",
                message="视觉输入中无法与文本核对客户值，建议人工抽检",
                blocking=False,
                source_values=[model_customer],
            )

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
        if (parsed := parse_date_or_none(value, allow_time=True))
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
        if (parsed := parse_date_or_none(value, allow_time=False))
    }
    if len(etd_dates) == 1:
        data["etd"] = etd_dates.pop()
    elif not etd_dates:
        # The booking-note template commonly writes ``1月21日``.  Infer only
        # the year that is already explicit in the document date.
        doc_year_match = re.match(r"(\d{4})-", str(data.get("doc_date") or ""))
        if doc_year_match is None:
            internal_ref = str(data.get("internal_ref") or "")
            doc_year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", internal_ref)
        if doc_year_match is None:
            # A year in an explicit date elsewhere in the source is a safer
            # fallback than leaving a mapped month/day string schema-invalid.
            doc_year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", source_text)
        year = int(doc_year_match.group(1)) if doc_year_match else reference_year
        if year:
            partial_dates: set[str] = set()
            for value in etd_values:
                match = re.fullmatch(r"\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*", value)
                if not match:
                    continue
                try:
                    partial_dates.add(
                        date(
                            year,
                            int(match.group(1)),
                            int(match.group(2)),
                        ).isoformat()
                    )
                except ValueError:
                    continue
            if len(partial_dates) == 1:
                data["etd"] = partial_dates.pop()
        else:
            partial_values = [
                value
                for value in etd_values
                if re.fullmatch(r"\s*\d{1,2}\s*月\s*\d{1,2}\s*日?\s*", value)
            ]
            if partial_values:
                data["etd"] = None
                _append_issue(
                    issues if issues is not None else data.setdefault("review_issues", []),
                    code="etd_year_missing",
                    field="etd",
                    message="船期只有月日且缺少可推断年份，已保留为空，需人工确认",
                    source_values=list(dict.fromkeys(partial_values)),
                )

    transit_values = _extract_explicit_values(
        source_text,
        ("中转港", "中转港代码", "中转港（卸港）", "中转港(卸港)", "卸港"),
    )
    transit_values.extend(
        value
        for value in _extract_table_column_values(
            source_text,
            ("中转港", "中转港代码", "中转港（卸港）", "中转港(卸港)", "卸港"),
        )
        if value not in transit_values
    )
    if transit_values:
        concrete = [
            value
            for value in transit_values
            if value not in _TRANSIT_LOOKUP_VALUES
            and value not in _BARE_TRANSIT_LOOKUP
            and normalize_label(value) not in _FIELD_LABEL_VOCAB
            and not _looks_like_field_label(value)
        ]
        lookup_only = [
            value
            for value in transit_values
            if value in _TRANSIT_LOOKUP_VALUES or value in _BARE_TRANSIT_LOOKUP
        ]
        if concrete:
            data["transit_port"] = concrete[0]
        elif lookup_only:
            data["transit_port"] = lookup_only[0]
        else:
            # 候选全是字段标签词残留：不写垃圾值，模型输出的标签词同样清除
            current = data.get("transit_port")
            if isinstance(current, str) and (
                normalize_label(current) in _FIELD_LABEL_VOCAB
            ):
                data["transit_port"] = None

    required_port_times = _extract_explicit_values(
        source_text, ("要求进港时间", "要求进港")
    )
    required_port_times.extend(
        value
        for value in _extract_table_column_values(source_text, ("要求进港时间", "要求进港"))
        if value not in required_port_times
    )
    if len(required_port_times) == 1:
        _append_top_level_remark(data, required_port_times)


def _apply_template_field_overrides(
    data: dict[str, Any], source_text: str, template_hint: str | None
) -> None:
    if template_hint != "bolian_segway":
        return
    for raw_line in source_text.splitlines():
        line = _MARKDOWN_PARAGRAPH_PREFIX_RE.sub("", raw_line.strip())
        match = re.fullmatch(r"([^\n|：:]{2,40}?)做箱通知", line.strip("# *_`"))
        if not match:
            continue
        factory = data.get("factory")
        if not isinstance(factory, dict):
            factory = {}
            data["factory"] = factory
        factory["name"] = match.group(1).strip()
        return


def _restore_numbered_notice_remark(
    data: dict[str, Any], source_text: str, issues: list[dict[str, Any]]
) -> None:
    """把“请注意”编号列表合并进 remark（保留已有内容），并提示人工确认。

    旧实现直接覆盖 data["remark"]，会把 LLM 已提取的 PO 号、装柜要求等
    备注内容整体丢弃；编号格式不匹配时还会把 remark 清空，且不产生任何
    review issue，属于静默数据丢失。改为合并去重并追加非阻断提示。

    若模型已自行输出编号分句（新模型常直接复述“请注意”列表），跳过
    恢复，避免编号内容重复出现。
    """
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

    existing_clauses: list[str] = []
    existing = data.get("remark")
    if isinstance(existing, str) and existing.strip():
        existing_clauses = [
            part.strip() for part in re.split(r"[；;\n]+", existing) if part.strip()
        ]
    # 模型已输出“请注意”编号列表（如“请注意：1、进港箱单全部打好。”）时
    # 跳过恢复，避免编号内容重复出现
    if any(
        "请注意" in clause and re.search(r"\d+[、.]", clause)
        for clause in existing_clauses
    ):
        return

    pickup = re.search(r"提箱码\s*[：:]?\s*([^\n|]+)", source_text)
    restored_clauses: list[str] = []
    if pickup:
        restored_clauses.append(f"提箱码：{pickup.group(1).strip()}")
    restored_clauses.extend(numbered)

    merged = list(existing_clauses)
    for clause in restored_clauses:
        if clause not in merged:
            merged.append(clause)
    data["remark"] = "；".join(merged) or None

    message = (
        "已从“请注意”编号列表恢复备注并保留原备注内容，请确认"
        if existing_clauses
        else "已从“请注意”编号列表恢复备注，请确认"
    )
    _append_issue(
        issues,
        code="notice_remark_restored",
        field="remark",
        message=message,
        source_values=restored_clauses,
        blocking=False,
    )


def finalize_extraction(
    data: dict[str, Any],
    *,
    source_text: str | None,
    reference_year: int | None = None,
    template_hint: str | None = None,
) -> dict[str, Any]:
    """补充订单映射，并把不可安全自动下单的情况转成结构化问题。"""
    raw_issues = data.get("review_issues")
    issues = [dict(issue) for issue in normalize_review_issues(raw_issues)]
    issues = _deduplicate_review_issues(issues)
    for issue in issues:
        if issue.get("code") in _ALWAYS_BLOCKING_CODES:
            issue["blocking"] = True

    # 同一 source_text 的承运人识别较慢，全流程只计算一次
    explicit_carriers = _extract_explicit_carriers(source_text) if source_text else []

    if source_text:
        data["raw_text_snippet"] = source_text[:200]
        _restore_explicit_header_fields(data, source_text, issues, reference_year, explicit_carriers)
        _apply_template_field_overrides(data, source_text, template_hint)
        _restore_numbered_notice_remark(data, source_text, issues)
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

    _validate_shipper_company(
        data,
        source_text,
        issues,
        template_hint=template_hint,
    )
    _validate_container_identifiers(data, source_text, issues)
    _sanitize_bill_numbers(data, issues)
    _reject_conflicting_mbl_no(data, source_text, issues)
    mbl_no_value = data.get("mbl_no")
    if isinstance(mbl_no_value, str) and mbl_no_value.strip():
        mbl_no_value = mbl_no_value.strip()
        if source_text and not _value_grounded_in_source(mbl_no_value, source_text):
            # 编造的格式合法提单号：先置空再尝试从原文恢复，恢复失败才报无据
            data["mbl_no"] = None
            _restore_mbl_no_from_source(data, source_text, issues)
            if not data.get("mbl_no"):
                _append_issue(
                    issues,
                    code="mbl_no_not_verbatim",
                    field="mbl_no",
                    message="提单号未在原文中逐字出现且无法从原文恢复，已置空待人工填入",
                    source_values=[mbl_no_value],
                )
        elif not source_text:
            # 纯视觉输入无文本可核对：保留值但必须提示不可验证
            _append_issue(
                issues,
                code="mbl_no_unverified",
                field="mbl_no",
                message="视觉输入中无法与文本核对提单号，建议人工抽检",
                blocking=False,
                source_values=[mbl_no_value],
            )
    else:
        _restore_mbl_no_from_source(data, source_text, issues)
    _prefer_explicit_detail_container(data, source_text, issues)
    _preserve_container_types(data, source_text, issues)
    _sanitize_container_measurements(data, issues)
    _restore_packages_unit(data, source_text)
    _normalize_port_fields(data, source_text)
    _inherit_single_container_mbl(data)

    unresolved_partial_etd = data.get("etd")
    if isinstance(unresolved_partial_etd, str) and re.fullmatch(
        r"\s*\d{1,2}\s*月\s*\d{1,2}\s*日?\s*", unresolved_partial_etd
    ):
        data["etd"] = None
        _append_issue(
            issues,
            code="etd_year_missing",
            field="etd",
            message="船期只有月日且缺少可推断年份，已保留为空，需人工确认",
            source_values=[unresolved_partial_etd.strip()],
        )

    expected_carrier = _carrier_from_mbl(data.get("mbl_no"))
    carrier = data.get("carrier")
    explicit_carrier = bool(explicit_carriers)
    if expected_carrier and not carrier:
        data["carrier"] = expected_carrier
    elif (
        expected_carrier
        and not explicit_carrier
        and isinstance(carrier, str)
        and carrier.upper() != expected_carrier
    ):
        data["carrier"] = expected_carrier
        _append_issue(
            issues,
            code="carrier_prefix_mismatch",
            field="carrier",
            message="承运人与主提单号前缀冲突，已按主提单号前缀修正，需人工确认",
            source_values=[carrier, expected_carrier],
        )
    _validate_carrier_source(data, source_text, issues, explicit_carriers)

    _restore_indexed_container_remarks(data)
    _remove_confirmed_ocr_artifacts(data, issues)
    _remove_empty_remark_clauses(data)
    _ground_free_text_fields(data, source_text, issues)
    _sanitize_schema_dates(data, issues)

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
                    message="同一柜存在多组件数、毛重或体积，需人工确认",
                    source_values=[remark],
                )
        _restore_missing_container_measurements(data, source_text, issues)

    shipper_company = data.get("shipper_company")
    if not isinstance(shipper_company, str) or not shipper_company.strip():
        shipper_company = None
    _remove_issue(issues, code="missing_shipper_company", field="shipper_company")

    customer = data.get("customer")
    if not isinstance(customer, str) or not customer.strip():
        data["customer"] = None
        _append_issue(
            issues,
            code="missing_customer",
            field="customer",
            message="缺少客户，未从 FM、客户栏或正文抬头提取到有效值，需人工确认",
        )
    else:
        data["customer"] = customer.strip()
        _remove_issue(issues, code="missing_customer", field="customer")

    loading_time = data.get("loading_time")
    if not isinstance(loading_time, str) or not loading_time.strip():
        data["loading_time"] = None
        _append_issue(
            issues,
            code="missing_loading_time",
            field="loading_time",
            message="缺少做箱日期，文档未解析到装箱日期，需人工确认",
            blocking=False,
        )
    else:
        data["loading_time"] = loading_time.strip()
        _remove_issue(issues, code="missing_loading_time", field="loading_time")

    mbl_no = data.get("mbl_no")
    if not isinstance(mbl_no, str) or not mbl_no.strip():
        data["mbl_no"] = None
        # 多提单号拒绝已明确表达原因（conflicting_mbl_no），不再冗余报缺失
        if not any(
            issue.get("code") == "conflicting_mbl_no" for issue in issues
        ):
            _append_issue(
                issues,
                code="missing_mbl_no",
                field="mbl_no",
                message="缺少提单号，需人工确认",
            )
    else:
        _remove_issue(issues, code="missing_mbl_no", field="mbl_no")

    factory = data.get("factory")
    factory_name = factory.get("name") if isinstance(factory, dict) else None
    if not isinstance(factory_name, str) or not factory_name.strip():
        factory_name = None

    factory_address = factory.get("address") if isinstance(factory, dict) else None
    if not isinstance(factory_address, str) or not factory_address.strip():
        if isinstance(factory, dict):
            factory["address"] = None
        _append_issue(
            issues,
            code="missing_address",
            field="factory.address",
            message="缺少详细街道地址，需人工确认",
        )
    else:
        _remove_issue(issues, code="missing_address", field="factory.address")

    issues = _filter_known_review_issues(issues, data.get("containers"))
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
