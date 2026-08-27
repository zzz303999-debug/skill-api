"""竞品账单导入下游客户端：调用方登录 TMS 取 sk → AddWork 表单逐单下单。

对齐《竞品账单导入接口文档》v1.4 §2.2/§3.7/§5.3 与 2026-08-12 业务决策：
- 鉴权：sk 由调用方登录 TMS 后经 /orders/bill/import 请求头透传（2026-08-19 起，
  移除服务端 GetWebKey → login 换取链路），逐单串行下单，不做缓存
- 下单通道固定 AddWork 表单（2026-08-13 起实测：AddWork 端点 + sk 头 +
  create_order=true 可直连下单；publishCreateOrder 强制要求 userId+roomId，204
  拒单，json 通道已移除）
- 任何情况不自动重试（防重复下单）；单失败不影响后续订单
- 超时/网络异常 → 该单 error（order_upstream_error），不中断整批
- missing_fields 非空的单照常提交（本服务不拦截）
- 重复上传去重（成功单注册表）：提交前查 imported_registry（per-bl_no 锁包住
  「查重→提交→登记」临界区），命中 → skipped（不调下游，sn 回显首次创建）；
  提交成功才登记（登记失败仅日志不冒泡）；无提单号单不查不登照常提交；
  service 层预判已标记 skipped 的单直接跳过（create_result 非 None）
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings
from app.logging_conf import get_logger

from ..http_client import post_form, unpack_json
from .imported_registry import get_imported_registry, lock_for, normalize
from .schema import BillOrder

log = get_logger(__name__)


def _nan_to_none(_token: str) -> None:
    """json.loads parse_constant：下游 NaN/Infinity 字面量 → None（JSON null）。

    避免透传进响应体后序列化失败（starlette JSONResponse allow_nan=False
    遇 float nan 抛 ValueError → 500），也避免调用方收到非法 JSON 数值。
    """

    return None


# AddWork 固定值字段（§2.2/§5.3 + 2026-08-11 抓包）：appendCost=true、o_id 新建为空、
# 图片/多皮重数组为空。duo_get/cost 合计恒发 0.00（BillOrder.order_data 只归集应收，
# 无 pay/duo_get/cost 条目；标准通道四通道发射见 payload.py _emit_fees/T13，
# 此处不重复构造）
_FIXED_FIELDS: dict[str, str] = {
    "appendCost": "true",
    "o_id": "",
    "img_data": "[]",
    "img_data_id": "[]",
    "multiple_tare": "[]",
    "duo_get[0][duo_get_hj_zj]": "0.00",
    "cost[0][supplier_hj_zj]": "0.00",
}

# 顶层固定键集（§2.2 已确认的键名，全部恒发；有值填值，无值空字符串，
# 满足 PHP 控制器直接索引读取——2026-08-12 实测缺 c_id/note 即拒单）：
# 订单（type 固定 1；c_id/note 本期恒空）/ 运输 / 门点
_TOP_FIXED_KEYS: tuple[str, ...] = (
    "order_num1",
    "type",
    "c_title",
    "c_name",
    "c_phone",
    "c_sn",
    "c_note",
    "c_id",
    "month",
    "note",
    "b_ship_name",
    "b_ship_num",
    "b_ship_company",
    "b_start_dock",
    "b_end_port",
    "b_end_dock",
    "b_wharf",
    "b_open_ship_time",
    "b_close_ship_time",
    "b_operator",
    "factory_name",
    "factory_bei",
    "factory_id",
    "b_factory_not",
    "b_tare",
)
# type 固定为 1（§5.3）；c_id/note 本期恒发空字符串（账单无客户 ID；note 为订单级备注，
# 实测缺键即拒单，先恒空保过，如需填充再议来源）
_TOP_FIXED_VALUES: dict[str, str] = {"type": "1"}

# data[N] 子键固定集（§2.2 确认：b_order_num/j/m/t/hh/mt；note 为 2026-08-12
# live 实证：顶层已发仍报 Undefined index: note，控制器读的是嵌套条目），全部恒发：
# b_order_num 取实际提单号，其余本期恒空
_DATA_KEYS: tuple[str, ...] = ("b_order_num", "j", "m", "t", "hh", "mt", "note")

# driver[N] 固定键集（§2.2/§5.1 全量键名 + live 实证 note）：无论有无值都发送，
# 无值发空字符串
_DRIVER_KEYS: tuple[str, ...] = (
    "d_id",
    "b_date",
    "b_date_time_start",
    "b_get_address",
    "b_back_address",
    "d_name",
    "d_num",
    "d_phone",
    "distance",
    "you_hao",
    "driver_note",
    "get_ys_zj",
    "pay_yf_zj",
    "note",
)

# shou 属性键固定集（2026-08-11 抓包确认：price_id/price_type/is_profit/dai_dian
# + money；note 为 live 实证下沉）：全部费用挂在 shou[0] 单条目下，属性恒发
_SHOU_KEYS: tuple[str, ...] = (
    "money",
    "price_id",
    "price_type",
    "is_profit",
    "dai_dian",
    "note",
)

# 不发送字段（§5.3/§2.2 明确）：user_name/car_name/section_name（走 web key 登录身份）、
# box_type_text/box_type/顶层 b_date/b_date_pick、pay[]/duo_get[]/cost[] 费用条目
# （order_data 只含 shou 费用；标准通道四通道发射见 payload.py _emit_fees/T13）、
# audit_status/b_lock 等状态类键
# —— 实现上通过固定键集天然排除，无需额外过滤

_ERROR_DESCRIPTION = "订单系统拒绝了请求或不可达，请稍后重试"


def _put_value(flat: dict[str, str], key: str, value: Any) -> None:
    """null → 空字符串，其余 str()（展平键值统一字符串化）。"""
    flat[key] = "" if value is None else str(value)


def flatten_order(order_data: dict[str, Any]) -> dict[str, str]:
    """order_data（嵌套）→ 顶层展平表单字段（超集键集 + 值填充，§2.2/§5.3）。

    PHP 控制器硬读键名（未知键忽略、缺键报错），故按 §2.2/抓包已确认键名发超集，
    note 除顶层外同步下沉到全部嵌套条目（2026-08-12 live 实证：顶层 note 已发
    仍报 Undefined index: note，控制器读的是嵌套结构）：
    - 顶层固定键集 25 键（订单/运输/门点，type 固定 1，c_id/note 恒空）
    - data[N] 7 子键（b_order_num 实际值，j/m/t/hh/mt/note 恒空）、
      driver[N] 14 键全部恒发
    - shou 单条目形态（抓包 2026-08-11）：所有费用挂 shou[0] 下不同费用名键，
      每费用名 6 属性键（money 实际金额，price_id/price_type/is_profit/dai_dian/note 恒空）；
      有费用时补发通道级 shou[0][note]（2026-08-25 测试环境实证：缺键 204 拒单）
    - box[N][b_type/box_num] 按实际内容，box[N][note] 恒空
    - 固定值：type=1 / appendCost=true / o_id="" / img_data=[] / img_data_id=[] /
      multiple_tare=[] / duo_get[0][duo_get_hj_zj]=0.00 / cost[0][supplier_hj_zj]=0.00
    - 不发送 user_name/car_name/section_name/box_type_text/box_type/顶层 b_date/
      b_date_pick、pay[]/duo_get[]/cost[] 费用条目（旧链路 order_data 仅含 shou；
      标准通道四通道发射见 payload.py _emit_fees/T13）、audit_status/b_lock 等
    """
    flat: dict[str, str] = dict(_FIXED_FIELDS)

    for key in _TOP_FIXED_KEYS:
        if key in _TOP_FIXED_VALUES:
            flat[key] = _TOP_FIXED_VALUES[key]
        else:
            _put_value(flat, key, order_data.get(key))

    for i, entry in enumerate(order_data.get("data", []) or []):
        if not isinstance(entry, dict):
            continue
        for key in _DATA_KEYS:
            _put_value(flat, f"data[{i}][{key}]", entry.get(key))

    for i, entry in enumerate(order_data.get("box", []) or []):
        if not isinstance(entry, dict):
            continue
        _put_value(flat, f"box[{i}][note]", "")
        for key in ("b_type", "box_num"):
            if key in entry:
                _put_value(flat, f"box[{i}][{key}]", entry[key])

    for i, entry in enumerate(order_data.get("driver", []) or []):
        if not isinstance(entry, dict):
            continue
        for key in _DRIVER_KEYS:
            _put_value(flat, f"driver[{i}][{key}]", entry.get(key))

    shou_emitted = False
    for _i, entry in enumerate(order_data.get("shou", []) or []):
        if not isinstance(entry, dict):
            continue
        for name, spec in entry.items():
            if not isinstance(spec, dict):
                continue
            # 抓包形态：所有费用挂 shou[0] 单条目下（不同费用名作键）
            for key in _SHOU_KEYS:
                _put_value(flat, f"shou[0][{name}][{key}]", spec.get(key))
            shou_emitted = True
    if shou_emitted:
        # 通道级 note 恒发（2026-08-25 测试环境 live 实证：缺键 204 拒单
        # Undefined index: note，WorkOut.php:1460；对齐标准通道 T13 口径——
        # 生产多余键无害：PHP 控制器硬读键名，缺键报错、多余忽略）
        flat["shou[0][note]"] = ""

    return flat


def _error_result(message: str, *, details: dict[str, Any]) -> dict[str, Any]:
    """单失败 create_result（不抛异常，调用方按单处理，不中断整批）。"""
    return {
        "success": False,
        "sn": None,
        "error": {
            "code": "order_upstream_error",
            "message": message,
            "description": _ERROR_DESCRIPTION,
            "details": details,
        },
    }


def build_add_work_form(order_data: dict[str, Any]) -> dict[str, str]:
    """组装 AddWork 表单（§5.3）：a="{}"、c="{}"、b=URL 编码 JSON（键为展平
    写法、与顶层同内容）、顶层超集展平字段。纯函数，add_work 与联调脚本共用
    （保证 live 与生产同一份实现）。"""
    flat = flatten_order(order_data)
    return {
        "a": "{}",
        "c": "{}",
        "b": json.dumps(flat, ensure_ascii=False),
        **flat,
    }


def _parse_create_response(response: httpx.Response, *, step: str) -> dict[str, Any]:
    """下游下单响应 → create_result（add_work 响应口径）。

    code "200"（字符串/数字皆可）→ 成功取 data[0].sn（缺 sn 仍成功，sn=None）；
    成功时原样保留 data[0] 回显（upstream 键，对齐 /orders 的 upstream.data[0]）；
    其他 → error 三元组（不抛异常，调用方按单处理）。网络异常由调用方捕获。
    """
    if response.status_code >= 400:
        return _error_result(
            f"{step} returned an HTTP error: {response.status_code}",
            details={
                "status_code": response.status_code,
                "upstream_response": unpack_json(response.text),
            },
        )
    try:
        raw = response.json(parse_constant=_nan_to_none)
    except ValueError:
        return _error_result(
            f"{step} returned a non-JSON response",
            details={"body_preview": response.text[:500]},
        )
    if not isinstance(raw, dict):
        return _error_result(
            f"{step} response is not a JSON object",
            details={
                "response_type": type(raw).__name__,
                "body_preview": response.text[:500],
            },
        )
    if str(raw.get("code")) != "200":
        log.warning("jxt_order_rejected", extra={"step": step, "upstream_code": raw.get("code")})
        return _error_result(
            f"{step} rejected the order: {raw.get('msg', '')}",
            details={
                "upstream_code": raw.get("code"),
                "upstream_message": raw.get("msg"),
                "upstream_response": unpack_json(raw),
            },
        )
    data_list = raw.get("data")
    sn = None
    if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict):
        sn = data_list[0].get("sn")
    log.info("jxt_order_ok", extra={"step": step, "sn": sn})
    result: dict[str, Any] = {"success": True, "sn": sn, "error": None}
    if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict):
        result["upstream"] = data_list[0]  # 原始回显（对齐 /orders 的 upstream.data[0]）
    return result


def add_work(sk: str, order_data: dict[str, Any]) -> dict[str, Any]:
    """POST AddWork 创建一单 → create_result（{success, sn, error}）。

    表单（§5.3）：a="{}"、c="{}"、b=URL 编码 JSON（键为展平写法，与顶层同内容）、
    顶层展平字段；header sk；content-type application/x-www-form-urlencoded。
    响应口径见 _parse_create_response（code "200" → 取 data[0].sn）。
    """
    form = build_add_work_form(order_data)
    try:
        response = post_form(
            settings.jxt_addwork_url,
            form,
            name="AddWork",
            headers={"sk": sk},
            timeout=settings.jxt_timeout_seconds,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning(
            "jxt_add_work_network_error",
            extra={"error_type": exc.__class__.__name__},
        )
        return _error_result(
            f"AddWork network error: {exc.__class__.__name__}",
            details={"error_type": exc.__class__.__name__},
        )
    return _parse_create_response(response, step="AddWork")


def _register_imported(bl_no: str, sn, source_sha256: str | None) -> None:
    """登记创建成功单（去重注册表）；登记失败仅记日志，不冒泡。

    单已真实创建，登记失败（磁盘满/权限等）不应使响应变失败——丢失记录的
    后果是重导可能重复下单（见 imported_registry 模块 docstring）。
    """
    try:
        get_imported_registry().register(bl_no, sn=sn, source_sha256=source_sha256)
    except Exception as exc:  # noqa: BLE001 - 防御：登记失败不使成功单变失败
        log.warning(
            "imported_register_failed",
            extra={"bl_no": bl_no, "error_type": exc.__class__.__name__},
        )


def _skipped_result(sn: str | None) -> dict[str, Any]:
    """去重命中（已成功创建过）的 create_result：success=True + skipped 标记。"""
    return {"success": True, "skipped": True, "sn": sn, "error": None}


def create_orders(orders: list[BillOrder], sk: str, source_sha256: str | None = None) -> None:
    """逐单串行下单并原地填充 create_result。

    sk 由调用方登录 TMS 后透传（2026-08-19 起，不再服务端换取）；
    通道固定 AddWork 表单（2026-08-13 起：AddWork 端点 + sk 头 + create_order=true）。
    单失败不影响后续；任何情况不自动重试；missing_fields 非空照常提交。
    重复上传去重见模块 docstring（查重→提交→登记在 per-bl_no 锁内原子化）。
    """
    if not orders:
        return
    submit = add_work
    for order in orders:
        if order.create_result is not None:
            continue  # service 层预判已标记 skipped → 直接跳过
        bl = normalize(order.order_num1)
        if not bl:
            order.create_result = submit(sk, order.order_data or {})
            continue
        with lock_for(bl):
            rec = get_imported_registry().lookup(bl)
            if rec:
                order.create_result = _skipped_result(rec.get("sn"))
                continue
            order.create_result = submit(sk, order.order_data or {})
            if order.create_result.get("success"):
                _register_imported(bl, order.create_result.get("sn"), source_sha256)


def _parse_canonical_response(response: httpx.Response) -> dict[str, Any]:
    """TMS 通道响应判定（《逆推规范》§3）：code 为字符串 "200" → 成功，
    回取 data[0].sn（TMS 业务编号）与 data[0].o_id；其余 → error 三元组。
    网络/HTTP 异常由调用方捕获；判定失败不抛异常（按单处理）。
    """
    if response.status_code >= 400:
        return _error_result(
            f"order API returned an HTTP error: {response.status_code}",
            details={
                "status_code": response.status_code,
                "upstream_response": unpack_json(response.text),
            },
        )
    try:
        raw = response.json(parse_constant=_nan_to_none)
    except ValueError:
        return _error_result(
            "order API returned a non-JSON response",
            details={"body_preview": response.text[:500]},
        )
    if not isinstance(raw, dict):
        return _error_result(
            "order API response is not a JSON object",
            details={
                "response_type": type(raw).__name__,
                "body_preview": response.text[:500],
            },
        )
    if str(raw.get("code")) != "200":
        log.warning(
            "jxt_canonical_order_rejected", extra={"upstream_code": raw.get("code")}
        )
        return _error_result(
            f"order API rejected the order: {raw.get('msg', '')}",
            details={
                "upstream_code": raw.get("code"),
                "upstream_message": raw.get("msg"),
                "upstream_response": unpack_json(raw),
            },
        )
    data_list = raw.get("data")
    sn = None
    o_id = None
    if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict):
        sn = data_list[0].get("sn")
        o_id = data_list[0].get("o_id")
    log.info("jxt_canonical_order_ok", extra={"sn": sn, "o_id": o_id})
    result: dict[str, Any] = {"success": True, "sn": sn, "error": None}
    if o_id is not None:
        result["o_id"] = o_id
    if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict):
        # 原始回显（对齐 /orders 的 upstream.data[0]：sns/o_id/c_title 等回写字段）
        result["upstream"] = data_list[0]
    return result


def submit_canonical(sk: str, order) -> dict[str, Any]:
    """TMS 通道 POST 一单（form-data + create_order=true，AddWork 端点，sk 头鉴权）。

    payload 构造见 payload.build_order_payload（接口怪癖全部封装在适配层）；
    多箱号等 warning 仅记日志（不阻断下单）；响应判定见 _parse_canonical_response。
    端点实测（2026-08-13 最小报文验证）：publishCreateOrder 无论 JSON/form-data
    均强制要求有效 userId+roomId（204 拒单），AddWork 端点 + sk 头 + create_order=true
    即可成功下单（费用四通道全空，sn=EX26080356）——TMS 直连以此为准。
    """
    from .payload import build_order_payload

    form, warnings = build_order_payload(order)
    if warnings:
        log.warning(
            "canonical_payload_warning",
            extra={"bl_no": order.bl_no, "warnings": warnings},
        )
    try:
        response = post_form(
            settings.jxt_addwork_url,
            form,
            name="AddWork-canonical",
            headers={"sk": sk},
            timeout=settings.jxt_timeout_seconds,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning(
            "jxt_canonical_order_network_error",
            extra={"error_type": exc.__class__.__name__},
        )
        return _error_result(
            f"order API network error: {exc.__class__.__name__}",
            details={"error_type": exc.__class__.__name__},
        )
    return _parse_canonical_response(response)


def create_canonical_orders(orders, sk: str, source_sha256: str | None = None) -> None:
    """TMS 通道逐单串行下单（CanonicalOrder → form-data）并原地填充 create_result。

    sk 由调用方登录 TMS 后透传（2026-08-19 起，不再服务端换取）；单失败隔离
    不中断；任何情况不自动重试（防重复下单）；missing_fields 非空照常提交。
    响应回取 data[0].sn（TMS 业务编号）与 o_id。
    重复上传去重见模块 docstring（查重→提交→登记在 per-bl_no 锁内原子化）。
    """
    if not orders:
        return
    for order in orders:
        if order.create_result is not None:
            continue  # service 层预判已标记 skipped → 直接跳过
        bl = normalize(order.bl_no)
        if not bl:
            order.create_result = submit_canonical(sk, order)
            continue
        with lock_for(bl):
            rec = get_imported_registry().lookup(bl)
            if rec:
                order.create_result = _skipped_result(rec.get("sn"))
                continue
            order.create_result = submit_canonical(sk, order)
            if order.create_result.get("success"):
                _register_imported(bl, order.create_result.get("sn"), source_sha256)
