"""竞品账单导入编排层：解析 → 归集 → BillParseResult 响应组装（含 meta 溯源）。

双管线（模板识别后自动分流，见 parser.ParseOutput）：
- 既有流程语义（columns 目标 = BillRow 字段）：group_orders → BillOrder → orders；
  create_order=True 时走既有双通道下单（client.create_orders，行为语义不变）；
  同时经 to_canonical 转换为 CanonicalOrder 供 TMS 通道（无标准字段模板的家族由此覆盖）。
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
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.core.errors import BadRequestError, LLMError, ParseError
from app.llm import achat_json

from .aggregator import group_orders
from .ai_header import build_llm_request, validate_ai_result
from .canonical_aggregator import group_canonical
from .client import create_canonical_orders_async, create_orders_async
from .fee_bootstrap import run_fee_bootstrap_async
from .fee_price_map import apply_price_map
from .parser import (
    AiHeaderNeeded,
    ParseOutput,
    open_and_identify,
    parse_ai_header,
    parse_exact_fallback,
)
from .schema import BillParseResult, to_canonical


def _sha256(data: bytes) -> str:
    """文件内容 sha256（meta 溯源用）。"""
    return hashlib.sha256(data).hexdigest()


def _order_dedup_parts(order) -> tuple[str | None, str | None]:
    """订单去重键段 (箱号, 行序号)：canonical 取结构化箱号 + row_seq，
    旧链路 BillOrder 取 container_no + row_seq；箱号缺失时键退化为行序号兜底
    （提单号|#seq），双缺再退化纯提单号（见 imported_registry.dedup_key）。"""
    container_no = None
    for container in getattr(order, "containers", None) or []:
        if getattr(container, "container_no", None):
            container_no = container.container_no
            break
    if container_no is None:
        container_no = getattr(order, "container_no", None)
    return container_no, getattr(order, "row_seq", None)


def _build_fee_reports(
    output, orders: list, create_order: bool, sk: str = "", *, bootstrap_report: dict | None
) -> dict:
    """费用对账报告（T14，只报告不拦截）：price_id 回填 + 恒等校验 + 报告清单。

    - 先对全部订单 apply_price_map（回填 tms_name/price_id；price_id null 降级
      excluded 并同步调整对账口径 recorded → excluded）；
    - 恒等校验：bill_total − recorded_total = excluded_total，容差 0.01；
      超差按单列条目（单号、通道、三口径值）；
    - 报告内容：price_id null 降级清单、unmapped skip 逐行（计数）、to_other 原名
      计数（≥阈值 warning）、金额解析失败清单。
    返回结构与既有 reconciliation 同键（meta["reconciliation"]），canonical 路径使用。
    bootstrap_report 由调用方先执行费目自举（run_fee_bootstrap_async）后传入
    （含 None：自举未执行/无缺失时无报告段）。
    """
    from app.core.config import settings

    dropped: list[dict] = []
    mismatch: list[dict] = []
    to_other_counts: Counter[str] = Counter()
    channel_stats: dict[str, dict] = {}
    # 费目自举（T25）：费用归一后、payload 构造前——本批缺失费目码自动建档 →
    # registry 登记 → 下方 apply_price_map 经 registry 命中回填（当批正常录入）；
    # **preview 零副作用**：create_order=false 只输出 planned 清单不发请求
    for order in orders:
        _, order_dropped = apply_price_map(order.fees)
        for entry in order_dropped:
            entry["bl_no"] = order.bl_no
            dropped.append(entry)
            # 降级项调整对账口径：recorded → excluded
            rec = order.fee_reconcile.get(entry["channel"])
            if rec is not None:
                rec.recorded_total -= Decimal(entry["money"])
                rec.excluded_total += Decimal(entry["money"])
        for channel, rec in order.fee_reconcile.items():
            rec.diff = rec.bill_total - rec.recorded_total - rec.excluded_total
            # 与 aggregator 同口径：无锚点（bill_total=0）不算 mismatch（no_anchor 语义）
            rec.ok = rec.bill_total == 0 or abs(rec.diff) <= Decimal("0.01")
            stats = channel_stats.setdefault(
                channel,
                {
                    "orders": 0,
                    "ok": 0,
                    "mismatch": 0,
                    "bill_total": Decimal("0"),
                    "recorded_total": Decimal("0"),
                    "excluded_total": Decimal("0"),
                    "diff": Decimal("0"),
                },
            )
            stats["orders"] += 1
            stats["bill_total"] += rec.bill_total
            stats["recorded_total"] += rec.recorded_total
            stats["excluded_total"] += rec.excluded_total
            stats["diff"] += rec.diff
            if rec.ok:
                stats["ok"] += 1
            else:
                stats["mismatch"] += 1
                mismatch.append(
                    {
                        "bl_no": order.bl_no,
                        "channel": channel,
                        "bill_total": str(rec.bill_total),
                        "recorded_total": str(rec.recorded_total),
                        "excluded_total": str(rec.excluded_total),
                        "diff": str(rec.diff),
                    }
                )
        for fee in order.fees:
            if fee.code == "other" and fee.note:
                for name in fee.note.split(","):
                    if name:
                        to_other_counts[name] += 1

    threshold = settings.fee_to_other_warning_threshold
    to_other_warning = [
        {"name": name, "count": count}
        for name, count in to_other_counts.items()
        if count >= threshold
    ]
    failures: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for row in output.canonical_rows or []:
        for item in row.get("_fee_failures") or []:
            failures[f"{item.get('section', '')}.{item.get('name', '')}"] += 1
        for item in row.get("_fee_skipped") or []:
            skipped[f"{item.get('section', '')}.{item.get('name', '')}"] += 1

    channels: dict = {}
    any_mismatch = False
    any_checked = False
    for channel, stats in channel_stats.items():
        diff = stats["diff"]
        if stats["orders"] > 0 and (stats["bill_total"] or stats["recorded_total"] or stats["excluded_total"]):
            any_checked = True
        if stats["mismatch"] > 0:
            any_mismatch = True
        channels[channel] = {
            "orders": stats["orders"],
            "ok": stats["ok"],
            "mismatch": stats["mismatch"],
            "bill_total": str(stats["bill_total"]),
            "recorded_total": str(stats["recorded_total"]),
            "excluded_total": str(stats["excluded_total"]),
            "diff": str(diff),
        }
    if any_mismatch:
        status = "mismatch"
    elif any_checked:
        status = "matched"
    else:
        status = "no_anchor"
    report: dict = {
        "status": status,
        "channels": channels,
        "mismatch_orders": mismatch,
        "reports": {
            "price_null_dropped": dropped,
            "unmapped_skipped": [{"fee": k, "count": v} for k, v in skipped.items()],
            "parse_failures": [{"fee": k, "count": v} for k, v in failures.items()],
            "to_other_counts": dict(to_other_counts.most_common()),
            "to_other_warning": to_other_warning,
        },
    }
    # 费目自举报告（T25）：preview=planned 计划清单（零副作用）/ create=created 建档
    # 成功清单（含新 price_id）+ failed 自举失败清单
    if bootstrap_report is not None:
        report["reports"]["fee_bootstrap"] = bootstrap_report
    return report


def _reject_unknown_box_types(orders: list) -> bool:
    """文件级箱型白名单校验（2026-08-18 用户拍板）：任一单含标准代码形态且不在
    白名单的箱型 → 全部未决单拒绝（unknown_box_type，不调下游），返回 True。

    preview 与 create 统一执行（preview 也拒）；已标记 skipped 的单不动
    （历史成功单必然合法）。单级 message 报该单自己的非法箱型，无非法箱型的
    单报文件级清单；details.unknown_box_types 同 message 口径。
    """
    from app.core.box_whitelist import check_unknown_box_types

    def _types(order) -> list[str]:
        if isinstance(getattr(order, "box_groups", None), list):
            return [g.b_type for g in order.box_groups if getattr(g, "b_type", None)]
        return [
            box.get("b_type")
            for box in (order.order_data or {}).get("box", []) or []
            if isinstance(box, dict) and box.get("b_type")
        ]

    file_unknown: list[str] = []
    per_order: dict[int, list[str]] = {}
    for idx, order in enumerate(orders):
        if order.create_result is not None:
            continue  # 已标记（skipped/预判）的单不动
        unknown = check_unknown_box_types(_types(order))
        if unknown:
            per_order[idx] = unknown
            for t in unknown:
                if t not in file_unknown:
                    file_unknown.append(t)
    if not file_unknown:
        return False
    for idx, order in enumerate(orders):
        if order.create_result is not None:
            continue
        order_unknown = per_order.get(idx, [])
        order.create_result = {
            "success": False,
            "sn": None,
            "error": {
                "code": "unknown_box_type",
                "message": (
                    f"系统没有此箱型：{'、'.join(order_unknown)}，请联系客服"
                    if order_unknown
                    else f"文件含非法箱型：{'、'.join(file_unknown)}，请联系客服"
                ),
                "description": "箱型不在 TMS 支持清单中，请联系客服",
                "details": {
                    "unknown_box_types": order_unknown or file_unknown,
                    # 全场景业务码统一可达（§3.7/既有规范）：本地拦截等价于该单
                    # 添加失败，对齐 TMS「新建全部失败 → 204」口径
                    "upstream": {"code": "204", "msg": "添加失败", "data": []},
                },
            },
        }
    return True

# ---- 异步编排（Phase 3 新增）：CPU 段 to_thread、网络段全 async，语义 ----


def _write_tempfile(suffix: str, file_bytes: bytes) -> str:
    """写临时文件（磁盘 IO，编排层 to_thread 执行），返回路径。"""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp.flush()
        return tmp.name


async def _parse_stage_async(filename: str, file_bytes: bytes) -> ParseOutput:
    """解析编排（两段式，2026-09 收尾改造）：CPU 段 to_thread、LLM 网络段真异步。

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
        from .payload import collect_unmapped_note

        for order in canonical_orders:
            order.unmapped_note = collect_unmapped_note(order)

    if create_order:
        from .imported_registry import get_imported_registry, normalize, owner_key

        _imported = get_imported_registry()
        _owner = owner_key(sk)
        for order in (*canonical_orders, *orders):
            bl = normalize(
                getattr(order, "bl_no", None) or getattr(order, "order_num1", None)
            )
            if not bl:
                order.create_result = {
                    "success": False,
                    "skipped": False,
                    "sn": None,
                    "error": {
                        "code": "missing_bl_no",
                        "message": "提单号缺失，未录入",
                        "description": "提单号为必填项，该行未录入；请补全提单号后重新导入",
                        "details": {},
                    },
                }
                continue
            box, seq = _order_dedup_parts(order)
            if rec := _imported.lookup(bl, _owner, container_no=box, fallback=seq):
                order.create_result = {
                    "success": True,
                    "skipped": True,
                    "sn": rec.get("sn"),
                    "error": None,
                }

    all_pending = [o for o in (*canonical_orders, *orders) if o.create_result is None]
    _reject_unknown_box_types(all_pending)
    return orders, canonical_orders, agg


async def build_result_async(
    *,
    filename: str,
    file_bytes: bytes,
    create_order: bool = False,
    sk: str = "",
) -> BillParseResult:
    """build_result（2026-09 异步化改造后为生产唯一入口）：解析/归集（CPU 密集）入线程池，网络段全 async
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

    # 未决单（去重 + 箱型校验后真正待处理）：费目自举/建档只对可录单执行
    pending = [o for o in canonical_orders if o.create_result is None]

    fee_reconciliation = None
    if pending and output.canonical_rows is not None:
        # 费目自举（create 模式建档网络；preview 零副作用只出 planned）→
        # 报告注入 _build_fee_reports（同步 CPU 段）
        bootstrap_report = await run_fee_bootstrap_async(
            pending, create_order=create_order, sk=sk
        )
        fee_reconciliation = _build_fee_reports(
            output, pending, create_order=create_order, sk=sk,
            bootstrap_report=bootstrap_report,
        )

    # 阶段三：基础资料阈值编排（create 建档网络；preview 只读探测）
    master_data_report = None
    if pending:
        from .master_data import run_master_data_async

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
        created = [
            o for o in pipeline if getattr(o, "create_result", None)
        ]
        summary = {
            "total": len(canonical_orders) or len(orders),
            "success": sum(1 for o in created if o.create_result.get("success")),
            "failed": sum(1 for o in created if not o.create_result.get("success")),
            "skipped": sum(1 for o in created if o.create_result.get("skipped")),
            "created": sum(
                1
                for o in created
                if o.create_result.get("success") and not o.create_result.get("skipped")
            ),
            "success_sns": [
                o.create_result.get("sn") for o in created if o.create_result.get("success")
            ],
            "failed_details": [
                {
                    **{
                        "order_num": getattr(o, "bl_no", None) or getattr(o, "order_num1", None),
                        "error_code": (o.create_result.get("error") or {}).get("code"),
                        "error_message": (o.create_result.get("error") or {}).get("message"),
                    },
                    **(
                        {"error_upstream": (o.create_result["error"].get("details") or {}).get("upstream")}
                        if (o.create_result.get("error") or {}).get("details", {}).get("upstream") is not None
                        else {}
                    ),
                }
                for o in created
                if not o.create_result.get("success")
            ],
        }
        upstream_data = [
            o.create_result.get("upstream")
            for o in created
            if o.create_result.get("success") and o.create_result.get("upstream")
        ]
        created_ok = any(
            o.create_result.get("success") and not o.create_result.get("skipped")
            for o in created
        )
        if created and not all(o.create_result.get("skipped") for o in created):
            upstream = (
                {"code": "200", "msg": "添加成功", "data": upstream_data}
                if created_ok
                else {"code": "204", "msg": "添加失败", "data": []}
            )

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
    if output.new_template is not None:
        meta["l3_template"] = output.new_template

    return BillParseResult(
        file=filename,
        bill_period=bill_period,
        total_rows=sum(o.row_count for o in (canonical_orders or orders)),
        order_count=len(canonical_orders) or len(orders),
        create_order=create_order,
        orders=orders,
        canonical_orders=canonical_orders,
        summary=summary,
        upstream=upstream,
        meta=meta,
    )
