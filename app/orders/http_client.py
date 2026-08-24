"""第三方 HTTP 调用统一封装：请求/响应/异常全量结构化日志，便于排查下游错误。

覆盖本项目全部业务下游 POST（下单 publishCreateOrder / AddWork、建档、舱单
addBill），调用方无需自行记录请求与响应——每条调用产出三条日志：

- ``third_party_request``：URL、payload 摘要（发出前记录，挂起也能还原现场）
- ``third_party_response``：状态码、耗时、响应体摘要
- ``third_party_request_error``：网络异常类型与耗时，随后原样抛出异常
  （不吞，由调用方按既有语义处理，如按单隔离失败）

约束：
- 请求体/响应体一律截断（默认 2000 字符），防超大响应撑爆日志；
- headers 值不落日志（sk 等凭据不泄露），仅透传给下游；
- 调用参数与 httpx.post 语义一致（url/data|json/headers/timeout），
  测试 monkeypatch httpx.post 的既有用例不受影响。
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx

from app.logging_conf import get_logger
from app.third_party_log import record as record_third_party

log = get_logger(__name__)

_BODY_PREVIEW_MAX = 2000


def _preview(body: Any) -> str:
    """请求体/响应体 → 截断文本（不可 JSON 序列化时退化 str()）。"""
    if body is None:
        return ""
    if isinstance(body, str):
        text = body
    else:
        try:
            text = json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(body)
    return text[:_BODY_PREVIEW_MAX]


def _nan_to_none(_token: str) -> None:
    """下游响应 NaN/Infinity 字面量 → None（对齐 bill 模块顶层 json.loads 防御）。"""
    return None


def _unpack_json_strings(
    value: Any, *, max_chars: int = _BODY_PREVIEW_MAX
) -> Any:
    """递归展开嵌套 JSON 字符串（下游响应常见整段 JSON 被序列化成字符串）。

    - NaN/Infinity 字面量 → None（防 JSONResponse 序列化 500）；
    - 字符串值截断到 max_chars（对象进入错误响应/日志时有界）；
    - 超深嵌套抛 RecursionError 时原样返回该子树。
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                # parse_constant：嵌套串中的 NaN/Infinity 一并转 None，
                # 否则复活为 float 后 JSONResponse 序列化会 500
                return _unpack_json_strings(
                    json.loads(stripped, parse_constant=_nan_to_none),
                    max_chars=max_chars,
                )
            except (ValueError, RecursionError):
                return value[:max_chars]
        return value[:max_chars]
    if isinstance(value, dict):
        return {
            key: _unpack_json_strings(item, max_chars=max_chars)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_unpack_json_strings(item, max_chars=max_chars) for item in value]
    return value


def unpack_json(value: Any) -> Any:
    """递归展开嵌套 JSON 字符串，返回结构化对象（下游错误透传可读化）。

    与 pretty_json 的区别：返回对象而非字符串——放入 JSON 响应后由序列化器
    自然呈现为嵌套多行，而不是整段转义文本（\n/\" 可见）。
    字符串值限长 _BODY_PREVIEW_MAX（2000），保证对象进入错误响应/日志时有界。
    """
    return _unpack_json_strings(value)


def pretty_json(value: Any, *, max_chars: int = 2000) -> str:
    """递归展开嵌套 JSON 字符串后美化输出（日志等字符串场景），超长截断。

    例：``{"parmasData": "{\"order_num1\": \"x\"}"}`` →
    ``parmasData`` 的值展开为多行 JSON，而非一行转义文本。
    """
    try:
        text = json.dumps(unpack_json(value), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(value)
    return text[:max_chars]


def _post(
    *,
    name: str,
    url: str,
    timeout: float,
    payload_kind: str,
    payload: Any,
    headers: dict[str, str] | None,
) -> httpx.Response:
    """POST 并记录请求/响应/异常日志；网络异常记录后原样抛出。"""
    started = time.monotonic()
    request_extra = {
        "endpoint": name,
        "method": "POST",
        "url": url,
        "payload_kind": payload_kind,
        "body_preview": _preview(payload),
    }
    log.info("third_party_request", extra=request_extra)
    record_third_party("third_party_request", "INFO", **request_extra)
    try:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if headers:
            kwargs["headers"] = headers
        if payload_kind == "json":
            kwargs["json"] = payload
        else:
            kwargs["data"] = payload
        response = httpx.post(url, **kwargs)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        error_extra = {
            "endpoint": name,
            "method": "POST",
            "url": url,
            "error_type": exc.__class__.__name__,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        log.warning("third_party_request_error", extra=error_extra)
        record_third_party("third_party_request_error", "WARNING", **error_extra)
        raise
    response_extra = {
        "endpoint": name,
        "method": "POST",
        "url": url,
        "status_code": response.status_code,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "body_preview": _preview(getattr(response, "text", "")),
    }
    log.info("third_party_response", extra=response_extra)
    record_third_party("third_party_response", "INFO", **response_extra)
    return response


def post_json(
    url: str,
    payload: Any,
    *,
    name: str,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST JSON body（content-type 由 httpx 默认 application/json）。"""
    return _post(
        name=name,
        url=url,
        timeout=timeout,
        payload_kind="json",
        payload=payload,
        headers=headers,
    )


def post_form(
    url: str,
    data: dict[str, Any],
    *,
    name: str,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST 表单（httpx data= 语义，x-www-form-urlencoded）。"""
    return _post(
        name=name,
        url=url,
        timeout=timeout,
        payload_kind="form",
        payload=data,
        headers=headers,
    )
