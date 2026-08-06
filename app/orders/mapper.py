"""校验文本订单抽取结果并生成下游 data 参数。"""

from __future__ import annotations

from typing import Any

from app.errors import SkillAPIError

from .schema import OrderTextExtraction


class OrderNotReadyError(SkillAPIError):
    http_status = 422
    code = "order_not_ready"


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _drop_none(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value


def build_order_data(
    extracted: OrderTextExtraction,
    *,
    customer_id: str = "",
) -> dict[str, Any]:
    missing_fields = [
        field
        for field, value in {
            "order_num1": extracted.order_num1,
            "c_title": extracted.c_title,
        }.items()
        if not value or not value.strip()
    ]
    if missing_fields:
        raise OrderNotReadyError(
            "text does not contain all required order fields",
            details={"missing_fields": missing_fields},
        )

    data = extracted.model_dump()
    data["type"] = 1
    # 客户 ID 已无配置来源，仅在显式传入时注入（缺省不发送）
    if customer_id:
        data["c_id"] = customer_id
    if not data["data"]:
        data["data"] = [{"b_order_num": extracted.order_num1}]
    else:
        for cargo in data["data"]:
            cargo["b_order_num"] = cargo.get("b_order_num") or extracted.order_num1
    return _drop_none(data)
