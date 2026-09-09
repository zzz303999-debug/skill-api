"""费用对账（T14，2026-09-09 P2 自 service.py 下沉）：price_id 回填 + 降级口径调整。

只报告不拦截：逐单 apply_price_map（回填 tms_name/price_id，均限当前 sk 的
owner 槽）、merged_to_other（动态码归并其它费保底）仅报告不调整对账、恒等复算
（bill_total − recorded_total = excluded_total，容差 0.01）与通道统计；报告
结构同既有 reconciliation 键（meta["reconciliation"]），canonical 路径使用。
费目自举（run_fee_bootstrap_async）由编排方先行执行，bootstrap_report 经
build_fee_reports 参数注入报告段。
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from .fee_price_map import apply_price_map


def reconcile_order_fees(
    orders: list, owner: str
) -> tuple[list[dict], list[dict], Counter[str], dict[str, dict]]:
    """逐单费用对账（T14）：price_id 回填 + 降级口径调整 + 恒等复算 + 通道统计。

    返回 (price_id null 降级清单, 超差单条目, to_other 原名计数, 通道统计累计)。
    费目自举（T25）已由调用方先执行（run_fee_bootstrap_async）：本批缺失费目码
    自动建档 → registry 登记 → 下方 apply_price_map 经 registry 命中回填
    （当批正常录入；preview 零副作用只出 planned 清单不发请求）。
    merged_to_other（动态码归并其它费保底）仅报告不调整对账——金额已并入桶
    条目正常发射，仍在 recorded 口径内（2026-09-08 拍板）。
    """
    dropped: list[dict] = []
    mismatch: list[dict] = []
    to_other_counts: Counter[str] = Counter()
    channel_stats: dict[str, dict] = {}
    for order in orders:
        _, order_dropped = apply_price_map(order.fees, owner)
        for entry in order_dropped:
            entry["bl_no"] = order.bl_no
            dropped.append(entry)
            # merged_to_other（动态码降级归并其它费）：金额已并入桶条目正常发射，
            # 仍在 recorded 口径内 → 不调整对账；其余降级项（未发射）recorded → excluded
            if entry.get("reason") == "merged_to_other":
                continue
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
    return dropped, mismatch, to_other_counts, channel_stats


def fee_parse_failures(output) -> tuple[Counter[str], Counter[str]]:
    """行级费用解析失败/无码跳过计数（parser 行私有键 _fee_failures/_fee_skipped）。"""
    failures: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for row in output.canonical_rows or []:
        for item in row.get("_fee_failures") or []:
            failures[f"{item.get('section', '')}.{item.get('name', '')}"] += 1
        for item in row.get("_fee_skipped") or []:
            skipped[f"{item.get('section', '')}.{item.get('name', '')}"] += 1
    return failures, skipped


def build_fee_reports(
    output, orders: list, create_order: bool, sk: str = "", *, bootstrap_report: dict | None
) -> dict:
    """费用对账报告（T14，只报告不拦截）：price_id 回填 + 恒等校验 + 报告清单。

    - 先对全部订单 apply_price_map（回填 tms_name/price_id，均限当前 sk 的
      owner 槽——订单费用只挂自己账号名下的档案，2026-09-08 费目隔离拍板；
      price_id null 降级 excluded 并同步调整对账口径 recorded → excluded）；
      merged_to_other（动态码归并其它费保底）仅报告不调整对账——金额已并入
      桶条目正常发射，仍在 recorded 口径内；
    - 恒等校验：bill_total − recorded_total = excluded_total，容差 0.01；
      超差按单列条目（单号、通道、三口径值）；
    - 报告内容：price_id null 降级清单、unmapped skip 逐行（计数）、to_other 原名
      计数（≥阈值 warning）、金额解析失败清单。
    返回结构与既有 reconciliation 同键（meta["reconciliation"]），canonical 路径使用。
    bootstrap_report 由调用方先执行费目自举（run_fee_bootstrap_async）后传入
    （含 None：自举未执行/无缺失时无报告段）。
    """
    from app.core.config import settings

    from .fee_bootstrap import _owner_for as _fee_owner_for

    owner = _fee_owner_for(sk)
    dropped, mismatch, to_other_counts, channel_stats = reconcile_order_fees(
        orders, owner
    )
    threshold = settings.fee_to_other_warning_threshold
    to_other_warning = [
        {"name": name, "count": count}
        for name, count in to_other_counts.items()
        if count >= threshold
    ]
    failures, skipped = fee_parse_failures(output)

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
