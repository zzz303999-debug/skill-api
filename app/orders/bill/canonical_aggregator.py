"""标准字段归集（TMS 通道，模板配置驱动；见《字段映射表》§4）。

标准字段语义行（columns 目标 = CanonicalOrder 字段）一行一票 → CanonicalOrder；
与旧链路 aggregator.py（BillRow → BillOrder）并列，由 service 层按模板识别
结果分流。清洗口径（车牌浮点尾巴/行序号）复用旧链路公开函数，两通道一致。
"""

from __future__ import annotations

from decimal import Decimal

from .aggregator import clean_group_key, clean_plate_no
from .schema import (
    BillPeriod,
    BoxGroup,
    CanonicalOrder,
    ContainerInfo,
    FeeItem,
    FeeReconcile,
)

# 单行取值字段（CanonicalOrder 标量字段，剔除聚合/元信息；
# bl_no 保留在单值集内——构造时单独清洗浮点尾巴后作为提单号）
_SINGLE_FIELDS: tuple[str, ...] = tuple(
    name
    for name in CanonicalOrder.model_fields
    if name
    not in {
        "box_groups",
        "containers",
        "month",
        "missing_fields",
        "source_template",
        "row_count",
        "fees",
        "fee_reconcile",
        # 行序号不入单值集：行 dict 键名为 seq，构造时单独清洗赋值（去重键兜底段）
        "row_seq",
    }
)


def _box_type_of(row: dict) -> str | None:
    """行内箱型：box_type_qty 归一化结果首项（非标箱型同此）。"""
    items = row.get("box_type_qty")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0].get("type")
    return None


def _box_groups_of(row: dict) -> list[BoxGroup]:
    """行内箱型聚合：box_type_qty 逐项按箱型累加数量（保出现序；一行可含多箱型）。"""
    counts: dict[str, int] = {}
    order: list[str] = []
    items = row.get("box_type_qty")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            box_type = str(item.get("type") or "").strip()
            if not box_type:
                continue
            qty = int(item.get("qty") or 1)
            if box_type not in counts:
                counts[box_type] = 0
                order.append(box_type)
            counts[box_type] += qty
    return [BoxGroup(b_type=t, box_num=counts[t]) for t in order]


def _container_of(row: dict) -> list[ContainerInfo]:
    """行内箱信息：一行至多一条（箱号/箱型/封条号，两者皆空不建）。"""
    container_no = str(row.get("container_no") or "").strip()
    seal_no = str(row.get("seal_no") or "").strip()
    if not container_no and not seal_no:
        return []
    return [
        ContainerInfo(
            container_no=container_no or None,
            box_type=_box_type_of(row),
            seal_no=seal_no or None,
        )
    ]


def _pick_month(row: dict) -> str | None:
    """账期 YYYY-MM：order_date 优先，work_date 回退。"""
    for key in ("order_date", "work_date"):
        value = row.get(key)
        if value and len(str(value)) >= 7:
            return str(value)[:7]
    return None


def group_canonical(
    rows: list[dict],
    template: dict,
    period: BillPeriod | None = None,
) -> list[CanonicalOrder]:
    """标准字段行一行一票 → CanonicalOrder 列表（TMS 通道）。

    2026-08-31 业务拍板：每条数据行独立成单，不再按模板 group_key 归集
    （配置废弃不再读取）；TMS 允许同提单号多条订单。每行的箱信息聚合为
    containers/box_groups、费用为行级金额；必填（bl_no/box_groups）缺失
    登记 missing_fields，不阻塞。输出保持账单行序。
    """
    return [_canonical_from_row(row, template, period) for row in rows]


def _fees_of(row: dict, template: dict) -> tuple[list[FeeItem], dict[str, FeeReconcile]]:
    """行级费用聚合（T10/T14）：按 (通道, 标准费目码) 累加 → FeeItem + 通道对账。

    - import:false 项（税金等）excluded=True（不录入仅对账，金额进排除项合计）；
    - to_other（code=other）原名去重进 note（payload 拼「原名 ¥金额」列表）；
    - 对账恒等：bill_total（账单锚点列「合计/小计」Σ，含排除项）− recorded_total
      = excluded_total，容差 0.01；超差 ok=False 进对账报告（只报告不拦截）。
    """
    fees_cfg = template.get("fees", {}) or {}
    channels = fees_cfg.get("channels") or {}
    default_channel = next(iter(channels.values()), "shou")

    by_key: dict[tuple[str, str], FeeItem] = {}
    reconcile: dict[str, FeeReconcile] = {}
    for item in row.get("_fees") or []:
        ch = str(item.get("channel") or "shou")
        code = str(item.get("code") or "other")
        money = Decimal(str(round(float(item.get("money") or 0), 2)))
        importable = bool(item.get("import", True))
        rec = reconcile.setdefault(ch, FeeReconcile())
        key = (ch, code)
        fee = by_key.get(key)
        if fee is None:
            fee = FeeItem(
                channel=ch,
                code=code,
                money=Decimal("0"),
                note=None,
                excluded=not importable,
            )
            by_key[key] = fee
        fee.money += money
        if code == "other":
            _merge_fee_note(fee, item.get("name"))
        if importable:
            rec.recorded_total += money
        else:
            rec.excluded_total += money
    for section, anchor_map in (row.get("_anchors") or {}).items():
        ch = channels.get(section) or default_channel
        rec = reconcile.setdefault(ch, FeeReconcile())
        for name, money in anchor_map.items():
            # bill_total 只取「合计/小计」列（已收/未收等状态列不参与）
            if "合计" in name or "小计" in name:
                rec.bill_total += Decimal(str(money))
    for rec in reconcile.values():
        rec.diff = rec.bill_total - rec.recorded_total - rec.excluded_total
        # 无锚点（bill_total=0，账单侧无有效合计列）不算 mismatch——与金科信
        # no_anchor 语义一致（只报告不拦截，缺锚点不误报）；有锚点才判恒等
        rec.ok = rec.bill_total == 0 or abs(rec.diff) <= Decimal("0.01")
    return list(by_key.values()), reconcile


def _merge_fee_note(fee: FeeItem, name) -> None:
    """to_other 原名去重拼接（保留出现顺序）。"""
    if not name:
        return
    parts = fee.note.split(",") if fee.note else []
    if name not in parts:
        parts.append(name)
    fee.note = ",".join(parts)


def _canonical_from_row(
    row: dict, template: dict, period: BillPeriod | None
) -> CanonicalOrder:
    """单行 → CanonicalOrder（一行一票；每行独立成单）。"""
    template_id = template.get("template_id", "")
    # 单值字段：非空直取（行序即账单出现顺序）
    values: dict[str, object] = {
        name: value
        for name in _SINGLE_FIELDS
        if (value := row.get(name)) is not None and str(value).strip()
    }
    bl_no = clean_group_key(values.get("bl_no"))
    box_groups = _box_groups_of(row)
    containers = _container_of(row)
    month = values.get("month") or _pick_month(row)
    fees, fee_reconcile = _fees_of(row, template)

    # 车牌清洗（Excel 数字单元格浮点尾巴 9486.0 → 9486，与旧链路同口径）；
    # 一行一票后单行至多一个车牌，历史「多车牌并入 remark」段不可能出现
    # （2026-08-26 多车牌防丢失口径由去重键提单号+箱号承接）
    if values.get("plate_no"):
        values["plate_no"] = clean_plate_no(values["plate_no"])

    order = CanonicalOrder(
        bl_no=bl_no or values.get("bl_no"),
        box_groups=box_groups,
        containers=containers,
        month=month,
        fees=fees,
        fee_reconcile=fee_reconcile,
        source_template=template_id,
        # 行序号（清洗浮点尾巴）：去重键的行序号兜底段（无箱号时）
        row_seq=clean_group_key(row.get("seq")),
        row_count=1,
        **{k: v for k, v in values.items() if k != "bl_no"},
    )
    # 费用通道默认值（payload 发射用）：模板 fees.fee_defaults 烘焙进私有属性
    order._fee_defaults = (template.get("fees", {}) or {}).get("fee_defaults") or {}
    # 必填缺失登记（配置 required 优先，缺省 bl_no/box_groups）
    if not order.bl_no:
        order.add_missing("bl_no")
    if not order.box_groups:
        order.add_missing("box_groups")
    if not order.customer_name:
        order.add_missing("customer_name")
    return order
