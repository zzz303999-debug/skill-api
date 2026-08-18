"""订单创建 HTTP 客户端。创建请求不自动重试，避免生成重复订单。"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import settings
from app.errors import SkillAPIError
from app.logging_conf import get_logger

from .schema import OrderApiResponse

log = get_logger(__name__)


class OrderUpstreamError(SkillAPIError):
    http_status = 502
    code = "order_upstream_error"


def publish_create_order(
    order_data: dict[str, Any],
    *,
    room_id: str,
    user_id: str,
) -> dict[str, Any]:
    # 下游仅需业务数据、userId（由上游经 /orders 透传）与 roomId，不再携带 apiKeyInfo
    payload = {
        "data": order_data,
        "userId": user_id,
        "roomId": room_id,
    }
    try:
        response = httpx.post(
            settings.order_api_url,
            json=payload,
            timeout=settings.order_api_timeout_seconds,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("order_api_network_error", extra={"error_type": exc.__class__.__name__})
        raise OrderUpstreamError(
            "order API network error",
            details={"error_type": exc.__class__.__name__},
        ) from exc

    if not response.is_success:
        log.warning("order_api_http_error", extra={"status_code": response.status_code})
        raise OrderUpstreamError(
            "order API returned an HTTP error",
            details={
                "status_code": response.status_code,
                "upstream_response": response.text[:2000],
            },
        )
    try:
        raw = response.json()
    except ValueError as exc:
        raise OrderUpstreamError(
            "order API returned a non-JSON response",
            details={
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "body_preview": response.text[:500],
            },
        ) from exc

    # Some legacy gateways wrap the JSON object in a JSON string.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OrderUpstreamError(
                "order API returned JSON with an unexpected string payload",
                details={"body_preview": raw[:500]},
            ) from exc
    try:
        parsed = OrderApiResponse.model_validate(raw)
    except ValidationError as exc:
        raise OrderUpstreamError(
            "order API response does not match its documented structure",
            details={
                "response_type": type(raw).__name__,
                "response_keys": sorted(raw) if isinstance(raw, dict) else [],
                "validation_errors": exc.errors(include_input=False),
            },
        ) from exc

    if str(parsed.code) != "200":
        # 完整透传下游错误（含原始响应体），便于排查：
        # 如 "no: userId"（缺凭据）、“no: xxxx”（业务校验失败）等
        raise OrderUpstreamError(
            f"order API rejected the order: {parsed.msg}",
            details={
                "upstream_code": parsed.code,
                "upstream_message": parsed.msg,
                "upstream_response": (
                    json.dumps(raw, ensure_ascii=False, indent=2)[:2000]
                    if not isinstance(raw, str)
                    else raw[:2000]
                ),
            },
        )
    log.info("order_created", extra={"upstream_code": parsed.code})
    return parsed.model_dump()
