"""账单导入文件级校验（2026-09-09 P1 自 service.py 下沉）：箱型白名单连坐 +
提单号缺失连坐。

两条校验共享同一文件级语义（preview 与 create 统一执行）：任一未决单触发 →
全部未决单拒绝（一单不录、不调下游）；已标记（skipped）的单不动（历史成功单
必然合法）。本地拦截等价于该单添加失败，details.upstream 对齐 TMS「新建全部
失败 → 204」口径（全场景业务码统一可达）。
"""

from __future__ import annotations

MISSING_BL_NO_MSG = "提单号为必填项；文件存在提单号缺失行时整批不录入，请补全提单号后重新导入"


def reject_unknown_box_types(orders: list) -> bool:
    """文件级箱型白名单校验（2026-08-18 用户拍板）：任一单含标准代码形态且不在
    白名单的箱型 → 全部未决单拒绝（unknown_box_type，不调下游），返回 True。

    preview 与 create 统一执行（preview 也拒）；已标记 skipped 的单不动
    （历史成功单必然合法）。单级 message 报该单自己的非法箱型，无非法箱型的
    单报文件级清单；details.unknown_box_types 同 message 口径。
    """
    from app.core.box_whitelist import check_unknown_box_types

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
                    "upstream": {"code": "204", "msg": "添加失败", "data": []},
                },
            },
        }
    return True


def reject_missing_bl_no(orders: list) -> bool:
    """文件级提单号缺失校验（2026-09-04 用户拍板，语义对齐箱型连坐）：任一未决单
    提单号缺失 → 全部未决单拒绝（missing_bl_no，一单不录，不调下游），返回 True。

    preview 与 create 统一执行（preview 也拒，msg 提示）；已标记（skipped）的单
    不动。拦截文案统一（不分缺号行/连坐行）；details 仅报缺失行数（2026-09-04
    精简：不收集行号——双表示（orders/canonical）下行号源不一致，且前端按 msg
    排查即可）。
    """
    from .submission.imported_registry import normalize

    missing_idx: list[int] = []
    for idx, order in enumerate(orders):
        if order.create_result is not None:
            continue  # 已标记（skipped）的单不动
        bl = normalize(
            getattr(order, "bl_no", None) or getattr(order, "order_num1", None)
        )
        if not bl:
            missing_idx.append(idx)
    if not missing_idx:
        return False
    for order in orders:
        if order.create_result is not None:
            continue
        order.create_result = {
            "success": False,
            "skipped": False,
            "sn": None,
            "error": {
                "code": "missing_bl_no",
                "message": MISSING_BL_NO_MSG,
                "description": MISSING_BL_NO_MSG,
                "details": {
                    "missing_bl_no_count": len(missing_idx),
                    # 全场景业务码统一可达（§3.7/既有规范）：本地拦截等价于该单
                    # 添加失败，对齐 TMS「新建全部失败 → 204」口径
                    "upstream": {"code": "204", "msg": "添加失败", "data": []},
                },
            },
        }
    return True
