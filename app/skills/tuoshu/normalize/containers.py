"""tuoshu 归一子包：containers 簇（P4-1 自 normalizer.py 拆分，行为零变更）。"""

from __future__ import annotations

import re
from typing import Any

from .aliases import _CONTAINER_ALIASES, KNOWN_CONTAINER_TYPES
from .common import _clean_number, _normalize_dict


def _normalize_container(item: dict[str, Any]) -> dict[str, Any]:
    result = _normalize_dict(item, _CONTAINER_ALIASES)
    container_type, container_qty = _split_container_type_qty(
        result.pop("container_type_qty", None)
    )
    if container_type and not result.get("type"):
        result["type"] = container_type
    if container_qty is not None and result.get("qty") is None:
        result["qty"] = container_qty
    if isinstance(result.get("type"), str):
        result["type"] = normalize_container_type(result["type"])

    for field, integral in (
        ("packages", True),
        ("gross_weight_kg", False),
        ("volume_cbm", False),
    ):
        cleaned, unit = _clean_number(result.get(field), integral=integral)
        result[field] = cleaned
        if field == "packages" and unit and not result.get("packages_unit"):
            result["packages_unit"] = unit
    return result


def _split_container_type_qty(value: Any) -> tuple[str | None, int | None]:
    if not isinstance(value, str):
        return None, None
    match = re.fullmatch(r"\s*(\d+)\s*[xX*×＊]\s*([A-Za-z0-9'’\s]+)\s*", value)
    if not match:
        return None, None
    return normalize_container_type(match.group(2)), int(match.group(1))


def normalize_container_type(value: str) -> str:
    """Remove dimensional punctuation while preserving the source type suffix."""
    return re.sub(r"['’\s]", "", value.strip())


def is_known_container_type(value: str) -> bool:
    lookup_key = re.sub(r"['’\s]", "", value).upper()
    return lookup_key in KNOWN_CONTAINER_TYPES
