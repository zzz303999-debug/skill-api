"""AddWork 通道表单组装（与 payload.py 的 canonical/TMS 表单对仗）。

旧链路（BillOrder.order_data）→ AddWork 超集扁平表单：键集与展平怪癖封装在
本层——PHP 控制器硬读键名（未知键忽略、缺键报错 204 拒单）。
build_add_work_form 为纯函数，client 与联调脚本共用（live 与生产同一份实现）。
"""

from __future__ import annotations

import json
from typing import Any

# 固定值字段（§2.2/§5.3）：appendCost=true、o_id 新建为空、图片/多皮重数组为空；
# duo_get/cost 合计恒发 0.00（旧链路只归集应收，四通道发射见 payload.py _emit_fees）
_FIXED_FIELDS: dict[str, str] = {
    "appendCost": "true",
    "o_id": "",
    "img_data": "[]",
    "img_data_id": "[]",
    "multiple_tare": "[]",
    "duo_get[0][duo_get_hj_zj]": "0.00",
    "cost[0][supplier_hj_zj]": "0.00",
}

# 顶层固定键集（§2.2）：全部恒发，有值填值、无值空串（控制器直接索引读取，
# 缺 c_id/note 即拒单）；订单（type 固定 1；c_id/note 恒空）/ 运输 / 门点
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
# type 固定 1（§5.3）；c_id/note 恒发空串（缺键即拒单；来源未定先恒空保过）
_TOP_FIXED_VALUES: dict[str, str] = {"type": "1"}

# data[N] 子键固定集（§2.2）：全部恒发；b_order_num 取实际提单号，其余恒空
# （note 须下沉嵌套条目：顶层已发仍报 Undefined index: note）
_DATA_KEYS: tuple[str, ...] = ("b_order_num", "j", "m", "t", "hh", "mt", "note")

# driver[N] 固定键集（§2.2/§5.1）：无论有无值都发，无值空串
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

# shou 属性键固定集（抓包）：全部费用挂 shou[0] 单条目下，六属性恒发
_SHOU_KEYS: tuple[str, ...] = (
    "money",
    "price_id",
    "price_type",
    "is_profit",
    "dai_dian",
    "note",
)

# 不发送字段（§5.3/§2.2）：user_name/car_name/section_name（走 web key 身份）、
# box_type_text/box_type/顶层 b_date/b_date_pick、pay[]/duo_get[]/cost[] 费用
# 条目（旧链路仅含 shou）、audit_status/b_lock 等——固定键集天然排除，无需过滤


def _put_value(flat: dict[str, str], key: str, value: Any) -> None:
    """null → 空字符串，其余 str()（展平键值统一字符串化）。"""
    flat[key] = "" if value is None else str(value)


def flatten_order(order_data: dict[str, Any]) -> dict[str, str]:
    """order_data（嵌套）→ 顶层展平表单字段（超集键集 + 值填充，§2.2/§5.3）。

    控制器硬读键名（未知键忽略、缺键报错），故按键集发超集；note 除顶层外
    同步下沉全部嵌套条目（顶层已发仍报 Undefined index: note）：
    - 顶层 25 键固定（订单/运输/门点，type=1，c_id/note 恒空）；data[N] 7 子键、
      driver[N] 14 键全部恒发
    - shou 单条目形态：费用挂 shou[0] 下不同费用名键，每名 6 属性键（money 实际
      金额，其余恒空）；有费用时补发通道级 shou[0][note]（缺键 204 拒单）
    - box[N][b_type/box_num] 按实际内容，box[N][note] 恒空；固定值见 _FIXED_FIELDS
    - 不发送字段见上方清单（固定键集天然排除，无需过滤）
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
            # 所有费用挂 shou[0] 单条目下（不同费用名作键）
            for key in _SHOU_KEYS:
                _put_value(flat, f"shou[0][{name}][{key}]", spec.get(key))
            shou_emitted = True
    if shou_emitted:
        # 有费用时通道级 note 恒发（缺键 204 拒单）
        flat["shou[0][note]"] = ""

    return flat


def build_add_work_form(order_data: dict[str, Any]) -> dict[str, str]:
    """组装 AddWork 表单（§5.3）：a="{}"、c="{}"、b=展平字段 JSON 双写
    （键与顶层同内容）+ 顶层超集展平字段。纯函数，live 与生产同一份实现。"""
    flat = flatten_order(order_data)
    return {
        "a": "{}",
        "c": "{}",
        "b": json.dumps(flat, ensure_ascii=False),
        **flat,
    }
