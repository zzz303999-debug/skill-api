"""请求访问日志中间件（审计）：记录请求/响应/耗时/状态与错误码。"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.api.middleware.rate_limit import _resolve_client_ip
from app.api.response_shell import _unified_error_body, _use_unified_response
from app.core import access_log_store
from app.core.config import settings
from app.core.errors import ERROR_CODE_DESCRIPTIONS
from app.core.logging_conf import get_logger

log = get_logger(__name__)

# 不记录日志接口自身，避免自动轮询刷屏日志（静态页面已随 2026-09 结构整理移除）
_SKIP_ACCESS_LOG_PATHS = {
    "/api/logs",
    "/api/third-party-logs",
    "/favicon.ico",
}


class _RequestBodyTooLarge(Exception):
    """请求体超过传输层上限（含 chunked 无 Content-Length 场景）。"""


async def _read_stream_limited(request: Request, max_bytes: int) -> bytes | None:
    """流式读取请求体，超过 max_bytes 时返回 None（不继续消费）。

    返回 None 表示超限（外层转 413）；读取过程中的真实异常（客户端中断、
    协议错误等）直接上抛，避免把故障误报为 payload_too_large。
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_body(request: Request) -> tuple[str | None, bool]:
    """读取 JSON 请求体用于审计，返回 (body文本, 是否被截断)。

    只处理 application/json（如 POST /orders）；文件上传等 multipart 请求
    不读 body，避免破坏文件流。读取后缓存回 request，FastAPI 后续解析
    body 参数时直接命中缓存，行为与不读时一致。
    Content-Length 超限/非法或 chunked 读取超限时抛 _RequestBodyTooLarge，
    由外层中间件统一返回 413。
    """
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("application/json"):
        return None, False
    length_header = request.headers.get("content-length", "")
    if length_header:
        try:
            length = int(length_header)
        except ValueError:
            length = -1
        if length < 0 or length > settings.api_max_upload_bytes:
            raise _RequestBodyTooLarge()
    else:
        length = 0
    if length <= 0:
        # chunked 请求：流式限读，超限抛错
        raw = await _read_stream_limited(request, settings.api_max_upload_bytes)
        if raw is None:
            raise _RequestBodyTooLarge()
    else:
        raw = await request.body()
    request._body = raw  # 缓存，供 handler 复用
    text = raw.decode("utf-8", errors="replace")
    max_chars = settings.access_log_body_max_chars
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars], True
    return text, False


async def _capture_json_response(response: Response) -> tuple[Response, str | None, bool]:
    """捕获 JSON 响应体用于审计，返回 (重建的 response, 响应体文本, 是否被截断)。

    消费 body_iterator 后重建响应返回——客户端仍收到完整响应，日志只保留
    截断后的文本（超限标记 response_truncated）。非 JSON 响应（文件下载/
    静态页）原样返回，不消费流。
    """
    content_type = response.headers.get("content-type", "").lower()
    if not content_type.startswith("application/json"):
        return response, None, False
    chunks = [chunk async for chunk in response.body_iterator]
    body = b"".join(chunks)
    rebuilt = Response(
        content=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )
    text = body.decode("utf-8", errors="replace")
    max_chars = settings.access_log_response_max_chars
    if max_chars > 0 and len(text) > max_chars:
        return rebuilt, text[:max_chars], True
    return rebuilt, text, False


# 摘要中保留的最大未映射表头数与建档明细条数（其余只留计数，避免日志膨胀）
_MAX_UNMATCHED_HEADERS_IN_SUMMARY = 20
_MAX_ARCHIVE_ITEMS_IN_SUMMARY = 10


def _summarize_import_response(text: str) -> str | None:
    """把竞品录入（/orders/bill/import）完整响应压缩为排查摘要 JSON。

    保留：文件/结算区间/行数/单数/create_order、建单统计 summary、meta 中
    模板命中/引擎/未映射表头（截断）与基础资料计数；丢弃 orders /
    canonical_orders 全量明细与 upstream 回显——排查定位用摘要足矣，
    完整明细可从客户端响应重新获取。响应统一外壳（code/msg/data）时
    先摘出 code/msg（业务码与展示文案排查直接可见）再解包业务层；非 JSON
    或非对象响应返回 None，调用方保持原文（摘要失败不回退原始大 JSON 的兜底）。
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    code, msg = None, None
    if "data" in data and isinstance(data["data"], dict) and "code" in data:
        # 统一外壳：先摘出 code/msg，再解包业务数据层
        code, msg = data.get("code"), data.get("msg")
        data = data["data"]
    out: dict[str, Any] = {}
    if code is not None or msg is not None:
        out["code"] = code
        out["msg"] = msg
    out.update(
        {
            k: data.get(k)
            for k in ("file", "bill_period", "total_rows", "order_count", "create_order")
        }
    )
    if data.get("summary") is not None:
        out["summary"] = data["summary"]
    meta = data.get("meta") or {}
    out["meta"] = {
        "template": meta.get("template"),
        "parser": meta.get("parser"),
        "raw_rows": meta.get("raw_rows"),
        "source_bytes": meta.get("source_bytes"),
    }
    unmatched = meta.get("unmatched_headers") or []
    if unmatched:
        out["meta"]["unmatched_headers"] = unmatched[:_MAX_UNMATCHED_HEADERS_IN_SUMMARY]
        if len(unmatched) > _MAX_UNMATCHED_HEADERS_IN_SUMMARY:
            out["meta"]["unmatched_truncated"] = (
                len(unmatched) - _MAX_UNMATCHED_HEADERS_IN_SUMMARY
            )
    md = meta.get("master_data")
    if isinstance(md, dict):
        out["meta"]["master_data"] = {
            k: md.get(k) for k in ("mode", "candidates", "degraded")
        }
        # 建档明细保留少量（含 archive_id），其余只留计数
        for key in ("archived", "exists_external", "failed"):
            items = md.get(key) or []
            if items:
                out["meta"]["master_data"][key] = items[:_MAX_ARCHIVE_ITEMS_IN_SUMMARY]
                if len(items) > _MAX_ARCHIVE_ITEMS_IN_SUMMARY:
                    out["meta"]["master_data"][f"{key}_truncated"] = (
                        len(items) - _MAX_ARCHIVE_ITEMS_IN_SUMMARY
                    )
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _summarize_response_for_log(path: str, text: str) -> str | None:
    """按 access_log_summarize_paths 配置对匹配路径的 JSON 响应做摘要。

    非匹配路径或摘要失败返回 None，调用方保留原始响应文本（响应截断标记
    语义不变）。仅作用于成功响应（status < 400）与 409 统一外壳响应；
    其余错误响应本身较小且是排查重点，不摘要。
    """
    needles = [
        p.strip() for p in settings.access_log_summarize_paths.split(",") if p.strip()
    ]
    if not needles or not any(n in path for n in needles):
        return None
    return _summarize_import_response(text)


async def _access_log_middleware(request: Request, call_next: Callable) -> Any:
    """请求访问日志（审计）：记录时间、客户端 IP、UA、方法、路径、上传文件、
    请求体 JSON、耗时、状态与错误码。

    handler 通过 `request.state.file_name`/`request.state.file_size` 透传
    上传文件名与大小，错误处理通过 `request.state.error_code` 透传业务错误码。
    """
    if request.url.path in _SKIP_ACCESS_LOG_PATHS:
        return await call_next(request)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    client_ip, forwarded_for = _resolve_client_ip(request)
    body_text, body_truncated = None, False
    start = time.perf_counter()
    status_code = 500
    response_text, response_truncated = None, False
    try:
        # 传输层硬限制：Content-Length 超限/非法直接 413，避免 Starlette 把
        # 超大 body 全量读入内存导致 OOM。multipart 按批量上限放宽（单文件
        # 大小仍由 handler 逐文件校验），避免 10×2MB 批量上传被整包拒绝。
        content_length_header = request.headers.get("content-length")
        if content_length_header is not None:
            try:
                declared = int(content_length_header)
            except ValueError:
                declared = -1
            transport_limit = settings.api_max_upload_bytes
            if request.headers.get("content-type", "").lower().startswith("multipart/"):
                transport_limit = settings.api_max_upload_bytes * settings.api_batch_max_files
            if declared < 0 or declared > transport_limit:
                request.state.payload_max_bytes = transport_limit
                raise _RequestBodyTooLarge()
        # 请求体读取也可能抛 _RequestBodyTooLarge（chunked 超限），
        # 必须在 try 内，统一返回 413 且 finally 仍会记录审计
        if settings.access_log_record_body:
            body_text, body_truncated = await _read_json_body(request)
        response = await call_next(request)
        status_code = response.status_code
        if settings.access_log_record_response:
            try:
                response, response_text, response_truncated = await _capture_json_response(response)
            except Exception:
                # 捕获失败时响应流可能已被部分消费，继续返回会造成空体与
                # content-length 不匹配；直接抛出使请求走 500，避免静默损坏响应
                log.exception("access_log_response_capture_failed")
                raise
    except _RequestBodyTooLarge:
        # 请求体超限（声明式 Content-Length 或 chunked 流式读取）：统一返回 413，
        # 与 try 内其他路径一致，finally 仍会记录审计
        status_code = 413
        max_bytes = getattr(request.state, "payload_max_bytes", settings.api_max_upload_bytes)
        request.state.error_code = "payload_too_large"
        request.state.error_detail = {
            "code": "payload_too_large",
            "message": "request body too large",
            "description": ERROR_CODE_DESCRIPTIONS["payload_too_large"],
            "details": {"max_bytes": max_bytes},
        }
        content = (
            _unified_error_body(
                "payload_too_large",
                ERROR_CODE_DESCRIPTIONS["payload_too_large"],
                {"max_bytes": max_bytes},
            )
            if _use_unified_response(request.url.path)
            else {
                "error": {
                    "code": "payload_too_large",
                    "message": "request body too large",
                    "description": ERROR_CODE_DESCRIPTIONS["payload_too_large"],
                    "details": {"max_bytes": max_bytes},
                }
            }
        )
        return JSONResponse(status_code=413, content=content)
    except Exception:
        raise
    finally:
        # 竞品录入等大响应：按配置只落排查摘要（客户端响应不受影响，截断
        # 标记同步重置——摘要本身远小于截断上限）；完整响应体单独保留
        # response_full 供导出/取证，受独立上限截断（response_full_truncated）
        response_summarized = False
        response_full = None
        response_full_truncated = False
        # 409 统一外壳响应（重复上传）同样走摘要：保留 code/msg/summary/meta，
        # 避免全量 orders 明细落日志（2026-08-26 审查修正）
        if response_text is not None and (status_code < 400 or status_code == 409):
            summarized = _summarize_response_for_log(request.url.path, response_text)
            if summarized is not None:
                response_full = response_text
                max_full = settings.access_log_response_full_max_chars
                if max_full > 0 and len(response_full) > max_full:
                    response_full_truncated = True
                    response_full = response_full[:max_full]
                response_text = summarized
                response_truncated = False
                response_summarized = True
        error_detail = getattr(request.state, "error_detail", None)
        if error_detail is None and status_code >= 400:
            # 非业务异常（如 FastAPI 校验 422）也记录错误信息，保证审计完整
            error_detail = {
                "code": f"http_{status_code}",
                "message": f"HTTP {status_code}",
                "details": None,
            }
        access_log_store.record(
            {
                "request_id": request_id,
                "ip": client_ip,
                "x_forwarded_for": forwarded_for,
                "user_agent": request.headers.get("user-agent"),
                "method": request.method,
                "path": request.url.path,
                "file": getattr(request.state, "file_name", None),
                "file_size": getattr(request.state, "file_size", None),
                "body": body_text,
                "body_truncated": body_truncated,
                "response": response_text,
                "response_truncated": response_truncated,
                "response_summarized": response_summarized,
                "response_full": response_full,
                "response_full_truncated": response_full_truncated,
                "status": status_code,
                "error_code": getattr(request.state, "error_code", None),
                "error": error_detail,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            }
        )
    return response
