"""托书结果后处理的校验与修复层：单据、容器、发货人与 grounding 校验。"""

from __future__ import annotations

import html
import re
from typing import Any

from .normalizer import (
    STANDARD_CONTAINER_SUFFIXES,
    is_known_container_type,
    normalize_container_type,
    normalize_date_value,
)
from .post_common import (
    _BARE_COMPANY_RE,
    _append_issue,
    _carrier_from_mbl,
    _extract_explicit_carriers,
    _extract_explicit_values,
    _extract_labeled_value,
    _extract_separated_label_values,
    _header_company_candidates,
    _remove_issue,
    _remove_remark_clauses_containing,
)
from .schema import DATE_OR_DATETIME_PATTERN, DATE_PATTERN

_MEASUREMENT_LABELS = {
    "packages": ("件数", "包装件数", "packages"),
    "volume_cbm": ("体积", "volume", "cbm"),
}


_GROUNDING_WRAPPERS = ("另有记录", "主值", "待人工确认")


_CONFIRMED_OCR_ARTIFACT_PATTERNS = (
    (re.compile(r"\s*作业\s*资水\s*[！!]?"), "作业资水"),
)


_CONTAINER_NO_RE = re.compile(r"^[A-Z]{4}\d{7}$")


_SEAL_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./-]{3,19}$")


_DATE_ONLY_RE = re.compile(DATE_PATTERN)


_DATE_OR_DATETIME_RE = re.compile(DATE_OR_DATETIME_PATTERN)


# 提单号：字母数字混合且同时包含字母与数字，至少 8 位。
# 排除纯数字（电话/日期/内部编号）与纯字母（船名/人名），
# 与 _restore_mbl_no_from_source 及 prompt 的“排除纯数字”规则保持一致。
_BILL_NO_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{8,}$")


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
    r"(?:GENERAL|OPENTOP|OPEN\s*TOP|TANK|FLAT|REF|NOR|"
    + "|".join(STANDARD_CONTAINER_SUFFIXES)
    + r"|DV|DC|SD|H)"
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
    data: dict[str, Any],
    source_text: str | None,
    issues: list[dict[str, Any]],
    *,
    template_hint: str | None,
) -> None:
    if not source_text:
        # vision-only 输入：LLM 直接看图提取的值是合法来源，不做原文核对；
        # 此类输入自带 vision_only_unverified blocking issue，人工必复核。
        return
    labels = ("托运人公司", "托运人", "发货人公司", "发货公司", "发货人", "SHIPPER")
    explicit_values = _extract_explicit_values(source_text, labels) if source_text else []
    # 做箱工厂是门点工厂，不是客户/托运人；客户只认 FM/FROM、客户栏或抬头公司
    # （zuoxiang_std_esff 模板历史上曾用做箱工厂兜底，会误把门点工厂当客户）
    if (
        not explicit_values
        and source_text
        and template_hint in {"bingsheng_transport", "bolian_segway"}
    ):
        explicit_values = _header_company_candidates(source_text)
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
    if not source_text:
        # vision-only 输入：没有 OCR/转换文本可逐字核对，保留模型从图片
        # 直接提取的值；该类输入自带 blocking issue 强制人工复核。
        return
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


def _remove_empty_remark_clauses(data: dict[str, Any]) -> None:
    targets: list[dict[str, Any]] = [data]
    containers = data.get("containers")
    if isinstance(containers, list):
        targets.extend(container for container in containers if isinstance(container, dict))
    for target in targets:
        remark = target.get("remark")
        if not isinstance(remark, str) or not remark.strip():
            continue
        clauses = [part.strip() for part in re.split(r"[；;\n]+", remark) if part.strip()]
        kept = [
            clause
            for clause in clauses
            if not re.fullmatch(r"[^：:；;\n]{1,30}[：:]", clause)
        ]
        target["remark"] = "；".join(kept) or None


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
            elif (
                field != "packages"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                container[field] = round(value, 3)
        if invalid:
            _append_issue(
                issues,
                code="missing_container_measurements",
                field=f"containers[{index}]",
                message="集装箱件数、毛重或体积包含非正数，已清空并需人工确认",
                source_values=invalid,
            )


def _sanitize_bill_numbers(data: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    targets: list[tuple[dict[str, Any], str, str]] = [
        (data, "mbl_no", "mbl_no"),
        (data, "hbl_no", "hbl_no"),
    ]
    containers = data.get("containers")
    if isinstance(containers, list):
        targets.extend(
            (container, "mbl_no", f"containers[{index}].mbl_no")
            for index, container in enumerate(containers)
            if isinstance(container, dict)
        )

    for target, key, field in targets:
        value = target.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if _BILL_NO_RE.fullmatch(value):
            target[key] = value
            continue
        target[key] = None
        _append_issue(
            issues,
            code=f"invalid_{key}",
            field=field,
            message="提单号必须至少 8 位，且只能由数字或英文字母数字组成，已清空",
            source_values=[value],
        )


_QUANTITY_LIKE_RE = re.compile(r"^\d+[A-Za-z]{2,}$")


_MASTER_BILL_LABELS = ("提单号", "主提单号", "主单号")


# 关单号即提单号（与 orders/parse-document 对齐）；提取函数要求标签前为
# 行首/空白，“报关单号”不会因前缀“报”字被误匹配
_CUSTOMS_NO_LABELS = ("关单号",)


# 报关单号/报关号不是提单号：格式特征恢复时显式排除其标签值
_CUSTOMS_DECLARATION_LABELS = ("报关单号", "报关号")


_CHILD_BILL_LABELS = ("子提单号", "子单号", "分提单号", "分单号", "HBL NO", "HB/L")


_KNOWN_FIELD_METADATA_KEYS = frozenset(
    {"raw_text_snippet", "source", "doc_type", "template_hint", "extracted_at"}
)


def _looks_like_quantity(token: str) -> bool:
    """`1100CTNS` 这类数字+单位形式的 token 不是提单号。"""
    return bool(_QUANTITY_LIKE_RE.match(token))


def _collect_known_strings(obj: Any) -> set[str]:
    """收集已在输出字段中占用的字符串，避免把已有值再次恢复进提单号。"""
    values: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _KNOWN_FIELD_METADATA_KEYS:
                    continue
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, str) and node:
            values.add(node)

    visit(obj)
    return values


def _restore_mbl_no_from_source(
    data: dict[str, Any], source_text: str, issues: list[dict[str, Any]]
) -> None:
    """表格展平文档的兜底：按标签或格式特征恢复主提单号。

    优先用 `提单号/主提单号/主单号` 标签锚定（同段相邻或跨空段）；
    其次仅当全文只有一个符合提单号格式特征、且未出现在已识别字段或
    子提单号标签后的 token 时，才按格式特征恢复。恢复值一律加
    blocking `mbl_no_by_format` 供人工确认。
    """
    current = data.get("mbl_no")
    if isinstance(current, str) and current.strip():
        return
    if not source_text:
        return

    bill_anchored: list[str] = []
    for value in _extract_explicit_values(source_text, _MASTER_BILL_LABELS):
        if _BILL_NO_RE.fullmatch(value) and not _looks_like_quantity(value):
            bill_anchored.append(value)
    for value in _extract_separated_label_values(source_text, _MASTER_BILL_LABELS):
        if (
            _BILL_NO_RE.fullmatch(value)
            and not _looks_like_quantity(value)
            and value not in bill_anchored
        ):
            bill_anchored.append(value)
    # 关单号即提单号：仅当原文无“提单号”标签时才作为 mbl_no 来源（提单号优先）；
    # 运编号/业务编号只进 internal_ref，不作为提单号来源；标签前有“报”字的
    # 报关单号不会被提取函数锚定（前边界为行首/空白）
    customs_anchored: list[str] = []
    for value in _extract_explicit_values(source_text, _CUSTOMS_NO_LABELS):
        if (
            _BILL_NO_RE.fullmatch(value)
            and not _looks_like_quantity(value)
            and value not in bill_anchored
            and value not in customs_anchored
        ):
            customs_anchored.append(value)
    for value in _extract_separated_label_values(source_text, _CUSTOMS_NO_LABELS):
        if (
            _BILL_NO_RE.fullmatch(value)
            and not _looks_like_quantity(value)
            and value not in bill_anchored
            and value not in customs_anchored
        ):
            customs_anchored.append(value)
    if len(bill_anchored) == 1:
        # 提单号标签优先：原文明确标注“提单号/主提单号/主单号”时以其为准
        restored = bill_anchored[0]
    elif len(set(customs_anchored)) == 1 and not bill_anchored:
        # 原文无提单号标签、仅有关单号标签（如“运编号+关单号”文档）：
        # 按“关单号即提单号”规则取关单号
        restored = customs_anchored[0]
    else:
        child_values = set(
            _extract_explicit_values(source_text, _CHILD_BILL_LABELS)
        ) | set(
            _extract_separated_label_values(source_text, _CHILD_BILL_LABELS)
        )
        # 报关单号/报关号不是提单号：即使未被 LLM 识别也不得按格式特征恢复为 mbl_no
        declaration_values = set(
            _extract_explicit_values(source_text, _CUSTOMS_DECLARATION_LABELS)
        ) | set(
            _extract_separated_label_values(source_text, _CUSTOMS_DECLARATION_LABELS)
        )
        known_values = _collect_known_strings(data)
        plausible = {
            token
            for token in re.findall(r"[A-Za-z0-9]{8,}", source_text)
            if _BILL_NO_RE.fullmatch(token)
            and not _looks_like_quantity(token)
            and re.search(r"[A-Za-z]", token)
            and re.search(r"\d", token)
            and token not in known_values
            and token not in child_values
            and token not in declaration_values
        }
        if len(plausible) != 1:
            return
        restored = plausible.pop()
    data["mbl_no"] = restored
    _append_issue(
        issues,
        code="mbl_no_by_format",
        field="mbl_no",
        message="提单号由原文标签或格式特征恢复，需人工确认",
        source_values=[restored],
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
        r"(?<![A-Za-z0-9])" + _CONTAINER_TYPE_TOKEN + r"(?![A-Za-z0-9])",
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
            container["type"] = None
            _append_issue(
                issues,
                code="missing_container_type",
                field=f"containers[{index}].type",
                message="缺少箱型，需人工确认",
            )
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
    data: dict[str, Any],
    source_text: str | None,
    issues: list[dict[str, Any]],
    explicit_carriers: list[str] | None = None,
) -> None:
    inferred_codes = {"carrier_by_mbl", "carrier_by_vessel", "carrier_source_unverified"}
    carrier = data.get("carrier")
    if not isinstance(carrier, str) or not carrier.strip():
        issues[:] = [issue for issue in issues if issue.get("code") not in inferred_codes]
        return
    carrier = carrier.strip()
    data["carrier"] = carrier

    if explicit_carriers is None:
        explicit_carriers = (
            _extract_explicit_carriers(source_text) if source_text is not None else []
        )
    if explicit_carriers:
        data["carrier"] = explicit_carriers[0]
        issues[:] = [issue for issue in issues if issue.get("code") not in inferred_codes]
        return

    carrier = carrier.upper()
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


def _sanitize_schema_dates(data: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    """Keep malformed optional dates from rejecting the entire extraction."""
    specs = (
        ("etd", False, _DATE_ONLY_RE),
        ("doc_date", False, _DATE_ONLY_RE),
        ("loading_time", True, _DATE_OR_DATETIME_RE),
    )
    for field, allow_time, pattern in specs:
        value = data.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            data[field] = None
            continue
        normalized = normalize_date_value(value, allow_time=allow_time)
        if isinstance(normalized, str) and pattern.fullmatch(normalized):
            data[field] = normalized
            continue
        data[field] = None
        _append_issue(
            issues,
            code="invalid_date_format",
            field=field,
            message="日期值格式或日历日期无效，已清空并需人工确认",
            source_values=[str(value)],
        )


def _restore_missing_container_measurements(
    data: dict[str, Any], source_text: str | None, issues: list[dict[str, Any]]
) -> None:
    """Flag a container whose explicitly supplied measurements are both absent.

    Missing values remain ``null`` in the extraction JSON.  The issue is generated
    here so a renderer never has to infer a second set of review rules.  When both
    values are present, model-authored missing hints are dropped as false reports
    (the non-positive sanitizer hint is preserved).
    """
    containers = data.get("containers")
    if not isinstance(containers, list):
        return
    removed_broad_issue = False
    for index, container in enumerate(containers):
        if not isinstance(container, dict):
            continue
        missing = [
            field for field in ("packages", "volume_cbm") if container.get(field) is None
        ]
        if len(missing) == 0:
            issues[:] = [
                issue
                for issue in issues
                if not (
                    issue.get("code") == "missing_container_measurements"
                    and str(issue.get("field", "")).startswith(f"containers[{index}]")
                    and issue.get("message")
                    != "集装箱件数、毛重或体积包含非正数，已清空并需人工确认"
                )
            ]
            continue
        if len(missing) != 2:
            continue
        # Text documents must actually expose a measurement label.  For vision
        # input source_text is unavailable, so an empty pair is itself unverified.
        if source_text is not None and not _source_mentions_measurements(source_text, missing):
            continue
        if not removed_broad_issue:
            issues[:] = [
                issue
                for issue in issues
                if not (
                    issue.get("code") == "missing_container_measurements"
                    and issue.get("field") == "containers"
                )
            ]
            removed_broad_issue = True
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
