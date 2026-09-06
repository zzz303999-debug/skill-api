"""tuoshu 归一子包：llm_output 簇（P4-1 自 normalizer.py 拆分，行为零变更）。"""

from __future__ import annotations

import re
from typing import Any

from app.core.text_normalize import normalize_date_value

from .aliases import (
    _CONTAINER_ALIASES,
    _FACTORY_ALIASES,
    _ORDER_MAPPING_ALIASES,
    _SOURCE_ALIASES,
    _TOP_LEVEL_ALIASES,
)
from .common import _normalize_dict
from .containers import _normalize_container, _split_container_type_qty
from .issues import normalize_review_issues


def _normalize_factory(data: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    if isinstance(data, dict):
        return _normalize_dict(data, _FACTORY_ALIASES)
    return data


def _normalize_source(data: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    if isinstance(data, dict):
        return _normalize_dict(data, _SOURCE_ALIASES)
    return data


def _split_vessel_voyage(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    if "/" in text:
        vessel, voyage = (part.strip() for part in text.rsplit("/", 1))
        voyage = re.sub(r"^V\.\s*", "", voyage, flags=re.IGNORECASE)
        return vessel or None, voyage or None
    match = re.fullmatch(r"(.+?)\s+V\.\s*([^\s]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip() or None, match.group(2).strip() or None
    # 无分隔符：整段视为船名（与订单文本解析器行为一致），纯航次例外
    if re.fullmatch(r"(?:V\.?|VOY\.?)\s*[A-Za-z0-9-]+", text, re.IGNORECASE):
        return None, text
    return text, None


def _merge_remark(data: dict[str, Any], label: str, value: Any) -> None:
    if value is None or value == "":
        return
    item = f"{label}：{value}"
    remark = data.get("remark")
    if isinstance(remark, str) and remark.strip():
        if item not in remark:
            data["remark"] = f"{remark.strip()}；{item}"
    else:
        data["remark"] = item


def normalize_llm_output(data: dict[str, Any]) -> dict[str, Any]:
    """将 LLM 输出的 raw dict 归一化为 TuoshuOutput 期望的字段名。

    在 Pydantic model_validate 之前调用。
    """
    # 顶层字段归一
    result = _normalize_dict(data, _TOP_LEVEL_ALIASES)

    # doc_type 枚举值中文→英文
    _DOC_TYPE_CN_TO_EN = {
        "做箱通知": "PACKING_NOTICE",
        "运输委托书": "TRANSPORT_ORDER",
        "派车托书": "TRUCKING_ORDER",
        "订舱托书": "BOOKING_NOTE",
        "未知": "UNKNOWN",
    }
    doc_type_val = result.get("doc_type")
    if isinstance(doc_type_val, str) and doc_type_val in _DOC_TYPE_CN_TO_EN:
        result["doc_type"] = _DOC_TYPE_CN_TO_EN[doc_type_val]

    # OCR and models frequently use Chinese dates or non-zero-padded slashes.
    # Normalize them before the strict Pydantic date-pattern validation.
    for field in ("etd", "doc_date"):
        result[field] = normalize_date_value(result.get(field), allow_time=False)
    result["loading_time"] = normalize_date_value(
        result.get("loading_time"), allow_time=True
    )

    combined_vessel_voyage = result.pop("vessel_voyage", None)
    vessel, voyage = _split_vessel_voyage(combined_vessel_voyage)
    if vessel and not result.get("vessel"):
        result["vessel"] = vessel
    if voyage and not result.get("voyage"):
        result["voyage"] = voyage

    container_type, container_qty = _split_container_type_qty(
        result.pop("container_type_qty", None)
    )
    if container_type and not result.get("type"):
        result["type"] = container_type
    if container_qty is not None and result.get("qty") is None:
        result["qty"] = container_qty

    _merge_remark(result, "开港时间", result.pop("port_opening_time", None))

    # containers 内部归一
    containers = result.get("containers")
    if isinstance(containers, list):
        result["containers"] = [
            _normalize_container(c) if isinstance(c, dict) else c
            for c in containers
        ]
    elif isinstance(containers, dict):
        # LLM 有时只返回单个 container dict 而不是 list
        result["containers"] = [_normalize_container(containers)]

    # factory 内部归一
    result["factory"] = _normalize_factory(result.get("factory"))
    factory_fields = {
        "address": result.pop("factory_address", None),
        "contact": result.pop("factory_contact", None),
        "phone": result.pop("factory_phone", None),
    }
    if any(value not in (None, "") for value in factory_fields.values()):
        factory = result.get("factory")
        if not isinstance(factory, dict):
            factory = {}
            result["factory"] = factory
        for key, value in factory_fields.items():
            if value not in (None, "") and not factory.get(key):
                factory[key] = value

    # order_mapping 内部归一
    order_mapping = result.get("order_mapping")
    if isinstance(order_mapping, dict):
        result["order_mapping"] = _normalize_dict(order_mapping, _ORDER_MAPPING_ALIASES)

    # review_issues 内部归一
    review_issues = result.get("review_issues")
    if review_issues is not None:
        result["review_issues"] = normalize_review_issues(review_issues)

    # source 内部归一
    result["source"] = _normalize_source(result.get("source"))

    # 处理容器字段泄漏到顶层的问题
    # 这些字段只应该出现在 containers[] 内部，不应出现在顶层
    _CONTAINER_ONLY_KEYS = {
        "type", "qty", "container_no", "seal_no",
        "packages", "packages_unit", "gross_weight_kg", "volume_cbm",
    }
    # 先把顶层的中文容器 key 也归一化（解决 LLM 把容器字段写顶层的问题）
    result = _normalize_dict(result, _CONTAINER_ALIASES)
    leaked = {k: result.pop(k) for k in _CONTAINER_ONLY_KEYS if k in result}
    if leaked:
        leaked = _normalize_container(leaked)
    if leaked and not result.get("containers"):
        # containers 为空，用泄漏字段构建一个 container 项
        result["containers"] = [leaked]
    elif leaked and isinstance(result.get("containers"), list) and result["containers"]:
        first = result["containers"][0]
        if isinstance(first, dict):
            for key, value in leaked.items():
                if first.get(key) is None:
                    first[key] = value

    return result
