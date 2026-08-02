"""Exact template fingerprints and deterministic field mapping."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .normalizer import (
    extract_number,
    is_separator_row,
    normalize_label,
    parse_date_or_none,
    parse_html_table_rows,
    pipe_row_cells,
)


@dataclass
class MapperResult:
    template_id: str | None = None
    fingerprint: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.template_id is not None and bool(self.values)


_BINGSHENG_HEADER_SEQUENCE = (
    ("主单号", "船名", "航次"),
    ("起运港", "目的港", "码头"),
    ("箱型箱量", "ETD", "中转港代码"),
    ("件数", "毛重", "体积"),
    ("地址类型", "地址", "时间"),
)
_KNOWN_HEADER_LABELS = frozenset(label for row in _BINGSHENG_HEADER_SEQUENCE for label in row)
_BINGSHENG_REQUIRED_KEYWORDS = ("主单号", "船名", "航次")


def _fingerprint(sequence: tuple[tuple[str, ...], ...]) -> str:
    payload = json.dumps(sequence, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


BINGSHENG_FINGERPRINT = _fingerprint(_BINGSHENG_HEADER_SEQUENCE)


def _table_rows(markdown: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = pipe_row_cells(line)
        if not cells or is_separator_row(cells):
            continue
        if cells[0] == "_row/col_":
            continue
        if cells and cells[0].isdigit():
            cells = cells[1:]
        rows.append(cells)
    if rows:
        return rows

    # MinerU may emit an HTML table for DOCX templates instead of pipe rows.
    return [row for row in parse_html_table_rows(markdown) if row]


def _header_sequence(rows: list[list[str]]) -> tuple[tuple[str, ...], ...]:
    headers: list[tuple[str, ...]] = []
    for row in rows:
        normalized = tuple(cell.strip().rstrip("：:") for cell in row if cell.strip())
        if normalized and all(cell in _KNOWN_HEADER_LABELS for cell in normalized):
            headers.append(normalized)
    return tuple(headers)


def _paired_table_values(rows: list[list[str]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, row in enumerate(rows[:-1]):
        labels = [cell.strip().rstrip("：:") for cell in row]
        if not labels or not all(label in _KNOWN_HEADER_LABELS for label in labels):
            continue
        next_row = rows[index + 1]
        for column, label in enumerate(labels):
            if column < len(next_row) and next_row[column].strip():
                values[label] = next_row[column].strip()
    return values


def _table_value_for_labels(rows: list[list[str]], labels: tuple[str, ...]) -> str | None:
    expected = {normalize_label(label) for label in labels}
    known_labels = {
        normalize_label(label)
        for _, field_labels in _NEIZHUANG_LABELS
        for label in field_labels
    }
    for row_index, row in enumerate(rows):
        for column, cell in enumerate(row):
            if normalize_label(cell) not in expected:
                continue
            # Inline tables alternate label/value cells on the same row.
            if column + 1 < len(row):
                inline_value = row[column + 1].strip()
                if inline_value and normalize_label(inline_value) not in known_labels:
                    return inline_value
            # Paired tables put labels in one row and values in the next.
            for next_row in rows[row_index + 1 :]:
                if is_separator_row(next_row):
                    continue
                if column < len(next_row) and next_row[column].strip():
                    return next_row[column].strip()
                break
    return None


def _mixed_row_value(rows: list[list[str]], label: str) -> str | None:
    normalized_label = label.rstrip("：:")
    for row in rows:
        for index, cell in enumerate(row[:-1]):
            if cell.strip().rstrip("：:") == normalized_label:
                value = row[index + 1].strip()
                if value:
                    return value
    return None


_NEIZHUANG_LABELS = (
    ("carrier", ("船公司", "承运人", "船东", "CARRIER")),
    ("etd", ("船期", "开航日期", "开航日", "ETD")),
    ("transit_port", ("中转港（卸港）", "中转港(卸港)", "中转港", "卸港")),
    ("remark", ("要求进港时间", "要求进港")),
)


def _extract_neizhuang_booking(markdown: str, rows: list[list[str]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for output_field, labels in _NEIZHUANG_LABELS:
        value = _table_value_for_labels(rows, labels)
        if value:
            values[output_field] = value
    return values


def _extract_bingsheng(markdown: str, rows: list[list[str]]) -> tuple[dict[str, Any], list[str]]:
    table_values = _paired_table_values(rows)
    internal_ref = _mixed_row_value(rows, "订单编号")
    recipient = _mixed_row_value(rows, "TO")

    date_value: str | None = None
    for row in rows:
        for index, cell in enumerate(row[:-1]):
            match = re.fullmatch(r"日期[：:]?(\d{0,4})", cell.strip())
            if match:
                date_value = f"{match.group(1)}{row[index + 1].strip()}"
                break
        if date_value:
            break
    doc_date = parse_date_or_none(date_value or "")
    mbl_no = table_values.get("主单号")
    sentinel_errors: list[str] = []
    if not mbl_no or re.search(r"[\u4e00-\u9fff]", mbl_no) or not re.fullmatch(
        r"[A-Za-z0-9./-]+", mbl_no
    ):
        sentinel_errors.append("invalid_mbl_no")
    if not doc_date:
        sentinel_errors.append("invalid_doc_date")
    if sentinel_errors:
        return {}, sentinel_errors

    shipper_agent_match = re.search(
        r"^(?!#|\|)([^\n|]{2,80}(?:有限责任公司|股份有限公司|有限公司|公司))\s*$",
        markdown,
        re.MULTILINE,
    )
    shipper_agent = None
    if shipper_agent_match:
        shipper_agent = re.sub(
            r"^_(?:p|l)\d+(?:: \(empty\))?_\s*",
            "",
            shipper_agent_match.group(1).strip(),
            flags=re.IGNORECASE,
        )
    container_text = table_values.get("箱型箱量", "")
    container_match = re.fullmatch(r"\s*(\d+)\s*[xX*×]\s*([^\s]+)\s*", container_text)
    container: dict[str, Any] = {
        "type": container_match.group(2) if container_match else (container_text or None),
        "qty": int(container_match.group(1)) if container_match else 1,
        "packages": extract_number(table_values.get("件数"), integer=True),
        "gross_weight_kg": extract_number(table_values.get("毛重")),
        "volume_cbm": extract_number(table_values.get("体积")),
    }

    address_value = table_values.get("地址", "")
    phone_match = re.search(r"1\d{10}", address_value)
    contact = address_value[: phone_match.start()].strip() if phone_match else None
    loading_time = table_values.get("时间")
    if loading_time and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", loading_time):
        loading_time = loading_time.replace(" ", "T")

    values: dict[str, Any] = {
        "doc_type": "TRANSPORT_ORDER",
        "internal_ref": internal_ref,
        "mbl_no": mbl_no,
        "vessel": table_values.get("船名"),
        "voyage": table_values.get("航次"),
        "pol": table_values.get("起运港"),
        "pod": table_values.get("目的港"),
        "terminal": table_values.get("码头") or None,
        "etd": parse_date_or_none(table_values.get("ETD", "")),
        "transit_port": table_values.get("中转港代码") or None,
        "containers": [container],
        "factory": {
            "name": None,
            "contact": contact,
            "phone": phone_match.group(0) if phone_match else None,
        },
        "shipper_company": shipper_agent,
        "shipper_agent": shipper_agent,
        "recipient": recipient,
        "doc_date": doc_date,
        "loading_time": loading_time,
    }
    return values, []


def map_template(markdown: str | None) -> MapperResult:
    if not markdown:
        return MapperResult()
    rows = _table_rows(markdown)
    neizhuang_values = _extract_neizhuang_booking(markdown, rows)
    normalized_source = normalize_label(markdown)
    if (
        ("内装箱委托书" in normalized_source and len(neizhuang_values) >= 3)
        or all(field in neizhuang_values for field, _ in _NEIZHUANG_LABELS)
    ):
        return MapperResult(
            template_id="neizhuang_booking",
            fingerprint=_fingerprint(
                tuple((labels[0],) for _, labels in _NEIZHUANG_LABELS)
            ),
            values=neizhuang_values,
        )
    sequence = _header_sequence(rows)
    fingerprint = _fingerprint(sequence)
    if fingerprint != BINGSHENG_FINGERPRINT or not all(
        keyword in markdown for keyword in _BINGSHENG_REQUIRED_KEYWORDS
    ):
        return MapperResult(fingerprint=fingerprint)

    values, sentinel_errors = _extract_bingsheng(markdown, rows)
    if sentinel_errors:
        return MapperResult(
            fingerprint=fingerprint,
            issues=[
                {
                    "code": "template_mismatch",
                    "field": "source.template_hint",
                    "message": "模板指纹命中但哨兵字段格式异常，已降级通用 AI 路线",
                    "source_values": sentinel_errors,
                    "blocking": True,
                }
            ],
        )
    return MapperResult(
        template_id="bingsheng_transport",
        fingerprint=fingerprint,
        values=values,
    )


def _merge_value(
    target: dict[str, Any],
    key: str,
    mapped: Any,
    path: str,
    issues: list[dict[str, Any]],
) -> None:
    if isinstance(mapped, dict):
        current = target.get(key)
        if not isinstance(current, dict):
            current = {}
            target[key] = current
        for child_key, child_value in mapped.items():
            _merge_value(current, child_key, child_value, f"{path}.{child_key}", issues)
        return
    if isinstance(mapped, list):
        current = target.get(key)
        if not isinstance(current, list):
            current = []
            target[key] = current
        for index, mapped_item in enumerate(mapped):
            if index >= len(current):
                current.append(mapped_item)
                continue
            if isinstance(mapped_item, dict) and isinstance(current[index], dict):
                for child_key, child_value in mapped_item.items():
                    _merge_value(
                        current[index],
                        child_key,
                        child_value,
                        f"{path}[{index}].{child_key}",
                        issues,
                    )
            elif current[index] not in (None, mapped_item):
                issues.append(_conflict_issue(f"{path}[{index}]", current[index], mapped_item))
                current[index] = mapped_item
        return
    if mapped is None:
        return
    current = target.get(key)
    if current not in (None, "", mapped):
        issues.append(_conflict_issue(path, current, mapped))
    target[key] = mapped


def _conflict_issue(path: str, ai_value: Any, mapped_value: Any) -> dict[str, Any]:
    return {
        "code": "deterministic_ai_conflict",
        "field": path,
        "message": "AI 结果与确定性模板映射冲突，已保留模板值，必须人工复核",
        "source_values": [str(ai_value), str(mapped_value)],
        "blocking": True,
    }


def merge_deterministic_values(data: dict[str, Any], result: MapperResult) -> dict[str, Any]:
    issues = data.setdefault("review_issues", [])
    issues.extend(result.issues)
    if not result.matched:
        return data
    for key, mapped in result.values.items():
        if key == "doc_type":
            # 文档类型由确定性模板锁定（模板指纹命中优先于路由/LLM 猜测），
            # 两者不同不算冲突，否则模板正确命中时会被误报 blocking 而阻塞下单。
            data[key] = mapped
            continue
        _merge_value(data, key, mapped, key, issues)
    return data
