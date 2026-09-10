"""竞品账单导入下游客户端：sk 由调用方透传 → AddWork 表单逐单下单。

对齐《竞品账单导入接口文档》v1.4 §2.2/§3.7/§5.3：
- 鉴权：sk 经 /orders/bill/import 请求头透传（服务端不再换取凭证），下单不做缓存
- 下单通道固定 AddWork 表单（AddWork 端点 + sk 头 + create_order=true 可直连
  下单；publishCreateOrder 不可行，json 通道已移除）
- 任何情况不自动重试（防重复下单）；单失败不影响后续订单；超时/网络异常 →
  该单 error（order_upstream_error），不中断整批；missing_fields 非空照常提交
- 重复上传去重（成功单注册表，按 (提单号, sk) 维度）：同 sk 重导已成功单 →
  skipped（不调下游）；提交前查 imported_registry（per-bl_no 锁包住「查重→
  提交→登记」临界区，owner=sk 哈希不落盘 token 原文）；提单号缺失/非法单由
  service 层文件级连坐拒绝，service 层预判已标记的单直接跳过
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import ServiceBusyError
from app.core.http_client import post_form_async, unpack_json
from app.core.logging_conf import get_logger

from ..schema import BillOrder, CreateResult, OrderError
from .addwork_form import build_add_work_form
from .imported_registry import (
    alock_for,
    get_imported_registry,
    normalize,
    owner_key,
)

log = get_logger(__name__)


def _nan_to_none(_token: str) -> None:
    """json.loads parse_constant：NaN/Infinity 字面量 → None。

    防响应序列化失败（allow_nan=False 遇 float nan 抛 ValueError → 500）。
    """

    return None



def _error_result(message: str, *, details: dict[str, Any]) -> CreateResult:
    """单失败 create_result（不抛异常，调用方按单处理，不中断整批）。"""
    return CreateResult(
        success=False,
        sn=None,
        error=OrderError(
            code="order_upstream_error", message=message, details=details
        ),
    )


def _sn_str(sn: Any) -> str | None:
    """下游 sn 归一为 str（模型 sn 为严格 str；下游/注册表回数字时防校验
    异常冒泡整批——单失败隔离优先）。"""
    return str(sn) if sn is not None else None


def _parse_downstream_response(
    response: httpx.Response,
    *,
    step: str,
    log_tag: str,
    want_o_id: bool = False,
) -> CreateResult:
    """下游下单响应 → create_result（AddWork / canonical 两通道共用判定）。

    code "200"（字符串/数字皆可）→ 成功取 data[0].sn（缺 sn 仍成功，sn=None）；
    want_o_id 时同取 data[0].o_id（TMS 通道）；成功时原样保留 data[0] 回显
    （upstream 键，对齐 /orders 的 upstream.data[0]）；其余 → error 三元组
    （不抛异常，调用方按单处理）。网络异常由调用方捕获。
    """
    if response.status_code >= 400:
        return _error_result(
            f"{step} returned an HTTP error: {response.status_code}",
            details={
                "status_code": response.status_code,
                "upstream_response": unpack_json(response.text),
            },
        )
    try:
        raw = response.json(parse_constant=_nan_to_none)
    except ValueError:
        return _error_result(
            f"{step} returned a non-JSON response",
            details={"body_preview": response.text[:500]},
        )
    if not isinstance(raw, dict):
        return _error_result(
            f"{step} response is not a JSON object",
            details={
                "response_type": type(raw).__name__,
                "body_preview": response.text[:500],
            },
        )
    if str(raw.get("code")) != "200":
        # AddWork 日志带 step 键（canonical 无）
        if want_o_id:
            log.warning(
                f"{log_tag}_rejected", extra={"upstream_code": raw.get("code")}
            )
        else:
            log.warning(
                f"{log_tag}_rejected",
                extra={"step": step, "upstream_code": raw.get("code")},
            )
        return _error_result(
            f"{step} rejected the order: {raw.get('msg', '')}",
            details={
                "upstream_code": raw.get("code"),
                "upstream_message": raw.get("msg"),
                "upstream_response": unpack_json(raw),
            },
        )
    data_list = raw.get("data")
    sn = None
    o_id = None
    if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict):
        sn = _sn_str(data_list[0].get("sn"))
        if want_o_id:
            o_id = data_list[0].get("o_id")
    if want_o_id:
        log.info(f"{log_tag}_ok", extra={"sn": sn, "o_id": o_id})
    else:
        log.info(f"{log_tag}_ok", extra={"step": step, "sn": sn})
    if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict):
        # 原始回显（对齐 /orders 的 upstream.data[0]：sns/o_id/c_title 等回写字段）
        return CreateResult(success=True, sn=sn, o_id=o_id, upstream=data_list[0])
    return CreateResult(success=True, sn=sn, o_id=o_id)


def _register_imported(
    bl_no: str,
    owner: str,
    sn,
    source_sha256: str | None,
    container_no: str | None = None,
    fallback: str | None = None,
) -> None:
    """登记创建成功单（去重注册表组合键，owner=sk 哈希）；登记失败仅记日志，不冒泡。

    单已真实创建，登记失败（磁盘满/权限等）不应使响应变失败——丢失记录的
    后果是重导可能重复下单（见 imported_registry 模块 docstring）。
    """
    try:
        get_imported_registry().register(
            bl_no,
            owner,
            sn=sn,
            source_sha256=source_sha256,
            container_no=container_no,
            fallback=fallback,
        )
    except Exception as exc:  # noqa: BLE001 - 防御：登记失败不使成功单变失败
        log.warning(
            "imported_register_failed",
            extra={"bl_no": bl_no, "error_type": exc.__class__.__name__},
        )


def _skipped_result(sn: str | None) -> CreateResult:
    """去重命中（已成功创建过）的 create_result：success=True + skipped 标记。"""
    return CreateResult(success=True, skipped=True, sn=_sn_str(sn))


# ---- 下游提交入口（网络段异步；表单/判定/登记复用纯函数）----


async def add_work_async(sk: str, order_data: dict[str, Any]) -> CreateResult:
    """add_work（生产唯一入口）：网络段走 post_form_async；表单构造/响应判定
    复用 build_add_work_form / _parse_downstream_response。"""
    form = build_add_work_form(order_data)
    try:
        response = await post_form_async(
            settings.jxt_addwork_url,
            form,
            name="AddWork",
            headers={"sk": sk},
            timeout=settings.jxt_timeout_seconds,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning(
            "jxt_add_work_network_error",
            extra={"error_type": exc.__class__.__name__},
        )
        return _error_result(
            f"AddWork network error: {exc.__class__.__name__}",
            details={"error_type": exc.__class__.__name__},
        )
    return _parse_downstream_response(
        response, step="AddWork", log_tag="jxt_order"
    )


async def submit_canonical_async(sk: str, order) -> CreateResult:
    """submit_canonical（生产唯一入口）：payload 构造/日志/响应判定
    （复用 build_order_payload 与 _parse_downstream_response）。"""
    from .payload import build_order_payload

    form, warnings = build_order_payload(order)
    if warnings:
        log.warning(
            "canonical_payload_warning",
            extra={"bl_no": order.bl_no, "warnings": warnings},
        )
    try:
        response = await post_form_async(
            settings.jxt_addwork_url,
            form,
            name="AddWork-canonical",
            headers={"sk": sk},
            timeout=settings.jxt_timeout_seconds,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning(
            "jxt_canonical_order_network_error",
            extra={"error_type": exc.__class__.__name__},
        )
        return _error_result(
            f"order API network error: {exc.__class__.__name__}",
            details={"error_type": exc.__class__.__name__},
        )
    return _parse_downstream_response(
        response, step="order API", log_tag="jxt_canonical_order", want_o_id=True
    )


# ---- 批量编排：并发下单（同键串行原子、异键有界并行）----


def _missing_bl_result() -> CreateResult:
    """提单号缺失的兜底 create_result（与 service 层预判同结构；不提交下游）。"""
    return CreateResult(
        success=False,
        skipped=False,
        sn=None,
        error=OrderError(code="missing_bl_no", message="提单号缺失，未录入"),
    )


async def _create_one_async(
    order,
    *,
    sk: str,
    submit,
    owner: str,
    source_sha256: str | None,
    semaphore: asyncio.Semaphore,
) -> None:
    """单单异步创建（批量编排共用）：

    - service 层预判已标记 → 跳过；
    - 无提单号 → 兜底 missing_bl_no（与 service 层同结构，不提交下游）；
    - 查重→提交→登记在 per-bl_no asyncio.Lock 内原子化（await 让出不阻塞
      异键协程）；有界并发槽限制同时在飞的下单数（不自动重试）。
    """
    if order.create_result is not None:
        return
    bl = normalize(getattr(order, "order_num1", None) or getattr(order, "bl_no", None))
    if not bl:
        order.create_result = _missing_bl_result()
        return
    # 一行一票：去重键=提单号+箱号（旧链路取 container_no；canonical 取首个结构化箱号；
    # 无箱号退化为行序号）
    if getattr(order, "containers", None):
        box = next(
            (c.container_no for c in (order.containers or []) if c.container_no), None
        )
    else:
        box = getattr(order, "container_no", None)
    async with alock_for(bl, container_no=box, fallback=order.row_seq):
        rec = get_imported_registry().lookup(
            bl, owner, container_no=box, fallback=order.row_seq
        )
        if rec:
            order.create_result = _skipped_result(rec.get("sn"))
            return
        async with semaphore:
            order.create_result = await submit(sk, order)
        if order.create_result.success:
            # 登记含磁盘原子写（小文件毫秒级）→ to_thread 避免阻塞事件循环；
            # 仍在键锁内，保持查重→提交→登记原子性
            await asyncio.to_thread(
                _register_imported,
                bl,
                owner,
                order.create_result.sn,
                source_sha256,
                container_no=box,
                fallback=order.row_seq,
            )


# ---- 进程级 create 下游闸（用户拍板：修复每请求新建 Semaphore 致 N×C 放大）----
# _create_batch_guard：并发导入批数上限（超出排队超时 503 server_busy）；
# _create_downstream_slots：全进程 AddWork 在飞路数上限（≤ bill_create_concurrency）。
# 覆盖本模块提交段；闸前的费目自举/建档带幂等登记，503 重试不会重复建档。
_create_batch_guard = asyncio.Semaphore(settings.bill_create_concurrency)
_create_downstream_slots = asyncio.Semaphore(settings.bill_create_concurrency)


async def _run_create_batch(
    orders, *, sk: str, submit, source_sha256: str | None = None
) -> None:
    """批量下单骨架：异键有界并发下单（两个入口共用）。

    - 空订单直接返回；owner = sk 哈希（注册表不落盘 token 原文）；
    - 批级占位：并发请求排队（超 skill_queue_wait_seconds 返 503 server_busy）；
    - gather 并发执行 _create_one_async（同键串行原子、异键有界并行）。
    """
    if not orders:
        return
    owner = owner_key(sk)
    try:
        await asyncio.wait_for(
            _create_batch_guard.acquire(), timeout=settings.skill_queue_wait_seconds
        )
    except TimeoutError:
        raise ServiceBusyError("server is busy, too many concurrent tasks") from None
    try:
        await asyncio.gather(
            *[
                _create_one_async(
                    order,
                    sk=sk,
                    submit=submit,
                    owner=owner,
                    source_sha256=source_sha256,
                    semaphore=_create_downstream_slots,
                )
                for order in orders
            ]
        )
    finally:
        _create_batch_guard.release()


async def create_orders_async(
    orders: list[BillOrder], sk: str, source_sha256: str | None = None
) -> None:
    """create_orders（生产唯一入口）：异键有界并发下单（AddWork 通道）。

    单失败隔离不中断；不自动重试；missing_fields 非空照常提交；去重
    （查重→提交→登记）在 per-bl_no 锁内原子化（first-write-wins）。
    """
    await _run_create_batch(
        orders,
        sk=sk,
        source_sha256=source_sha256,
        submit=lambda sk_, order_: add_work_async(sk_, order_.order_data or {}),
    )


async def create_canonical_orders_async(orders, sk: str, source_sha256: str | None = None) -> None:
    """create_canonical_orders（生产唯一入口，TMS 通道，语义同 create_orders_async）。"""
    await _run_create_batch(
        orders,
        sk=sk,
        source_sha256=source_sha256,
        submit=lambda sk_, order_: submit_canonical_async(sk_, order_),
    )
