"""竞品账单导入下游客户端：sk 由调用方透传 → AddWork 表单逐单下单。

对齐《竞品账单导入接口文档》v1.4 §2.2/§3.7/§5.3：
- 鉴权：sk 经 /orders/bill/import 请求头透传（服务端不再换取凭证），下单不做缓存
- 下单通道固定 AddWork 表单（AddWork 端点 + sk 头 + create_order=true 可直连
  下单；publishCreateOrder 不可行，json 通道已移除）
- 任何情况不自动重试（防重复下单）；单失败不影响后续订单；超时/网络异常 →
  该单 error（order_upstream_error），不中断整批；missing_fields 非空照常提交
- 凭证失效熔断：连续 3 笔凭证类失败（upstream_code=203 /「登录已过期」）→
  主动中止整批（排队单放弃提交、不登记；重传自动续跑），防令牌过期时全量白跑
- 去重（成功单注册表）：**跨批次**按 (提单号+箱号, sk) 维度——同 sk 重导已
  成功键 → skipped（不调下游）；**同批内同键重复行全部提交**（一行一票，
  行级重复不再少录）；提交前查 imported_registry（per-组合键锁包住「查重→
  提交→登记」临界区，owner=sk 哈希不落盘 token 原文）；提单号缺失/非法单由
  service 层文件级连坐拒绝，service 层预判已标记的单直接跳过
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.config import settings
from app.core.http_client import post_form_async, unpack_json
from app.core.logging_conf import get_logger
from app.core.tms_gate import tms_write_slots

from ..schema import BillOrder, CreateResult, OrderError
from .addwork_form import build_add_work_form
from .imported_registry import (
    alock_for,
    dedup_key,
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


# 凭证类失败判据（熔断用）：上游码为准 + 消息标记兜底防文案漂移（TMS 登录态
# 失效实测文案「您的登录已过期，请重新登录！」）
_CREDENTIAL_FAILURE_CODES = ("203",)
_CREDENTIAL_FAILURE_MARKERS = ("登录已过期",)


def _is_credential_failure(result: CreateResult) -> bool:
    """单失败是否属凭证类（TMS 登录态失效）：上游码命中或消息含标记。"""
    error = result.error
    if error is None:
        return False
    details = error.details or {}
    if str(details.get("upstream_code") or "") in _CREDENTIAL_FAILURE_CODES:
        return True
    text = f"{error.message or ''} {details.get('upstream_message') or ''}"
    return any(marker in text for marker in _CREDENTIAL_FAILURE_MARKERS)


def _aborted_result() -> CreateResult:
    """批级熔断中止的兜底结果：未提交下游、未登记（重传自动续跑）。"""
    return CreateResult(
        success=False,
        skipped=False,
        sn=None,
        error=OrderError(
            code="batch_aborted",
            message=(
                "批次已中止：连续多单凭证失效（登录已过期）——剩余订单未提交，"
                "重新登录后重传将自动续跑"
            ),
        ),
    )


class _BatchAbortState:
    """批级凭证失效熔断状态（asyncio 单线程内读写，无锁）。

    连续凭证类失败计数；成功或非凭证类失败清零（偶发 sn 竞态/箱型拒绝
    不误触，失败隔离语义不变）；达阈值 → event 置位，排队单取得并发槽后
    检查并放弃提交（在飞单自然完成）。
    """

    __slots__ = ("threshold", "consecutive", "event")

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.consecutive = 0
        self.event = asyncio.Event()

    def record(self, result: CreateResult) -> bool:
        """登记一笔提交结果；返回 True 表示本次触发中止（调用方记日志）。"""
        if result.success or not _is_credential_failure(result):
            self.consecutive = 0
            return False
        self.consecutive += 1
        if self.consecutive >= self.threshold and not self.event.is_set():
            self.event.set()
            return True
        return False


async def _create_one_async(
    order,
    *,
    sk: str,
    submit,
    owner: str,
    source_sha256: str | None,
    semaphore: asyncio.Semaphore,
    batch_keys: set[str],
    abort_state: _BatchAbortState,
) -> None:
    """单单异步创建（批量编排共用）：

    - service 层预判已标记 → 跳过；
    - 无提单号 → 兜底 missing_bl_no（与 service 层同结构，不提交下游）；
    - 查重→提交→登记在 per-组合键 asyncio.Lock 内原子化（await 让出不阻塞
      异键协程）；有界并发槽限制同时在飞的下单数（不自动重试）；
    - 去重只拦**早于本批**的已建记录（重传/跨批重导防护）；同批内同键
      重复行经 batch_keys 放行、全部提交（一行一票，行级重复不丢）；
    - 凭证熔断置位后：取得并发槽的排队单放弃提交（batch_aborted，不登记）；
      提交结果交 abort_state 计数（连续凭证类失败达阈值触发中止）。
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
    key = dedup_key(bl, container_no=box, fallback=order.row_seq)
    async with alock_for(bl, container_no=box, fallback=order.row_seq):
        rec = get_imported_registry().lookup(
            bl, owner, container_no=box, fallback=order.row_seq
        )
        # 同批内前序行刚登记的键放行（重复行各自成单）；仅历史记录触发跳过
        if rec and key not in batch_keys:
            order.create_result = _skipped_result(rec.get("sn"))
            return
        async with semaphore:
            if abort_state.event.is_set():
                # 凭证失效熔断已触发：排队单放弃提交（未登记，重传自动续跑）
                order.create_result = _aborted_result()
                return
            order.create_result = await submit(sk, order)
        # 计数紧贴提交结果（登记含 to_thread await；延迟计数会让成功单的
        # 清零晚于后续失败，误触熔断）
        tripped = abort_state.record(order.create_result)
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
            batch_keys.add(key)
        if tripped:
            error = order.create_result.error
            log.warning(
                "jxt_order_batch_aborted",
                extra={
                    "consecutive": abort_state.consecutive,
                    "threshold": abort_state.threshold,
                    "upstream_code": (error.details or {}).get("upstream_code")
                    if error
                    else None,
                    "bl_no": bl,
                },
            )


# ---- 进程级 TMS 写通道（用户拍板：修复每请求新建 Semaphore 致 N×C 放大）----
# _create_downstream_slots：全局 TMS 写通道（core.tms_gate.tms_write_slots，
#   账单下单/建档/费目自举/文本下单/舱单共用——TMS 侧不支持并发写，服务侧
#   统一串行化）。多批次可异步接收（不设批位闸），写请求按到达序排队、
#   任意时刻至多 1 个在飞。
_create_downstream_slots = tms_write_slots


async def _run_create_batch(
    orders, *, sk: str, submit, source_sha256: str | None = None
) -> None:
    """批量下单骨架：异键有界并发下单（两个入口共用）。

    - 空订单直接返回；owner = sk 哈希（注册表不落盘 token 原文）；
    - gather 并发执行 _create_one_async（同键串行原子、异键有界并行）；
      批内 batch_keys 记录本批已成功登记的键——同批重复行全部提交，
      跨批已建记录仍触发跳过（重传防护）；
    - 凭证失效熔断：连续 3 笔凭证类失败 → 中止批内后续提交
      （剩余单 batch_aborted 未提交不登记，重传自动续跑）。
    """
    if not orders:
        return
    owner = owner_key(sk)
    batch_keys: set[str] = set()  # 本批已成功登记的键（同批重复行放行依据）
    abort_state = _BatchAbortState(3)
    await asyncio.gather(
        *[
            _create_one_async(
                order,
                sk=sk,
                submit=submit,
                owner=owner,
                source_sha256=source_sha256,
                semaphore=_create_downstream_slots,
                batch_keys=batch_keys,
                abort_state=abort_state,
            )
            for order in orders
        ]
    )


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
