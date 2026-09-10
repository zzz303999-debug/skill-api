"""竞品账单导入编排层：解析 → 归集 → BillParseResult 响应组装（含 meta 溯源）。

双管线（模板识别后自动分流，见 parser.ParseOutput）：
- 既有流程语义（columns 目标 = BillRow 字段）：group_orders → BillOrder → orders；
  create_order=True 时走既有双通道下单（client.create_orders，行为语义不变）；
  to_canonical 副本仅供内部建档管线（master_data pending）输入，响应不回传
  （R2——前端只读 orders）。
- 标准字段语义（columns 目标 = CanonicalOrder 字段）：group_canonical →
  CanonicalOrder → canonical_orders；create_order=True 时走 TMS form-data 通道
  （payload/client，见 T7）；费用 price_id 回填（T12）+ 费用对账报告（T14）
  在此层统一完成。

L3 模板候选（ParseOutput.new_template，AI 映射通过校验闸门后生成）不再自动
固化：预览界面展示映射结果，人工确认后由调用方调 template_store.save_yaml_template
固化为 templates/{family}_v1.yaml（meta.l3_template 携带候选配置）。既有
storage 固化模板（source="ai"）继续可用（sha1 指纹路径）。凭证失败 502 不逐单、
单失败隔离不中断、不自动重试等行为语义全部沿用。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.core.errors import BadRequestError, LLMError, ParseError
from app.llm import achat_json

from .aggregation.aggregator import group_orders
from .aggregation.canonical_aggregator import group_canonical, to_canonical
from .fees.fee_bootstrap import run_fee_bootstrap_async
from .fees.reconcile import build_fee_reports
from .parsing.ai_header import build_llm_request, validate_ai_result
from .parsing.opener import (
    AiHeaderNeeded,
    open_and_identify,
    parse_ai_header,
    parse_exact_fallback,
)
from .parsing.parser import ParseOutput
from .response_build import build_summary, build_upstream, order_dedup_parts
from .schema import BillParseResult
from .submission.client import (
    _skipped_result,
    create_canonical_orders_async,
    create_orders_async,
)
from .validation import reject_missing_bl_no, reject_unknown_box_types


def _sha256(data: bytes) -> str:
    """文件内容 sha256（meta 溯源用）。"""
    return hashlib.sha256(data).hexdigest()


# ---- 异步编排（Phase 3 新增）：CPU 段 to_thread、网络段全 async，语义 ----

# ---- 文件级校验（箱型白名单/提单号缺失连坐）已下沉 validation.py（P1） ----


def _write_tempfile(suffix: str, file_bytes: bytes) -> str:
    """写临时文件（磁盘 IO，编排层 to_thread 执行），返回路径。"""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp.flush()
        return tmp.name


async def _parse_stage_async(filename: str, file_bytes: bytes) -> ParseOutput:
    """解析编排（两段式，收尾改造）：CPU 段 to_thread、LLM 网络段真异步。

    - 常见路径（L1/L2 指纹命中）：open_and_identify 单段完成，零额外开销；
    - L3 未命中：AiHeaderNeeded 信号 → achat_json 表头映射（真异步）→
      闸门/解析二次进段；LLM 不可用/响应非法回退精确匹配（与同步
      parse_bill 语义一致，找不到表头照旧 400）；
    - 资源生命周期由本函数统一管理：临时文件与 workbook 均在 finally
      释放（needed.close 幂等）。同步 parse_bill 仅供 tests 直调。
    """
    suffix = Path(filename).suffix.lower()
    tmp_path = ""
    import asyncio

    needed: AiHeaderNeeded | None = None
    try:
        tmp_path = await asyncio.to_thread(_write_tempfile, suffix, file_bytes)
        outcome = await asyncio.to_thread(open_and_identify, tmp_path)
        if not isinstance(outcome, AiHeaderNeeded):
            return outcome
        needed = outcome
        messages, schema = build_llm_request(needed.zone_lines)
        try:
            raw, meta = await achat_json(messages, json_schema=schema)
        except (LLMError, ParseError):
            # LLM 不可用/响应非法 → 回退精确匹配（找不到表头照旧 400）
            return await asyncio.to_thread(parse_exact_fallback, needed)
        ai = await asyncio.to_thread(
            validate_ai_result, needed.view, raw, meta, needed.zone_lines
        )
        return await asyncio.to_thread(parse_ai_header, needed, ai)
    finally:
        if needed is not None:
            needed.close()
        if tmp_path:
            os.unlink(tmp_path)


def _aggregate_stage(output, create_order: bool, sk: str):
    """归集与预判段（内存 CPU，to_thread 执行）：双管线分流 → unmapped_note →
    create 模式去重预判（registry 内存查重）→ 箱型白名单文件级校验。

    registry lookup 为进程内内存读（threading.Lock 微秒级），随归集段入线程池；
    返回 (orders, canonical_orders, agg)。
    """
    agg = group_orders(output.rows, output.period)
    orders = agg.orders
    canonical_orders: list = []
    if output.canonical_rows is not None:
        template = output.template_match.template if output.template_match else {}
        canonical_orders = group_canonical(
            output.canonical_rows, template, output.period
        )
    elif orders:
        canonical_orders = [to_canonical(o) for o in orders]

    if canonical_orders:
        from .submission.payload import collect_unmapped_note

        for order in canonical_orders:
            order.unmapped_note = collect_unmapped_note(order)

    # 重复上传去重预判（成功单注册表，方案一，起按 (提单号+箱号, sk) 维度）：
    # create 模式先查同一 sk 已成功组合键，命中即标记 skipped（只查不登；登记在
    # 提交成功后由 client 完成）；不同 sk 各自可导（生产误拦修正）。
    # 一行一票（业务拍板）：提单号必填；缺失行无去重键，不查重保持
    # 未决，交下方文件级校验统一连坐拒绝（用户拍板：与箱型同语义，
    # 一单不录）。计数/自举/费用报告只对未决单进行；preview 不预判（零注册表读写）。
    if create_order:
        from .submission.imported_registry import get_imported_registry, normalize, owner_key

        _imported = get_imported_registry()
        _owner = owner_key(sk)
        for order in (*canonical_orders, *orders):
            bl = normalize(
                getattr(order, "bl_no", None) or getattr(order, "order_num1", None)
            )
            if not bl:
                continue  # 无键不查重；文件级 missing_bl_no 校验统一拒绝
            box, seq = order_dedup_parts(order)
            if rec := _imported.lookup(bl, _owner, container_no=box, fallback=seq):
                order.create_result = _skipped_result(rec.get("sn"))

    all_pending = [o for o in (*canonical_orders, *orders) if o.create_result is None]
    reject_unknown_box_types(all_pending)
    return orders, canonical_orders, agg


async def build_result_async(
    *,
    filename: str,
    file_bytes: bytes,
    create_order: bool = False,
    sk: str = "",
) -> BillParseResult:
    """build_result（异步化改造后为生产唯一入口）：解析/归集（CPU 密集）入线程池，网络段全 async
    （并发下单/自举/建档），响应组装语义。

    create_order=True 时下单走 create_orders_async / create_canonical_orders_async
    （异键有界并发，同键串行原子）；费用对账/自举/建档管线顺序与既有语义一致。
    """
    import asyncio

    from app.core.config import settings

    # 防御性校验（置于解析前，零 IO 快速失败）：服务层公开入口语义
    if create_order and not (sk or "").strip():
        raise BadRequestError(
            "missing sk header for create mode: login to TMS first",
            description="缺少 TMS token，请先登录 TMS 获取 token，并以 sk 请求头携带",
            details={
                "upstream": {
                    "code": "400",
                    "msg": "缺少 TMS token（sk 请求头），请先登录 TMS",
                    "data": [],
                },
            },
        )

    # 解析 + 归集 + 预判 + 箱型校验（CPU 密集段入线程池，不阻塞事件循环；
    # L3 表头映射 LLM 调用经 _parse_stage_async 真异步）
    output = await _parse_stage_async(filename, file_bytes)

    # 单次导入行数上限（一柜一行）：preview/create 一致拦截，超限零副作用直接拒绝
    total_rows = len(output.rows) + len(output.canonical_rows or [])
    if total_rows > settings.bill_import_max_rows:
        raise BadRequestError(
            f"bill has too many rows: {total_rows} > {settings.bill_import_max_rows}",
            code="too_many_rows",
            details={
                "total_rows": total_rows,
                "max_rows": settings.bill_import_max_rows,
                "upstream": {
                    "code": "400",
                    "msg": "数据量过大，联系人工客服",
                    "data": [],
                },
            },
        )

    orders, canonical_orders, agg = await asyncio.to_thread(
        _aggregate_stage, output, create_order, sk
    )

    bill_period = (
        f"{output.period.start}~{output.period.end}"
        if output.period is not None and output.period.start and output.period.end
        else None
    )

    # 文件级提单号缺失校验（用户拍板，语义对齐箱型连坐）：任一未决单
    # 提单号缺失 → 全部未决单拒绝（一单不录，不调下游）。置于箱型校验之后：
    # 两者同时存在时箱型先标记（路由 msg 优先级 unknown_box_type > missing_bl_no）。
    # 双表示分组执行：既有语义路径 orders（BillRow 源）与 canonical_orders 同源
    # 双份，各自组内自洽（缺失行数/行号不跨组重复计数）；标准字段路径 orders 空。
    # preview 与 create 统一执行（纯内存标记，preview 响应消费 msg 提示）。
    reject_missing_bl_no([o for o in orders if o.create_result is None])
    reject_missing_bl_no(
        [o for o in canonical_orders if o.create_result is None]
    )

    # 未决单（去重 + 箱型校验后真正待处理）：preview 时未预判即全量；
    # 修正——必须在校验后重算，校验被拒单 create_result 已标记
    # （非 None），自然排除，费目自举/建档只对可录单执行（被拒文件零下游副作用）；
    # 校验前快照会让被拒单仍进入建档/自举（实测 AddCarClient 被误调）
    pending = [o for o in canonical_orders if o.create_result is None]

    fee_reconciliation = None
    if pending and output.canonical_rows is not None:
        # 费目自举（create 模式建档网络；preview 零副作用只出 planned）→
        # 报告注入 build_fee_reports（同步 CPU 段）
        bootstrap_report = await run_fee_bootstrap_async(
            pending, create_order=create_order, sk=sk
        )
        fee_reconciliation = build_fee_reports(
            output, pending, create_order=create_order, sk=sk,
            bootstrap_report=bootstrap_report,
        )

    # BillRow 链（jinxin 直传名）模板外费用建档（用户拍板；建档段走
    # create_archives_async）：订单费用以中文名直传可录，
    # 但 TMS「费用管理」只有 AddCarPrice 建档过的费目——模板外新费目订单有、
    # 费用管理无档案 → create 自动建档同名档案（复用费目自举配置/端点/registry）；
    # preview 只出 planned 计划清单零副作用；建档失败不阻塞下单（直传不依赖
    # price_id，仅报告下批重试）；已建档名跳过（幂等）。
    billrow_fee_bootstrap_report = None
    if orders and not output.canonical_rows:
        pending_legacy = [o for o in orders if o.create_result is None]
        if pending_legacy:
            from .fees.fee_bootstrap import run_billrow_fee_bootstrap_async

            billrow_fee_bootstrap_report = await run_billrow_fee_bootstrap_async(
                pending_legacy, create_order=create_order, sk=sk
            )
    # canonical 链模板外费目建档（拍板废除 B2 孤儿建档）：模板外列
    # 经 fee_map 按列名判定为独立动态码费目，建档已并入 run_fee_bootstrap（上述
    # _build_fee_reports 内、apply 回填之前执行）→ 建档成功当批独立发射；
    # 建档失败降级归并其它费保底（apply_price_map 内处理）。无独立 B2 挂点。
    # （原 B2 在 apply 之后建档，档案永远赶不上当批——费用永远挂其它费）

    # 阶段三：基础资料阈值编排（create 建档网络；preview 只读探测）
    master_data_report = None
    if pending:
        from .master_data.orchestrator import run_master_data_async

        master_data_report = await run_master_data_async(
            pending, create_order=create_order, sk=sk
        )

    summary = None
    upstream = None
    file_sha256 = _sha256(file_bytes)
    if create_order:
        if orders:
            # 既有语义：双通道下单（异键有界并发，同键串行原子）
            await create_orders_async(orders, sk, source_sha256=file_sha256)
        elif canonical_orders:
            await create_canonical_orders_async(
                canonical_orders, sk, source_sha256=file_sha256
            )
        pipeline = orders if orders else canonical_orders
        created = [o for o in pipeline if getattr(o, "create_result", None)]
        summary = build_summary(len(canonical_orders) or len(orders), created)
        upstream = build_upstream(created)

    meta: dict = {
        "source_sha256": file_sha256,
        "source_bytes": len(file_bytes),
        "parsed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parser": output.engine,
        "raw_rows": len(output.rows) + len(output.canonical_rows or []),
        "template": output.template,
        "unmatched_headers": output.unmatched_headers,
    }
    if orders and not output.canonical_rows:
        meta["reconciliation"] = agg.reconciliation
    if fee_reconciliation is not None:
        meta["reconciliation"] = fee_reconciliation
    if master_data_report is not None:
        meta["master_data"] = master_data_report
    # BillRow 链模板外费用建档报告（与 master_data 同风格：顶层独立键；
    # canonical 链的费目自举报告在 meta.reconciliation.reports.fee_bootstrap）
    if billrow_fee_bootstrap_report is not None:
        meta["fee_bootstrap"] = billrow_fee_bootstrap_report
    if output.new_template is not None:
        meta["l3_template"] = output.new_template

    # R2（用户拍板）：响应只回实际下单那份——BillRow 源前端读
    # orders（canonical_orders 置空；to_canonical 副本仅内部建档管线用，避免
    # 双份不同步与 payload 翻倍）；标准字段源 orders 恒空、canonical_orders 全量
    response_canonical = canonical_orders if not orders else []
    return BillParseResult(
        file=filename,
        bill_period=bill_period,
        total_rows=sum(o.row_count for o in (canonical_orders or orders)),
        order_count=len(canonical_orders) or len(orders),
        create_order=create_order,
        orders=orders,
        canonical_orders=response_canonical,
        summary=summary,
        upstream=upstream,
        meta=meta,
    )
