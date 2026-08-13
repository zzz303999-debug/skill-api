"""FastAPI 入口。

启动时：
1. 加载 logging
2. discover() 自动扫描 app.skills 下所有子包并注册
3. 为每个已注册的 skill 动态挂一条 `POST /skills/{name}/extract` 路由，
   并把 skill 自己的 `output_model` 作为 response schema，OpenAPI 自动生成精确文档。
"""

from __future__ import annotations

import asyncio
import hmac
import math
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, create_model

from app import access_log, rate_limit
from app.config import settings
from app.core import registry
from app.core.skill_base import SkillBase, SkillMeta
from app.errors import (
    ERROR_CODE_DESCRIPTIONS,
    BadRequestError,
    ServiceBusyError,
    SkillAPIError,
)
from app.logging_conf import get_logger, setup_logging
from app.orders import (
    CreateOrderFromTextRequest,
    CreateOrderFromTextResponse,
    ParseDocumentResponse,
    build_order_data,
    extract_order_text,
    parse_document_to_order,
    parse_source_fields,
    publish_create_order,
)
from app.orders.bill import BillParseResult, build_result

setup_logging()
log = get_logger(__name__)

app = FastAPI(
    title="skill-api",
    version="0.1.0",
    description="Multi-skill extraction API service backed by an OpenAI-compatible LLM.",
)

# Skill.run 是同步契约，统一放到有界线程池，避免文件转换和 LLM 请求阻塞事件循环，
# 同时限制对 LLM 服务的并发压力。
_skill_executor = ThreadPoolExecutor(
    max_workers=settings.skill_max_concurrency,
    thread_name_prefix="skill-runner",
)
# 在途任务信号量：与线程池 worker 数一致，防止无界排队导致内存膨胀
_inflight_semaphore = asyncio.Semaphore(settings.skill_max_concurrency)

# 不记录日志接口自身与静态页面，避免自动轮询刷屏日志
_SKIP_ACCESS_LOG_PATHS = {"/logs", "/api/logs", "/favicon.ico"}

# 鉴权豁免路径：健康检查、OpenAPI 文档与日志页面本身（页面无数据）；
# /api/logs 日志数据接口含 PII，不在豁免内，必须鉴权才能查看。
_AUTH_FREE_PATHS = frozenset(
    {"/healthz", "/skills", "/docs", "/redoc", "/openapi.json", "/favicon.ico", "/logs"}
)


# 请求限流（内存滑动窗口，按客户端 IP；单进程部署下精确）
_heavy_limiter = rate_limit.SlidingWindowLimiter(
    max_requests=settings.rate_limit_heavy_max_requests,
    window_seconds=settings.rate_limit_heavy_window_seconds,
)
_light_limiter = rate_limit.SlidingWindowLimiter(
    max_requests=settings.rate_limit_light_max_requests,
    window_seconds=settings.rate_limit_light_window_seconds,
)
_rate_limit_whitelist = frozenset(
    ip.strip() for ip in settings.rate_limit_whitelist.split(",") if ip.strip()
)
_LIMITERS = {"heavy": _heavy_limiter, "light": _light_limiter}


def _resolve_client_ip(request: Request) -> tuple[str | None, str | None]:
    """解析客户端 IP，返回 (客户端IP, 原始X-Forwarded-For头)。

    云服务前面通常有 nginx/负载均衡，access_log_trust_proxy 开启时：
    - X-Forwarded-For 存在时取**最后一个**地址（nginx `$proxy_add_x_forwarded_for`
      是追加语义，最后一个即离本服务最近的代理看到的真实客户端 IP）；
      客户端自行伪造的前缀地址被忽略，限流与审计 IP 不可被污染；
    - 其次 X-Real-IP；均不存在或未开启信任时回退到直连地址。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if settings.access_log_trust_proxy:
        if forwarded:
            parts = [part.strip() for part in forwarded.split(",") if part.strip()]
            if parts:
                return parts[-1], forwarded
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip(), forwarded
    host = request.client.host if request.client else None
    return host, forwarded


@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next: Callable) -> Any:
    """请求限流：按客户端 IP 对 heavy/light 档接口滑动窗口计数。

    注册在 access_log 中间件之前（执行时处于其内层），被 429 拒绝的请求
    仍会经过外层 access_log，审计留痕完整；限流计数在事件循环线程执行。
    """
    if not settings.rate_limit_enabled:
        return await call_next(request)
    group = rate_limit.classify_path(request.url.path)
    if group is None:
        return await call_next(request)
    client_ip, _ = _resolve_client_ip(request)
    if not client_ip or client_ip in _rate_limit_whitelist:
        return await call_next(request)
    allowed, retry_after = _LIMITERS[group].allow(client_ip)
    if not allowed:
        request.state.error_code = "rate_limited"
        request.state.error_detail = {
            "code": "rate_limited",
            "message": "too many requests",
            "description": ERROR_CODE_DESCRIPTIONS["rate_limited"],
            "details": {"retry_after_seconds": math.ceil(retry_after)},
        }
        return rate_limit.build_rate_limited_response(retry_after)
    return await call_next(request)


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


@app.middleware("http")
async def _auth_middleware(request: Request, call_next: Callable) -> Any:
    """接口鉴权：配置了 api_key 时校验 Bearer / X-API-Key。

    注册在 access_log 与 rate_limit 之间：未授权请求同样留下审计记录，
    且不消耗限流配额。未配置 api_key 时直接放行（仅限可信内网部署）。
    """
    if not settings.api_key:
        return await call_next(request)
    if request.url.path in _AUTH_FREE_PATHS:
        return await call_next(request)
    # 常量时间比较，避免时序侧信道泄露 api_key 信息
    auth = request.headers.get("authorization", "")
    auth_ok = False
    if auth.startswith("Bearer "):
        auth_ok = hmac.compare_digest(auth[7:].encode(), settings.api_key.encode())
    header_key = request.headers.get("x-api-key")
    if not auth_ok and header_key:
        auth_ok = hmac.compare_digest(header_key.encode(), settings.api_key.encode())
    if auth_ok:
        return await call_next(request)
    request.state.error_code = "unauthorized"
    request.state.error_detail = {
        "code": "unauthorized",
        "message": "invalid or missing API key",
        "description": ERROR_CODE_DESCRIPTIONS["unauthorized"],
        "details": None,
    }
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "code": "unauthorized",
                "message": "invalid or missing API key",
                "description": ERROR_CODE_DESCRIPTIONS["unauthorized"],
                "details": None,
            }
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


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


@app.middleware("http")
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
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "payload_too_large",
                    "message": "request body too large",
                    "description": ERROR_CODE_DESCRIPTIONS["payload_too_large"],
                    "details": {"max_bytes": max_bytes},
                }
            },
        )
    except Exception:
        raise
    finally:
        error_detail = getattr(request.state, "error_detail", None)
        if error_detail is None and status_code >= 400:
            # 非业务异常（如 FastAPI 校验 422）也记录错误信息，保证审计完整
            error_detail = {
                "code": f"http_{status_code}",
                "message": f"HTTP {status_code}",
                "details": None,
            }
        access_log.record(
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
                "status": status_code,
                "error_code": getattr(request.state, "error_code", None),
                "error": error_detail,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            }
        )
    return response


@app.exception_handler(SkillAPIError)
async def _skill_api_error_handler(_: Request, exc: SkillAPIError) -> JSONResponse:
    log.warning(
        "skill_api_error",
        extra={
            "code": exc.code,
            "error_message": exc.message,
            "description": exc.description,
            "details": exc.details,
        },
    )
    _.state.error_code = exc.code
    # 完整错误详情（含中文说明）透传访问日志，供审计导出错误信息
    _.state.error_detail = {
        "code": exc.code,
        "message": exc.message,
        "description": exc.description,
        "details": exc.details,
    }
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "description": exc.description,
                "details": exc.details,
            }
        },
    )


# ---- 健康检查：依赖配置检查 + 可达性探测 ----
# /healthz 的 dependencies：只验可达性，不调 chat/completions（避免计费与推理消耗）；
# 订单上游接口为 POST 下单端点（请求即下单），绝不实际探测，仅做配置级检查。

_LLM_PROBE_PATH = "/models"


def _probe_order_config() -> str:
    """订单上游仅做配置级检查：接口地址已配置即视为可下单。"""
    return "ok" if settings.order_api_url else "not_configured"


async def _http_probe(base_url: str, path: str) -> str:
    """通用可达性探测：任何 HTTP 响应（含 401/4xx）都视为网关可达，
    仅连接失败/超时视为 unreachable。
    """
    try:
        async with httpx.AsyncClient(timeout=settings.health_probe_timeout_seconds) as client:
            await client.get(f"{base_url.rstrip('/')}{path}")
        return "ok"
    except (httpx.HTTPError, OSError):
        return "unreachable"


async def _probe_llm() -> str:
    """探测 LLM 网关：GET {base_url}/models；未配置（缺 key）不发起请求。"""
    if not settings.llm_api_key or not settings.llm_base_url:
        return "not_configured"
    return await _http_probe(settings.llm_base_url, _LLM_PROBE_PATH)


async def _probe_mineru() -> str:
    """探测 MinerU：GET {base_url}/；未启用（可降级依赖）返回 disabled。"""
    if not settings.mineru_enabled or not settings.mineru_base_url:
        return "disabled"
    return await _http_probe(settings.mineru_base_url, "/")


async def _probe_dependencies() -> dict[str, str]:
    """并行探测三个依赖；探测关闭时全部标记 skipped（测试隔离/自定义探活）。"""
    if not settings.health_probe_enabled:
        return {"llm": "skipped", "mineru": "skipped", "order_api": "skipped"}
    llm_status, mineru_status = await asyncio.gather(_probe_llm(), _probe_mineru())
    return {
        "llm": llm_status,
        "mineru": mineru_status,
        "order_api": _probe_order_config(),
    }


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    """存活检查 + 依赖状态。

    status 恒为 "ok"（进程存活，Docker healthcheck 与鉴权豁免语义不变）；
    dependencies 反映 LLM/MinerU/订单上游的配置与可达状态，供监控与人工
    排障；探测失败不影响 HTTP 200，避免网络抖动误判容器不健康。
    """
    return {
        "status": "ok",
        "skills": [s.name for s in registry.all_skills()],
        "dependencies": await _probe_dependencies(),
    }


@app.get("/skills", response_model=list[SkillMeta], tags=["meta"])
def list_skills() -> list[SkillMeta]:
    return [s.meta() for s in registry.all_skills()]


@app.get("/api/logs", tags=["meta"])
def list_request_logs(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    path: str | None = Query(default=None),
    file: str | None = Query(default=None),
    status: int | None = Query(default=None, ge=100, le=599),
    request_id: str | None = Query(default=None),
    ip: str | None = Query(default=None),
) -> dict:
    """请求访问日志列表（时间倒序），支持分页与过滤。"""
    return access_log.query(
        limit=limit,
        offset=offset,
        path=path,
        file=file,
        status=status,
        request_id=request_id,
        ip=ip,
    )


@app.get("/logs", include_in_schema=False)
def request_logs_page() -> FileResponse:
    """内置的请求日志查看页面。"""
    static_dir = Path(__file__).resolve().parent / "static"
    return FileResponse(static_dir / "logs.html")


def _typed_response_model(skill: SkillBase) -> type[BaseModel]:
    """为每个 skill 动态生成一个精确类型的响应模型：
    data 字段的类型 = skill.output_model，这样 OpenAPI 就能显示精确 schema。
    """
    data_type: Any = skill.output_model if skill.output_model else dict
    fields: dict[str, Any] = {
        "skill": (str, skill.name),
        "version": (str, skill.version),
        "data": (data_type, ...),
        "meta": (dict, Field(default_factory=dict)),
    }
    if skill.include_content:
        fields["content"] = (str, ...)
    return create_model(
        f"{skill.name.title().replace('-', '')}Response",
        **fields,
        __base__=BaseModel,
    )


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read(settings.api_max_upload_bytes + 1)
    if len(content) > settings.api_max_upload_bytes:
        raise BadRequestError(
            "uploaded file is too large",
            code="file_too_large",
            details={"max_bytes": settings.api_max_upload_bytes},
        )
    if not content:
        raise BadRequestError(
            "uploaded file is empty",
            code="empty_file",
            details={"file": file.filename or "unnamed"},
        )
    return content


async def _run_in_executor(call: Callable[[], Any]) -> Any:
    """在有界线程池中执行同步调用，避免阻塞事件循环。

    在途任务数受 skill_max_concurrency 限制：超限的新请求最多排队
    skill_queue_wait_seconds 秒，仍无空位则返回 503 server_busy，
    防止 LLM/转换任务（持有大文件字节）无界堆积耗尽内存。
    """
    try:
        await asyncio.wait_for(
            _inflight_semaphore.acquire(),
            timeout=settings.skill_queue_wait_seconds,
        )
    except TimeoutError:
        raise ServiceBusyError("server is busy, too many concurrent tasks") from None
    except ValueError:
        # wait_for 超时取消与 release() 的竞争：等待者 future 已被弹出，
        # acquire 未成功，按繁忙处理（避免裸 500）
        raise ServiceBusyError("server is busy, too many concurrent tasks") from None
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_skill_executor, call)
    finally:
        _inflight_semaphore.release()


async def _run_skill(skill: SkillBase, content: bytes, filename: str) -> dict:
    return await _run_in_executor(
        partial(
            skill.run,
            file_bytes=content,
            filename=filename,
            options=None,
        )
    )


async def _publish_order(
    order_data: dict[str, Any],
    *,
    room_id: str,
    user_id: str,
) -> dict[str, Any]:
    return await _run_in_executor(
        partial(publish_create_order, order_data, room_id=room_id, user_id=user_id)
    )


async def _extract_order_text(text: str):
    return await _run_in_executor(partial(extract_order_text, text))


@app.post(
    "/orders",
    response_model=CreateOrderFromTextResponse,
    tags=["orders"],
    summary="Extract and create an order from free text",
)
async def create_order_from_text(body: CreateOrderFromTextRequest) -> dict[str, Any]:
    text = body.content.strip()
    encoded = text.encode("utf-8")
    if len(encoded) > settings.api_max_upload_bytes:
        raise BadRequestError(
            "text is too large",
            code="text_too_large",
            details={"max_bytes": settings.api_max_upload_bytes},
        )

    extracted, meta = await _extract_order_text(text)
    order_data = build_order_data(extracted)
    upstream = await _publish_order(
        order_data,
        room_id=body.roomId,
        user_id=body.userId,
    )
    return {
        "roomId": body.roomId,
        "source_fields": parse_source_fields(text),
        "extracted": extracted.model_dump(),
        "order_data": order_data,
        "upstream": upstream,
        "meta": meta,
    }


async def _parse_document_to_order(file_bytes: bytes, filename: str):
    return await _run_in_executor(
        partial(
            parse_document_to_order,
            file_bytes,
            filename,
        )
    )


@app.post(
    "/orders/parse-document",
    response_model=ParseDocumentResponse,
    tags=["orders"],
    summary="Parse an uploaded document into order fields without creating an order",
)
async def parse_order_document(
    file: Annotated[UploadFile, File()],
    request: Request,
) -> dict[str, Any]:
    """上传附件（托书/做箱通知等），转换为下单接口字段但不实际下单。

    必填字段：提单号（≥8 位纯数字或字母数字）、箱型（4 位）、客户、
    地址、做箱日期、件数、毛重、体积。字段缺失或格式不合法时不报错，
    返回 200 + needs_manual_confirmation=true + missing_fields（缺失字段）
    + missing_reasons（缺失原因：原文未找到，请人工确认 / 格式不合法）。
    order_data 始终返回（缺失项为 null，做箱日期缺失时 driver 为 [{}]），
    由调用方人工确认后补充并提交。
    """
    request.state.file_name = file.filename or "unnamed"
    content = await _read_upload(file)
    request.state.file_size = len(content)
    return await _parse_document_to_order(content, file.filename or "unnamed")


@app.post(
    "/orders/bill/import",
    response_model=BillParseResult,
    tags=["orders"],
    summary="Import a competitor bill and optionally create orders",
)
async def import_bill(
    file: Annotated[UploadFile, File()],
    request: Request,
    create_order: bool = Form(default=False),
) -> BillParseResult:
    """上传竞品应收对账单（.xls/.xlsx/.xlsm），解析归集后返回订单预览。

    create_order 缺省 false（只预览不下单）；显式传 true 时逐单创建订单
    （GetWebKey → login → AddWork 链路，见 app/orders/bill/client.py），
    响应附 orders[].create_result 与 summary；凭证获取失败返回 502。
    表头识别/格式校验/坏文件等由 parse_bill 覆盖，错误统一走全局异常处理；
    请求自动记录访问日志（文件名/大小/耗时/状态码）。
    """
    request.state.file_name = file.filename or "unnamed"
    content = await _read_upload(file)
    request.state.file_size = len(content)
    return await _run_in_executor(
        partial(
            build_result,
            filename=file.filename or "unnamed",
            file_bytes=content,
            create_order=create_order,
        )
    )


def _make_extract_route(skill: SkillBase):
    """为一个 skill 生成一个 handler。闭包捕获 skill 实例。"""

    async def handler(
        file: Annotated[UploadFile, File()],
        request: Request,
    ) -> dict[str, Any]:
        # 先设置文件名，读取失败时访问日志也能记录到 file
        request.state.file_name = file.filename or "unnamed"
        content = await _read_upload(file)
        request.state.file_size = len(content)
        out = await _run_skill(skill, content, file.filename or "unnamed")
        response = {
            "skill": skill.name,
            "version": skill.version,
            "data": out["result"],
            "meta": out.get("meta", {}),
        }
        if skill.include_content:
            response["content"] = out.get("content", "")
        return response

    return handler


def _make_batch_extract_route(skill: SkillBase):
    """为一个 skill 生成批量处理 handler。接收多个文件，并行识别后汇总。"""

    async def handler(
        files: Annotated[list[UploadFile], File()],
        request: Request,
    ) -> dict[str, Any]:
        request.state.file_name = ", ".join(f.filename or "unnamed" for f in files)
        request.state.file_size = 0
        if not files:
            raise BadRequestError(
                "no files uploaded in the batch",
                code="empty_batch",
                details={"min_files": 1},
            )
        if len(files) > settings.api_batch_max_files:
            raise BadRequestError(
                "too many files in one batch",
                code="too_many_files",
                details={"max_files": settings.api_batch_max_files},
            )

        async def run_one(f: UploadFile) -> dict:
            try:
                content = await _read_upload(f)
                request.state.file_size += len(content)
                out = await _run_skill(skill, content, f.filename or "unnamed")
                result = {
                    "file": f.filename or "unnamed",
                    "result": out["result"],
                    "meta": out.get("meta", {}),
                }
                if skill.include_content:
                    result["content"] = out.get("content", "")
                return result
            except SkillAPIError as e:
                return {
                    "file": f.filename or "unnamed",
                    "error": {
                        "code": e.code,
                        "message": e.message,
                        "description": e.description,
                        "details": e.details,
                    },
                }
            except Exception:
                log.exception("batch_skill_failed", extra={"file": f.filename or "unnamed"})
                return {
                    "file": f.filename or "unnamed",
                    "error": {
                        "code": "internal_error",
                        "message": "skill execution failed",
                        "description": ERROR_CODE_DESCRIPTIONS["internal_error"],
                    },
                }

        # 并行执行所有文件识别
        raw_results = await asyncio.gather(*[run_one(f) for f in files])
        results = [r for r in raw_results if "error" not in r]
        errors = [r for r in raw_results if "error" in r]
        return {
            "skill": skill.name,
            "version": skill.version,
            "total": len(files),
            "success": len(results),
            "failed": len(errors),
            "results": results,
            "errors": errors,
        }

    return handler


def _register_skill_routes() -> None:
    registry.discover()
    for skill in registry.all_skills():
        handler = _make_extract_route(skill)
        response_model = _typed_response_model(skill)
        app.add_api_route(
            f"/skills/{skill.name}/extract",
            handler,
            methods=["POST"],
            tags=[f"skill:{skill.name}"],
            summary=f"Run skill: {skill.name}",
            description=skill.description,
            response_model=response_model,
        )
        log.info("route_mounted", extra={"path": f"/skills/{skill.name}/extract"})

        # 批量接口
        batch_handler = _make_batch_extract_route(skill)
        app.add_api_route(
            f"/skills/{skill.name}/batch-extract",
            batch_handler,
            methods=["POST"],
            tags=[f"skill:{skill.name}"],
            summary=f"Batch run skill: {skill.name}",
            description=f"批量处理多个文件，逐个调用 {skill.name} skill 识别后汇总。",
        )
        log.info("route_mounted", extra={"path": f"/skills/{skill.name}/batch-extract"})


_register_skill_routes()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
