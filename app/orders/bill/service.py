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

from .aggregator import group_canonical, group_orders
from .client import create_orders
from .fee_price_map import apply_price_map
from .parser import parse_bill
from .schema import BillParseResult, to_canonical


def _sha256(data: bytes) -> str:
    """文件内容 sha256（meta 溯源用）。"""
    return hashlib.sha256(data).hexdigest()


def _build_fee_reports(output, orders: list) -> dict:
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
            rec.ok = abs(rec.diff) <= Decimal("0.01")
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
    return report


def build_result(
    *,
    filename: str,
    file_bytes: bytes,
    create_order: bool = False,
) -> BillParseResult:
    """编排：写临时文件 → 解析 → 归集（双管线分流）→ 组装 BillParseResult。

    create_order=True 时先逐单创建，再填 summary {total, success, failed}；
    凭证失败抛 UpstreamError（502，不逐单执行）。meta 含 source_sha256 /
    source_bytes / parsed_at / parser / raw_rows / template / unmatched_headers。
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

    fee_reconciliation = None
    if canonical_orders and output.canonical_rows is not None:
        # 费用 price_id 回填 + 费用对账报告（T12/T14，canonical 路径）
        fee_reconciliation = _build_fee_reports(output, canonical_orders)

    summary = None
    upstream = None
    if create_order:
        if orders:
            # 既有语义：双通道下单（行为语义不变）
            create_orders(orders)
        elif canonical_orders:
            from .client import create_canonical_orders

            create_canonical_orders(canonical_orders)
        created = [
            o
            for o in (*canonical_orders, *orders)
            if getattr(o, "create_result", None)
        ]
        summary = {
            "total": len(canonical_orders) or len(orders),
            "success": sum(1 for o in created if o.create_result.get("success")),
            "failed": sum(1 for o in created if not o.create_result.get("success")),
            # 下单成功回显（对齐 /orders 的 upstream 语义）：成功单 TMS 业务编号列表
            "success_sns": [
                o.create_result.get("sn") for o in created if o.create_result.get("success")
            ],
            # 失败单明细（单号 + 错误码/消息），人工可查
            "failed_details": [
                {
                    "order_num": getattr(o, "bl_no", None) or getattr(o, "order_num1", None),
                    "error_code": (o.create_result.get("error") or {}).get("code"),
                    "error_message": (o.create_result.get("error") or {}).get("message"),
                }
                for o in created
                if not o.create_result.get("success")
            ],
        }
        # 上游原始回显（对齐 /orders 的 upstream：code 200 + msg 添加成功 + 每单回显）；
        # 全部失败时保持 null（失败原因见 summary.failed_details）
        upstream_data = [
            o.create_result.get("upstream")
            for o in created
            if o.create_result.get("success") and o.create_result.get("upstream")
        ]
        upstream = (
            {"code": 200, "msg": "添加成功", "data": upstream_data} if upstream_data else None
        )

    meta: dict = {
        "source_sha256": _sha256(file_bytes),
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
