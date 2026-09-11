"""tuoshu 归一子包：common 簇（P4-1 自 normalizer.py 拆分，行为零变更）。"""

from __future__ import annotations

import re
from typing import Any


def _normalize_dict(data: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    """按别名映射表归一化 dict 的 keys。

    如果原始 key 已经是正确的 schema key，保留不动。
    如果有别名冲突（别名和正式 key 同时存在），正式 key 优先。
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        canonical = aliases.get(key, key)
        stripped_alias = None
        if canonical == key and isinstance(key, str) and re.search(r"\s", key):
            stripped_alias = aliases.get(re.sub(r"\s+", "", key))
            if stripped_alias:
                canonical = stripped_alias
        if canonical in result:
            # 正式 key 已存在：正式 key 的值优先，别名值只补 None 空位
            if key == canonical and stripped_alias is None:
                result[canonical] = value
            elif result[canonical] is None:
                result[canonical] = value
        else:
            result[canonical] = value
    return result


def normalize_label(value: str) -> str:
    """Normalize layout whitespace without changing the value cells."""
    return re.sub(r"\s+", "", value).rstrip("：:").lower()


def extract_number(value: str | None, *, integer: bool = False) -> int | float | None:
    """Extract the first number embedded in a value (e.g. ``约 1200 KG``).

    ``integer=True`` 时只接受整数：非整数值（如 1200.5）返回 None，
    避免 float 落入 int 字段导致整份结果 schema 校验失败（502）。
    """
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    if not match:
        return None
    number = float(match.group(0))
    if integer:
        return int(number) if number.is_integer() else None
    return number


_CLEAN_NUMBER_LEAD = re.compile(
    r"^(?:约|大约|大概|約|approx(?:oximately)?\.?|about)\s*", re.IGNORECASE
)


_CLEAN_NUMBER_TRAIL = re.compile(r"(?:左右|上下|以内|以上)$")


def _clean_number(value: Any, *, integral: bool) -> tuple[Any, str | None]:
    if value is None or isinstance(value, (int, float)):
        return value, None
    if not isinstance(value, str):
        return value, None
    text = value.replace(",", "").strip()
    # LLM 口语化输出常带前缀（"约 1200KG"），先剥离再匹配数字
    text = _CLEAN_NUMBER_LEAD.sub("", text)
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*([^\d\s].*)?", text)
    if not match:
        return value, None
    number = float(match.group(1))
    if integral:
        if not number.is_integer():
            return value, None
        cleaned: int | float = int(number)
    else:
        cleaned = number
    unit = match.group(2).strip().upper() if match.group(2) else None
    if unit:
        unit = _CLEAN_NUMBER_TRAIL.sub("", unit)
    return cleaned, unit
