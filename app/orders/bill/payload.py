"""CanonicalOrder → TMS 业务订单新增接口 form-data（《逆推规范》§4 映射表）。

接口怪癖全部封装在本层，不外泄：
- form-data 扁平括号记法（group[idx][field]）；
- `create_order=true` 触发下单、`o_id` 留空 = 新增；
- 端点实测（2026-08-13）：TMS 直连走 AddWork 端点 + sk 头 + create_order=true
  （publishCreateOrder 强制要求有效 userId+roomId，不可行）；b 字段（JSON 双写）
  实测非必需，不再发送；
- `type` 枚举：目前仅确认 1=出口，其余值留 TODO（《逆推规范》§6-5）默认 1；
- 多箱号 `b_num`：实测首箱可正确写入（VRCU000001），第二箱号无落点
  （《逆推规范》§6-3 待界面确认），配置位 `split_per_container` 预留（默认 false）；
- 费用四通道（shou/pay/duo_get/cost）本轮整体省略（§5 范围外）。

build_order_form 为纯函数，返回 (form 字段字典, 警告清单)；客户端与
测试共用同一实现（保证 live 与生产一致）。
"""

from __future__ import annotations

from .schema import CanonicalOrder

# type 枚举：1=出口（2026-08-13 抓包确认）；其余值待实测补全（TODO），暂默认 1
_TYPE_EXPORT = "1"

# 多箱号拆分调用配置位（《逆推规范》§6-3 待验证）：true 时逐箱拆单，本轮默认 false
SPLIT_PER_CONTAINER = False


def _first(value: str | None) -> str | None:
    """空值 → None（不发送空键；与既有「非必填空值省略键」语义一致）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _type_value(order: CanonicalOrder) -> str:
    """业务类型 → type 枚举：仅确认 1=出口（biz_type/io_type 命中出口语义）；
    其余值（进口/倒箱/内装…）留 TODO（《逆推规范》§6-5），暂默认 1。"""
    biz = _first(order.biz_type) or _first(order.io_type) or ""
    if "出口" in biz or biz == "出":
        return _TYPE_EXPORT
    # TODO(type)：进口/倒箱等枚举值待界面切换抓包补全，未确认前默认 1
    return _TYPE_EXPORT


def _pick_month(order: CanonicalOrder) -> str | None:
    """账期 YYYY-MM：month 优先，order_date/work_date 回退。"""
    for value in (order.month, order.order_date, order.work_date):
        if value and len(str(value)) >= 7:
            return str(value)[:7]
    return None


def _build_note(order: CanonicalOrder) -> str | None:
    """b_note：备注 + 业务编号/业务类型备查（竞品编号 TMS 无直接字段，拼入备注）。"""
    segments: list[str] = []
    remark = _first(order.remark)
    if remark:
        segments.append(remark)
    if order.biz_no:
        segments.append(f"业务编号：{order.biz_no}")
    if order.biz_type:
        segments.append(f"业务类型：{order.biz_type}")
    return "；".join(segments) if segments else None


def build_order_form(order: CanonicalOrder) -> tuple[dict[str, str], list[str]]:
    """CanonicalOrder → (form-data 字段字典, 警告清单)。

    字段布局（《逆推规范》§2/§4）：单头业务字段（顶层）+ 客户（c_name/c_sn）+
    货物明细 data[0]（b_order_num/j/m/hh）+ 箱信息（box[N] + 顶层 b_num/b_lock）+
    门点（factory_name）+ 派车段 driver[0]。费用四通道省略。
    """
    warnings: list[str] = []
    month = _pick_month(order)
    note = _build_note(order)
    containers = order.containers or []
    first_container = containers[0] if containers else None

    # 多箱号写法（《逆推规范》§6-3）：b_num 顶层单值，实测首箱可正确写入；
    # 第二箱号无落点（split_per_container 配置位预留，待界面确认后调整）
    b_num = None
    if first_container is not None and first_container.container_no:
        b_num = first_container.container_no
    if len(containers) > 1:
        warnings.append(
            f"一票多箱（{len(containers)} 箱），b_num 已写入首箱"
            f"（{b_num or '无'}），其余箱号暂不发送；"
            "split_per_container 配置位预留（默认 false），待界面确认后调整"
        )

    form: dict[str, str] = {
        # 操作元字段：常量（order_num1 票数、o_id 空=新增、create_order 触发下单）
        "order_num1": "1",
        "o_id": "",
        "create_order": "true",
        "appendCost": "true",
        # 单头业务字段
        "type": _type_value(order),
        "b_note": note or "",
        # 客户
        "c_name": order.customer_name or "",
        "c_sn": order.customer_no or "",
        # 门点
        "factory_name": order.door_point or "",
        # 箱信息（顶层单值）
        "b_num": b_num or "",
        "b_lock": (first_container.seal_no if first_container else None) or "",
        # 运输段
        "b_ship_name": order.vessel or "",
        "b_ship_num": order.voyage or "",
        "b_ship_company": order.shipping_company or "",
        "b_wharf": order.port_area or "",
        "b_end_port": order.discharge_port or "",
        "b_open_ship_time": order.port_open_time or "",
        "b_close_ship_time": order.port_cut_time or "",
    }
    if month:
        form["month"] = month

    # 货物明细 data[0]：提单号必填，件数/毛重/货名选填
    form["data[0][b_order_num]"] = order.bl_no or ""
    form["data[0][j]"] = str(order.pieces) if order.pieces is not None else ""
    form["data[0][m]"] = str(order.gross_weight) if order.gross_weight is not None else ""
    form["data[0][hh]"] = order.cargo_name or ""

    # 箱信息 box[N]：一票多箱型展开为数组（40HQ*2 → 一条 box_num=2）
    for i, group in enumerate(order.box_groups):
        form[f"box[{i}][b_type]"] = group.b_type
        form[f"box[{i}][box_num]"] = str(group.box_num)

    # 派车段 driver[0]：做箱时间/提还箱点/司机信息
    driver: dict[str, str] = {
        "b_date": order.work_date or "",
        "b_get_address": order.pickup_point or "",
        "b_back_address": order.return_point or "",
        "d_name": order.driver_name or "",
        "d_num": order.plate_no or "",
        "d_phone": order.driver_phone or "",
        "d_group": order.fleet or "",
    }
    for key, value in driver.items():
        form[f"driver[0][{key}]"] = value

    return form, warnings


def build_order_payload(order: CanonicalOrder) -> tuple[dict[str, str], list[str]]:
    """TMS 直连完整表单：扁平字段 + create_order=true（b 双写实测非必需，不再发送）。

    返回 (form 字段字典, 警告清单)。
    """
    return build_order_form(order)
