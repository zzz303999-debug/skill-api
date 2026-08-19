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

from app.errors import BadRequestError

from .aggregator import group_canonical, group_orders
from .client import create_orders
from .fee_price_map import apply_price_map
from .parser import parse_bill
from .schema import BillParseResult, to_canonical


def _sha256(data: bytes) -> str:
    """文件内容 sha256（meta 溯源用）。"""
    return hashlib.sha256(data).hexdigest()


def _build_fee_reports(output, orders: list, create_order: bool) -> dict:
    """费用对账报告（T14，只报告不拦截）：price_id 回填 + 恒等校验 + 报告清单。

    - 先对全部订单 apply_price_map（回填 tms_name/price_id；price_id null 降级
      excluded 并同步调整对账口径 recorded → excluded）；
    - 恒等校验：bill_total − recorded_total = excluded_total，容差 0.01；
      超差按单列条目（单号、通道、三口径值）；
    - 报告内容：price_id null 降级清单、unmapped skip 逐行（计数）、to_other 原名
      计数（≥阈值 warning）、金额解析失败清单。
    返回结构与既有 reconciliation 同键（meta["reconciliation"]），canonical 路径使用。
    """
    from app.config import settings

    dropped: list[dict] = []
    mismatch: list[dict] = []
    to_other_counts: Counter[str] = Counter()
    channel_stats: dict[str, dict] = {}
    # 费目自举（T25）：费用归一后、payload 构造前——本批缺失费目码自动建档 →
    # registry 登记 → 下方 apply_price_map 经 registry 命中回填（当批正常录入）；
    # **preview 零副作用**（与阶段三一致）：create_order=false 只输出 planned
    # 计划清单不发请求；真实导入才建档。disabled/无缺失 → None（不产生报告段）
    bootstrap_report = None
    if orders:
        from .fee_bootstrap import run_fee_bootstrap

        bootstrap_report = run_fee_bootstrap(orders, create_order=create_order)
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
    from .box_whitelist import check_unknown_box_types

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


def build_result(
    *,
    filename: str,
    file_bytes: bytes,
    create_order: bool = False,
) -> BillParseResult:
    """编排：写临时文件 → 解析 → 归集（双管线分流）→ 组装 BillParseResult。

    create_order=True 时先逐单创建，再填 summary {total, success, failed,
    skipped, created}；凭证失败抛 UpstreamError（502，不逐单执行）。meta 含
    source_sha256 / source_bytes / parsed_at / parser / raw_rows / template /
    unmatched_headers。
    箱型白名单（2026-08-18 用户拍板）：**文件级校验**——preview 与 create 统一
    执行，任一单含标准代码形态且不在白名单的箱型（如 40GOH）→ 全部未决单拒绝
    （unknown_box_type「系统没有此箱型：<箱型>，请联系客服」），不调下游；
    无强制提交通道；非标表述（大冷/拼箱/17M飞翼车等）不校验（既有规则不变）。
    """
    suffix = Path(filename).suffix.lower()
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(file_bytes)
            tmp.flush()
            output = parse_bill(tmp_path)
    finally:
        if tmp_path:
            os.unlink(tmp_path)

    # 单次导入行数上限（一柜一行）：preview/create 一致拦截，超限零副作用直接拒绝。
    # 双管线同口径统计（与 meta.raw_rows 一致）：标准字段（canonical_rows）与
    # 既有语义（rows）任一超限即拒绝（标准字段模板下 rows 恒空，只看 rows 会漏拦）
    from app.config import settings

    total_rows = len(output.rows) + len(output.canonical_rows or [])
    if total_rows > settings.bill_import_max_rows:
        raise BadRequestError(
            f"bill has too many rows: {total_rows} > {settings.bill_import_max_rows}",
            code="too_many_rows",
            details={
                "total_rows": total_rows,
                "max_rows": settings.bill_import_max_rows,
                # 对齐 create 模式 upstream 结构（code/msg/data），
                # 保证对接方统一按 upstream.code 判断时错误码可达
                "upstream": {
                    "code": "400",
                    "msg": "数据量过大，联系人工客服",
                    "data": [],
                },
            },
        )

    # 双管线分流：标准字段（canonical_rows）→ CanonicalOrder；既有语义 → BillOrder
    agg = group_orders(output.rows, output.period)
    orders = agg.orders
    canonical_orders: list = []
    if output.canonical_rows is not None:
        template = output.template_match.template if output.template_match else {}
        canonical_orders = group_canonical(
            output.canonical_rows, template, output.period
        )
    elif orders:
        # 既有语义路径：归集结果转换为标准订单（TMS 通道入口）
        canonical_orders = [to_canonical(o) for o in orders]

    bill_period = (
        f"{output.period.start}~{output.period.end}"
        if output.period is not None and output.period.start and output.period.end
        else None
    )

    # 未映射字段报告：customer_name 在 F2 确认 TMS「客户」字段键前不进表单，
    # 只进 unmapped_note（预览/对账报告可见，下单路径同样填充）
    if canonical_orders:
        from .payload import collect_unmapped_note

        for order in canonical_orders:
            order.unmapped_note = collect_unmapped_note(order)

    # 重复上传去重预判（成功单注册表，方案一）：create 模式先查已成功提单号，
    # 命中即标记 skipped（只查不登；登记在提交成功后由 client 完成）。计数/自举/
    # 费用报告只对未决单进行；preview 不预判（零注册表读写、零副作用）。
    if create_order:
        from .imported_registry import get_imported_registry, normalize

        _imported = get_imported_registry()
        for order in (*canonical_orders, *orders):
            bl = normalize(
                getattr(order, "bl_no", None) or getattr(order, "order_num1", None)
            )
            if bl and (rec := _imported.lookup(bl)):
                order.create_result = {
                    "success": True,
                    "skipped": True,
                    "sn": rec.get("sn"),
                    "error": None,
                }

    # 未决单（去重后待处理）：preview 时未预判，即全量
    pending = [o for o in canonical_orders if o.create_result is None]

    # 文件级箱型白名单校验（2026-08-18 用户拍板）：preview 与 create 统一执行，
    # 任一单含非法箱型（标准代码形态不在白名单，如 40GOH）→ 全部未决单拒绝，
    # 不调下游（置于费目自举/建档之前，被拒文件零副作用）；无强制提交通道。
    # 既有规则不变：非标表述（大冷/拼箱/17M飞翼车等）不校验照常提交。
    all_pending = [o for o in (*canonical_orders, *orders) if o.create_result is None]
    _reject_unknown_box_types(all_pending)

    fee_reconciliation = None
    if pending and output.canonical_rows is not None:
        # 费用 price_id 回填 + 费用对账报告（T12/T14，canonical 路径）
        fee_reconciliation = _build_fee_reports(
            output, pending, create_order=create_order
        )

    # 阶段三：基础资料阈值编排（聚合后、payload 构造前；T19）——计数 → 建档 →
    # 当批回填 order._archive_refs（payload 构造在 create 分支内，先于下单执行）；
    # preview 只读探测不计数；disabled → None（不产生报告段）
    master_data_report = None
    if pending:
        from .master_data import run_master_data

        master_data_report = run_master_data(pending, create_order=create_order)

    summary = None
    upstream = None
    file_sha256 = _sha256(file_bytes)
    if create_order:
        if orders:
            # 既有语义：双通道下单（行为语义不变）
            create_orders(orders, source_sha256=file_sha256)
        elif canonical_orders:
            from .client import create_canonical_orders

            create_canonical_orders(canonical_orders, source_sha256=file_sha256)
        # 与创建分支同管线口径（orders 优先，elif canonical_orders）：双管线并存
        # 时（同一批数据的两种表示）只统计实际创建管线，避免 summary 计数翻倍
        # （去重预判标记了两边，created 统计不再合并计数）
        pipeline = orders if orders else canonical_orders
        created = [
            o for o in pipeline if getattr(o, "create_result", None)
        ]
        summary = {
            "total": len(canonical_orders) or len(orders),
            "success": sum(1 for o in created if o.create_result.get("success")),
            "failed": sum(1 for o in created if not o.create_result.get("success")),
            # 重复上传去重（方案一）：本次跳过数（成功单注册表命中，不调下游）
            "skipped": sum(1 for o in created if o.create_result.get("skipped")),
            # 本次实际新建数（success 含 skipped 单，created = success − skipped）
            "created": sum(
                1
                for o in created
                if o.create_result.get("success") and not o.create_result.get("skipped")
            ),
            # 下单成功回显（对齐 /orders 的 upstream 语义）：成功单 TMS 业务编号列表
            "success_sns": [
                o.create_result.get("sn") for o in created if o.create_result.get("success")
            ],
            # 失败单明细（单号 + 错误码/消息/上游业务码），人工可查；本地拦截
            # （unknown_box_type）额外带 error_upstream（对齐 TMS 204 失败口径）
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
        # 上游回显（对齐 TMS 通道格式，见《逆推规范》§3）：确有新建成功单 →
        # code "200" + msg 添加成功 + data 成功单原始回显（含 sn/sns；成功但无
        # 回显时 data 为空仍为 200）；新建全失败 → code "204" + msg 添加失败 +
        # 空 data；全部 skipped（无新建动作）保持 null（与「无单可创建」同语义，
        # 路由层转 409；失败原因见 summary.failed_details）
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
    # 既有语义路径保留对账信息（账单锚点校验，只报告不拦截）
    if orders and not output.canonical_rows:
        meta["reconciliation"] = agg.reconciliation
    # 标准字段路径：费用对账报告（与既有 reconciliation 同键）
    if fee_reconciliation is not None:
        meta["reconciliation"] = fee_reconciliation
    # 阶段三：基础资料阈值报告（T21；只报告不拦截，建档异常不使订单丢失）
    if master_data_report is not None:
        meta["master_data"] = master_data_report
    # L3 候选模板配置（人工确认固化的载体）
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
