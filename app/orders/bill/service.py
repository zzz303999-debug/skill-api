"""竞品账单导入编排层：解析 → 归集 → BillParseResult 响应组装（含 meta 溯源）。

双管线（模板识别后自动分流，见 parser.ParseOutput）：
- 既有流程语义（columns 目标 = BillRow 字段）：group_orders → BillOrder → orders；
  create_order=True 时走既有双通道下单（client.create_orders，行为语义不变）；
  同时经 to_canonical 转换为 CanonicalOrder 供 TMS 通道（无标准字段模板的家族由此覆盖）。
- 标准字段语义（columns 目标 = CanonicalOrder 字段）：group_canonical →
  CanonicalOrder → canonical_orders；create_order=True 时走 TMS form-data 通道
  （payload/client，见 T7）。

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

from .aggregator import group_canonical, group_orders
from .client import create_orders
from .parser import parse_bill
from .schema import BillParseResult, to_canonical


def _sha256(data: bytes) -> str:
    """文件内容 sha256（meta 溯源用）。"""
    return hashlib.sha256(data).hexdigest()


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

    summary = None
    if create_order:
        if orders:
            # 既有语义：双通道下单（行为语义不变）
            create_orders(orders)
        elif canonical_orders:
            from .client import create_canonical_orders

            create_canonical_orders(canonical_orders)
        summary = {
            "total": len(canonical_orders) or len(orders),
            "success": sum(
                1
                for o in (*canonical_orders, *orders)
                if getattr(o, "create_result", None)
                and o.create_result.get("success")
            ),
            "failed": sum(
                1
                for o in (*canonical_orders, *orders)
                if getattr(o, "create_result", None)
                and not o.create_result.get("success")
            ),
        }

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
        meta=meta,
    )
