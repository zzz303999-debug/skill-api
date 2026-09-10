"""标准字段归集（TMS 通道，模板配置驱动；见《字段映射表》§4）。

标准字段语义行（columns 目标 = CanonicalOrder 字段）一行一票 → CanonicalOrder；
与旧链路 aggregator.py（BillRow → BillOrder）并列，由 service 层按模板识别
结果分流。清洗口径（车牌浮点尾巴/行序号）复用旧链路公开函数，两通道一致。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..fees.fee_name_map import (
    canonicalize_fee_name,
    is_dynamic_code,
    other_alias_names,
)
from ..schema import (
    BillOrder,
    BillPeriod,
    BoxGroup,
    CanonicalOrder,
    ContainerInfo,
    FeeItem,
    FeeReconcile,
)
from .aggregator import clean_group_key, clean_plate_no

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


def _boxes_of(row: dict) -> tuple[list[BoxGroup], Any]:
    """行内箱聚合：逐项按箱型累加数量（保出现序）+ 首项箱型原文（container 用）。

    首项返回原 _box_type_of 语义：items[0].get("type") 原文不清洗/不判空。"""
    counts: dict[str, int] = {}
    order: list[str] = []
    items = row.get("box_type_qty")
    first = None
    if isinstance(items, list):
        if items and isinstance(items[0], dict):
            first = items[0].get("type")
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
    return [BoxGroup(b_type=t, box_num=counts[t]) for t in order], first


def _container_of(row: dict, box_type: Any) -> list[ContainerInfo]:
    """行内箱信息：一行至多一条（箱号/箱型/封条号，两者皆空不建）。"""
    container_no = str(row.get("container_no") or "").strip()
    seal_no = str(row.get("seal_no") or "").strip()
    if not container_no and not seal_no:
        return []
    return [
        ContainerInfo(
            container_no=container_no or None,
            box_type=box_type,
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

    业务拍板：每条数据行独立成单，不再按模板 group_key 归集
    （配置废弃不再读取）；TMS 允许同提单号多条订单。每行的箱信息聚合为
    containers/box_groups、费用为行级金额；必填（bl_no/box_groups）缺失
    登记 missing_fields，不阻塞。输出保持账单行序。
    """
    return [_canonical_from_row(row, template, period) for row in rows]


def _fees_of(row: dict, template: dict) -> tuple[list[FeeItem], dict[str, FeeReconcile]]:
    """行级费用聚合（T10/T14）：按 (通道, 费目码) 累加 → FeeItem + 通道对账。

    - import:false 项（税金等）excluded=True（不录入仅对账，金额进排除项合计）；
    - code=other 仅剩真其它费列（列名=其它费近义，fee_map 按列名判定后其余
      已转动态码独立费目，拍板）；其它费近义名自身不写 note（冗余）；
    - 独立费目（动态码）note 保留原名（建档命名用；建档失败时 apply 降级归并
      其它费保底）；
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
        name = item.get("name")
        if code == "other":
            # 真其它费列：近义名自身不写 note（避免 note="其它费"冗余）；
            # 若多列不同近义名归并其它费则保留原名可读性
            if name and name not in other_alias_names():
                _merge_fee_note(fee, name)
        elif is_dynamic_code(code) and fee.note is None and name:
            # 模板外动态码：原名保留——建档命名 + 降级归并保底原料
            # （标准码 note 恒 None——避免响应 note 字段意外扩展）
            fee.note = str(name)
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
    bl_no_raw = values.pop("bl_no", None)
    bl_no = clean_group_key(bl_no_raw)
    box_groups, first_box_type = _boxes_of(row)
    containers = _container_of(row, first_box_type)
    month = values.get("month") or _pick_month(row)
    fees, fee_reconcile = _fees_of(row, template)

    # 车牌清洗（Excel 数字单元格浮点尾巴 9486.0 → 9486，与旧链路同口径）；
    # 一行一票后单行至多一个车牌，历史「多车牌并入 remark」段不可能出现
    # （多车牌防丢失口径由去重键提单号+箱号承接）
    if values.get("plate_no"):
        values["plate_no"] = clean_plate_no(values["plate_no"])

    order = CanonicalOrder(
        bl_no=bl_no or bl_no_raw,
        box_groups=box_groups,
        containers=containers,
        month=month,
        fees=fees,
        fee_reconcile=fee_reconcile,
        source_template=template_id,
        # 行序号（清洗浮点尾巴）：去重键的行序号兜底段（无箱号时）
        row_seq=clean_group_key(row.get("seq")),
        row_count=1,
        **values,
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


def _first_nonempty(d: dict[str, Any], *keys: str) -> Any | None:
    """按序取首个非空值。"""
    for key in keys:
        value = d.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def to_canonical(order: BillOrder, source_template: str = "jinxin_v1") -> CanonicalOrder:
    """BillOrder → CanonicalOrder 转换（既有流程零感知；内置模板家族的 TMS 通道入口）。

    由 schema 迁入归集簇（schema 回归纯契约）：旧链路订单 → 标准订单
    的转换与 group_canonical 同属归集职责。映射口径见《TMS业务订单新增接口-
    逆推规范》§4；旧流程 order_data 的扁平键逐项对齐到标准字段；缺失项（必填
    bl_no/box_groups）登记 missing_fields。费用：order_data["shou"]（费目名 →
    金额）经费目别名字典归一为 FeeItem（未命中字典 → 动态码独立费目 + 原名进
    note，拍板），金额为 0/空不生成记录。
    """
    data = order.order_data or {}
    box_groups = [
        BoxGroup(b_type=box["b_type"], box_num=int(box.get("box_num", 1)))
        for box in data.get("box", []) or []
        if isinstance(box, dict) and box.get("b_type")
    ]
    driver = (data.get("driver") or [{}])[0]
    fees: list[FeeItem] = []
    for entry in data.get("shou", []) or []:
        if not isinstance(entry, dict):
            continue
        for name, spec in entry.items():
            if not isinstance(spec, dict):
                continue
            money = spec.get("money")
            if not isinstance(money, (int, float)) or money == 0:
                continue  # 空值与 0 均不生成费用记录（账单侧合计仍参与对账）
            code, note = canonicalize_fee_name(str(name))
            fees.append(
                FeeItem(
                    channel="shou",
                    code=code,
                    money=Decimal(str(round(float(money), 2))),
                    note=note,
                )
            )
    canonical = CanonicalOrder(
        bl_no=order.order_num1 or _first_nonempty(data, "order_num1"),
        box_groups=box_groups,
        # 一行一票：本行箱号结构化进 containers（去重组合键取值处）
        containers=(
            [ContainerInfo(container_no=order.container_no)] if order.container_no else []
        ),
        customer_name=order.c_title or _first_nonempty(data, "c_title"),
        customer_no=_first_nonempty(data, "c_sn"),
        customer_contact=_first_nonempty(data, "c_name"),
        contact_phone=_first_nonempty(data, "c_phone"),
        door_point=_first_nonempty(data, "factory_name"),
        load_address=_first_nonempty(data, "factory_bei"),
        port_area=_first_nonempty(data, "b_wharf"),
        work_date=_first_nonempty(driver, "b_date"),
        pickup_point=_first_nonempty(driver, "b_get_address"),
        return_point=_first_nonempty(driver, "b_back_address"),
        plate_no=_first_nonempty(driver, "d_num"),
        driver_name=_first_nonempty(driver, "d_name"),
        driver_phone=_first_nonempty(driver, "d_phone"),
        month=_first_nonempty(data, "month"),
        remark=_first_nonempty(data, "c_note"),
        fees=fees,
        source_template=source_template,
        row_count=order.row_count,
    )
    # 必填缺失登记（旧流程 missing 口径 → 标准字段名）
    if order.order_num1 is None or not canonical.bl_no:
        canonical.add_missing("bl_no")
    if not box_groups:
        canonical.add_missing("box_groups")
    if order.c_title is None and not canonical.customer_name:
        canonical.add_missing("customer_name")
    return canonical
