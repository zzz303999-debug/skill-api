"""自由文本订单纯解析器：只读显式标签，不补全、不推断、不归一化值。"""

from __future__ import annotations

import re
from typing import Any

from .schema import BoxItem, DriverItem, OrderTextExtraction

_LABELS: dict[str, tuple[str, ...]] = {
    "factory_bei": ("门点地址", "工厂地址", "装箱地址"),
    "loading_time": ("做箱时间", "做箱日期", "装箱时间", "装箱日期"),
    "vessel_voyage": ("船名航次", "船名/航次"),
    "order_num1": ("提单号", "主提单号", "主单号"),
    "box_text": ("箱型箱量", "箱型/箱量"),
    "c_title": ("托运人/公司名称", "托运人公司名称", "托运人", "公司名称"),
    "c_name": ("托运人联系人", "联系人"),
    "c_phone": ("托运人联系电话", "联系电话"),
    "b_ship_name": ("船名",),
    "b_ship_num": ("航次",),
    "b_ship_company": ("船公司",),
    "factory_name": ("门点简称", "工厂名称"),
    "b_factory_not": ("装箱备注",),
    "b_start_dock": ("启运港", "起运港"),
    "b_end_port": ("中转港代码", "中转港"),
    "b_end_dock": ("目的港",),
    "b_wharf": ("港区",),
    "b_open_ship_time": ("开港时间/开航时间", "开港时间", "开航时间", "ETD"),
    "c_sn": ("内部编号", "业务编号", "我司业务编号"),
    "c_note": ("客户备注", "备注"),
    "packages": ("件数",),
    "gross_weight": ("毛重",),
    "volume": ("体积",),
    "cargo_name": ("货名",),
    "marks": ("唛头",),
}
_ALIAS_TO_FIELD = {
    alias.casefold(): field for field, aliases in _LABELS.items() for alias in aliases
}
_BOX_PATTERNS = (
    re.compile(r"(?P<qty>\d+)\s*[*xX×]\s*(?P<type>\d{2}[A-Za-z]+)"),
    re.compile(r"(?P<type>\d{2}[A-Za-z]+)\s*[*xX×]\s*(?P<qty>\d+)"),
)


def _fields(text: str) -> dict[str, str]:
    return {
        _ALIAS_TO_FIELD[label.casefold()]: value
        for label, value in parse_source_fields(text).items()
        if label.casefold() in _ALIAS_TO_FIELD
    }


def parse_source_fields(text: str) -> dict[str, str]:
    """Return labels and values as written, excluding only surrounding delimiters."""
    result: dict[str, str] = {}
    for segment in re.split(r"[；;\n]+", text):
        match = re.fullmatch(r"\s*([^：:]+?)\s*[：:]\s*(.*?)\s*", segment)
        if match and match.group(2):
            result[match.group(1)] = match.group(2)
    return result


def _split_vessel_voyage(value: str) -> tuple[str | None, str | None]:
    match = re.fullmatch(r"(.+?)\s+(?:V\.?|VOY\.?)\s*([A-Za-z0-9-]+)", value, re.IGNORECASE)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    parts = [part.strip() for part in re.split(r"[/／]", value, maxsplit=1)]
    if len(parts) == 2:
        return parts[0] or None, parts[1] or None
    return value, None


def _parse_boxes(value: str | None) -> list[BoxItem]:
    if not value:
        return []
    boxes: list[BoxItem] = []
    for pattern in _BOX_PATTERNS:
        matches = list(pattern.finditer(value))
        if matches:
            boxes.extend(
                BoxItem(
                    b_type=match.group("type"),
                    box_num=int(match.group("qty")),
                )
                for match in matches
            )
            break
    return boxes


def extract_order_text(text: str) -> tuple[OrderTextExtraction, dict[str, Any]]:
    fields = _fields(text)
    vessel = fields.get("b_ship_name")
    voyage = fields.get("b_ship_num")
    if combined := fields.get("vessel_voyage"):
        vessel, voyage = _split_vessel_voyage(combined)

    cargo_values = {
        "j": fields.get("packages"),
        "m": fields.get("gross_weight"),
        "t": fields.get("volume"),
        "hh": fields.get("cargo_name"),
        "mt": fields.get("marks"),
    }
    loading_time = fields.get("loading_time")
    extracted = OrderTextExtraction.model_validate(
        {
            "order_num1": fields.get("order_num1"),
            "c_title": fields.get("c_title"),
            "c_name": fields.get("c_name"),
            "c_phone": fields.get("c_phone"),
            "b_ship_name": vessel,
            "b_ship_num": voyage,
            "b_ship_company": fields.get("b_ship_company"),
            "factory_name": fields.get("factory_name"),
            "factory_bei": fields.get("factory_bei"),
            "b_factory_not": fields.get("b_factory_not"),
            "b_start_dock": fields.get("b_start_dock"),
            "b_end_port": fields.get("b_end_port"),
            "b_end_dock": fields.get("b_end_dock"),
            "b_wharf": fields.get("b_wharf"),
            "b_open_ship_time": fields.get("b_open_ship_time"),
            "c_sn": fields.get("c_sn"),
            "c_note": fields.get("c_note"),
            "data": [cargo_values] if any(cargo_values.values()) else [],
            "box": _parse_boxes(fields.get("box_text")),
            "driver": [DriverItem(b_date=loading_time)] if loading_time else [],
        }
    )
    return extracted, {"extractor": "explicit_labels", "value_mode": "verbatim"}
