"""CanonicalOrder → TMS 业务订单新增接口 form-data（《逆推规范》§4 映射表）。

接口怪癖封装在本层，不外泄：
- form-data 扁平括号记法（group[idx][field]）；create_order=true 触发下单，
  o_id 留空 = 新增；
- 直连走 AddWork 端点 + sk 头（publishCreateOrder 不可行）；b 字段 JSON
  双写实测非必需，不再发送；
- type 枚举仅确认 1=出口（其余留 TODO，默认 1）；多箱号 b_num 只写首箱
  （第二箱号无落点，split_per_container 配置位预留）；
- 费用四通道（T13）：键位按《逆推规范》§2.7，合计由我方计算回写（TMS
  汇总回显不可信）。

build_order_payload 为纯函数，返回 (form 字段字典, 警告清单)；客户端与
测试共用同一实现（保证 live 与生产一致）。
"""

from __future__ import annotations

from ..schema import CanonicalOrder, FeeItem

# type 枚举：1=出口（抓包确认）；其余值待实测补全（TODO），暂默认 1
_TYPE_EXPORT = "1"

# 多箱号拆分调用配置位（《逆推规范》§6-3 待验证）：true 时逐箱拆单，本轮默认 false
SPLIT_PER_CONTAINER = False

# 费用通道默认值（模板 fees.fee_defaults 未配置时的兜底，T13 实证值）
_FEE_DEFAULT_PRICE_TYPE = "1"
_FEE_DEFAULT_IS_PROFIT = "1"
_FEE_DEFAULT_DAI_DIAN = "1"


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
    """备注 + 业务编号/业务类型备查（竞品编号 TMS 无直接字段，拼入备注）。

    结果同时发 b_note 与 c_note（用户拍板）：TMS 界面「业务备注」落点未实证，
    双发零风险（PHP 控制器硬读键名，缺键才 204）。
    """
    segments: list[str] = []
    remark = _first(order.remark)
    if remark:
        segments.append(remark)
    if order.biz_no:
        segments.append(f"业务编号：{order.biz_no}")
    if order.biz_type:
        segments.append(f"业务类型：{order.biz_type}")
    return "；".join(segments) if segments else None


def collect_unmapped_note(order: CanonicalOrder) -> str | None:
    """收集无 TMS 表单落点的标准字段 → 报告说明（预览/对账报告可见）。

    当前客户字段已由 `c_title` 承接（旧链路 AddWork 实测 dump：
    c_title=小王），其余标准字段均有落点（《逆推规范》§4）；本函数为扩展点，
    未来 F3 档案匹配策略（客户未命中报告）在此扩展。
    """
    segments: list[str] = []
    return "；".join(segments) if segments else None


def _emit_fees(form: dict[str, str], order: CanonicalOrder) -> None:
    """费用四通道发射（T13，键位按《逆推规范》§2.7）。

    - 每通道每费目：`{channel}[0][{tms_name}][money]` 两位小数 + `[price_id]` +
      `price_type`/`is_profit`/`dai_dian`（模板 fees.fee_defaults，默认 "1"）；
    - 合计由我方计算回写：`driver[0][get_ys_zj]`=Σshou、`[pay_yf_zj]`=Σpay、
      `cost[0][supplier_hj_zj]`=Σcost（键位按抓包原样）；duo_get 合计预留不出口；
    - **有费用的通道 note 必须恒发**（控制器硬读该键，缺键 204 拒单
      Undefined index: note）：to_other 原名拼「原名 ¥金额」列表，无则空串；
    - 空通道整段省略；excluded 项（税金/price_id 待补）不录入、不进合计。
    """
    defaults = getattr(order, "_fee_defaults", {}) or {}
    price_type = defaults.get("price_type") or _FEE_DEFAULT_PRICE_TYPE
    is_profit = defaults.get("is_profit") or _FEE_DEFAULT_IS_PROFIT
    dai_dian = defaults.get("dai_dian") or _FEE_DEFAULT_DAI_DIAN

    by_channel: dict[str, list[FeeItem]] = {}
    for fee in order.fees:
        if fee.excluded or fee.price_id is None or not fee.tms_name:
            continue  # 不录入项（税金/price_id 待补）整段省略
        by_channel.setdefault(fee.channel, []).append(fee)

    totals: dict[str, float] = {}
    notes: dict[str, list[str]] = {}
    for channel, items in by_channel.items():
        for fee in items:
            prefix = f"{channel}[0][{fee.tms_name}]"
            form[f"{prefix}[money]"] = f"{fee.money:.2f}"
            form[f"{prefix}[price_id]"] = str(fee.price_id)
            form[f"{prefix}[price_type]"] = price_type
            form[f"{prefix}[is_profit]"] = is_profit
            form[f"{prefix}[dai_dian]"] = dai_dian
            if channel == "cost":
                # cost 费目条目硬读 driver_name（《逆推规范》§2.7；缺键 204 拒单）
                form[f"{prefix}[driver_name]"] = ""
            totals[channel] = totals.get(channel, 0.0) + float(fee.money)
            if fee.code == "other" and fee.note:
                notes.setdefault(channel, []).append(f"{fee.note} ¥{fee.money:.2f}")
    for channel in by_channel:
        # 通道级 note 恒发（控制器硬读，缺键 204 拒单）
        form[f"{channel}[0][note]"] = "；".join(notes.get(channel, []))

    # 合计回写（键位按抓包原样）：应收/应付进 driver[0]，成本进 cost[0]
    shou = totals.get("shou")
    if shou:
        form["driver[0][get_ys_zj]"] = f"{shou:.2f}"
    pay = totals.get("pay")
    if pay:
        form["driver[0][pay_yf_zj]"] = f"{pay:.2f}"
    cost = totals.get("cost")
    if cost:
        form["cost[0][supplier_hj_zj]"] = f"{cost:.2f}"


def _archive_id(order: CanonicalOrder, kind: str) -> str:
    """阶段三已建档档案的 TMS 主键（T20 回填）；未建档 → ""（维持文本提交）。"""
    refs = getattr(order, "_archive_refs", {}) or {}
    return str((refs.get(kind) or {}).get("archive_id") or "")


def build_order_payload(order: CanonicalOrder) -> tuple[dict[str, str], list[str]]:
    """CanonicalOrder → (form-data 字段字典, 警告清单)。

    字段布局（《逆推规范》§2/§4）：单头业务字段 + 客户（c_title/c_name/c_sn）+
    货物明细 data[0] + 箱信息（box[N] + 顶层 b_num/b_lock）+ 门点 + 派车段
    driver[0]；费用四通道见 _emit_fees。

    客户字段口径（实测，见《逆推规范》§4）：c_title=客户名称（自由文本落点）；
    c_name=客户联系人（UI「联系人」）；c_id 已建档回填 id、未建档空串（控制器
    硬读，缺键 204 拒单）；c_note 与 b_note 双发同内容（用户拍板）。
    """
    warnings: list[str] = []
    month = _pick_month(order)
    note = _build_note(order)
    containers = order.containers or []
    first_container = containers[0] if containers else None
    unmapped = collect_unmapped_note(order)
    if unmapped:
        warnings.append(unmapped)

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
        # 客户：c_title=客户名称；c_name=客户联系人（勿填客户名称）；
        # c_id：已建档回填档案 id（T20）；未建档恒发空串（控制器硬读，缺键 204）
        "c_title": order.customer_name or "",
        "c_name": order.customer_contact or "",
        "c_phone": order.contact_phone or "",
        "c_sn": order.customer_no or "",
        # c_note 与 b_note 双发同内容（用户拍板：落点未实证，双发零风险）
        "c_note": note or "",
        "c_id": _archive_id(order, "client"),
        # 门点：已建档回填 factory_id + b_factory_address_msg（T20；键位未实证，
        # 先按同键发，多发键安全）；未建档维持文本
        "factory_name": order.door_point or "",
        "factory_id": _archive_id(order, "factory"),
        "b_factory_address_msg": (
            str(order.load_address).strip() if order.load_address else ""
        ),
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

    # 费用四通道（T13）：有费用的通道整段发射 + 合计回写；空通道省略
    if order.fees:
        _emit_fees(form, order)

    return form, warnings
