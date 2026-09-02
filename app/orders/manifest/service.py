"""舱单导入编排层：解析 → 箱型白名单 → 预览/创建 → 响应组装。

管线（一文件一票，对齐《舱单导入接口文档》）：
- 解析：parse_manifest → ManifestOrder（家族识别/字段提取/必填缺失标记）；
- 箱型白名单：复用账单导入同一份 `config/box_type_whitelist.yaml` 与
  check_unknown_box_types（用户确认「箱型复用账单录入的」）；文件级拒绝
  对齐账单语义——任一单含白名单外标准码箱型 → 全部未决单 create_result
  标记 unknown_box_type（preview 亦拒绝，不调下游，upstream 204 口径）；
- 箱型整体缺失（v1.7）：解析未提取到箱型 → 文件级拒绝 manifest_box_missing
  （preview 亦拒绝；形态对齐 unknown_box_type；空列表不触发白名单，互斥）；
- 多提单号（v1.8）：全工作簿提取到 ≥2 个不同提单号（strip+大写去重）→
  文件级拒绝 manifest_multi_bl_no（preview 亦拒绝；形态对齐 box_missing；
  同一提单号重复出现、斜杠双号取后段计 1 个、HBL 分单不计）；
- 每箱运价恒 0（build_order_data 内冻结；模版无运价字段，TMS 误设必填）；
- create 模式必填缺失（bl_no/pol）拦截该单 manifest_order_not_ready；
- 重复导入（v1.9）：放开本地去重——不查成功单注册表、不登记，重复上传
  照常提交（重复风险调用方自负；registry 模块暂停生产使用，历史数据保留）；
- preview 零副作用（无下游调用）。

响应：orders[].order_data = 展示口径请求体回显（preview=待提交、create=实际
提交）；summary/upstream 仅 create 模式填充（口径对齐账单导入）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.logging_conf import get_logger

from .client import submit_manifest_async
from .parser import parse_manifest
from .payload import build_order_data, to_submit_payload
from .schema import MANIFEST_REQUIRED, ManifestParseResult

log = get_logger(__name__)


def _sha256(data: bytes) -> str:
    """文件内容 sha256（meta 溯源用）。"""
    return hashlib.sha256(data).hexdigest()


def _mark_box_type_rejection(order) -> bool:
    """箱型白名单文件级校验（复用账单 check_unknown_box_types）。

    任一单含白名单外标准码箱型 → 该单 create_result 标记 unknown_box_type
    （message 对齐账单文案），preview 与 create 统一执行，不调下游。
    返回是否命中（调用方跳过后续提交）。
    """
    from app.orders.bill.box_whitelist import check_unknown_box_types

    unknown = check_unknown_box_types([g.b_type for g in order.box_groups])
    if not unknown:
        return False
    order.create_result = {
        "success": False,
        "sn": None,
        "error": {
            "code": "unknown_box_type",
            "message": f"系统没有此箱型：{'、'.join(unknown)}，请联系客服",
            "description": "箱型不在 TMS 支持清单中，请联系客服",
            "details": {
                "unknown_box_types": unknown,
                # 全场景业务码统一可达：本地拦截等价于该单添加失败，
                # 对齐 TMS「新建全部失败 → 204」口径
                "upstream": {"code": "204", "msg": "添加失败", "data": []},
            },
        },
    }
    return True


def _mark_multi_bl_no_rejection(order, bl_nos: list[str]) -> bool:
    """多提单号文件级拒绝

    全工作簿提取到 ≥2 个不同提单号（strip + 大写规范化去重）→ preview 与
    create 统一拒绝（manifest_multi_bl_no，不调下游），形态对齐 manifest_box_missing
    （create_result 标记 + upstream 204 口径）。同一提单号重复出现（如表单区与
    明细表同号）、托书斜杠双号（取后段计 1 个）不算多票；HBL NO 分单不参与计数。
    返回是否命中（调用方跳过后续提交）。
    """
    unique = {bl.upper() for bl in bl_nos if bl and bl.strip()}
    if len(unique) < 2:
        return False
    order.create_result = {
        "success": False,
        "sn": None,
        "error": {
            "code": "manifest_multi_bl_no",
            "message": "舱单文件包含多个提单号，一文件仅支持一票，请拆分文件后重试",
            "description": "舱单文件包含多个提单号，无法录入，请拆分文件后重试",
            "details": {
                "bl_nos": sorted(unique),
                # 全场景业务码统一可达：本地拦截等价于该单添加失败，
                # 对齐 TMS「新建全部失败 → 204」口径
                "upstream": {"code": "204", "msg": "添加失败", "data": []},
            },
        },
    }
    return True


def _mark_box_missing_rejection(order) -> bool:
    """箱型整体缺失文件级拒绝

    解析未提取到任何箱型（SI 变体 1 无箱型来源、箱型无箱量变体）→ preview 与
    create 统一拒绝（manifest_box_missing，不调下游），形态对齐 unknown_box_type
    （create_result 标记 + upstream 204 口径）。box_groups 为空不触发白名单校验
    （空列表全放行），两者互斥。返回是否命中（调用方跳过后续提交）。
    """
    if order.box_groups:
        return False
    missing = {f: order.missing_reasons.get(f, "原文未找到") for f in ("box_groups",)}
    order.create_result = {
        "success": False,
        "sn": None,
        "error": {
            "code": "manifest_box_missing",
            "message": "舱单未识别到箱型箱量（box_groups），请检查文件后重试",
            "description": "舱单未提取到箱型箱量，无法录入，请检查文件后重试",
            "details": {
                "missing_fields": ["box_groups"],
                "missing_reasons": missing,
                # 全场景业务码统一可达：本地拦截等价于该单添加失败，
                # 对齐 TMS「新建全部失败 → 204」口径
                "upstream": {"code": "204", "msg": "添加失败", "data": []},
            },
        },
    }
    return True


def _mark_not_ready(order) -> None:
    """必填缺失拦截（create 模式；preview 只报告 missing_fields）。"""
    missing = {f: order.missing_reasons.get(f, "原文未找到") for f in MANIFEST_REQUIRED if f in order.missing_fields}
    order.create_result = {
        "success": False,
        "sn": None,
        "error": {
            "code": "manifest_order_not_ready",
            "message": f"舱单必填信息不完整：{'、'.join(missing)}，请补充后重试",
            "description": "舱单必填信息不完整，无法录入，请补充后重试",
            "details": {
                "missing_fields": list(missing),
                "missing_reasons": missing,
                "upstream": {"code": "204", "msg": "添加失败", "data": []},
            },
        },
    }

async def build_manifest_result_async(
    filename: str,
    file_bytes: bytes,
    create_order: bool = False,
    sk: str = "",
) -> ManifestParseResult:
    """build_manifest_result（2026-09 异步化改造后为生产唯一入口）：解析段（openpyxl CPU 密集）入线程池，
    提交段走 submit_manifest_async（模块级绑定，测试可 patch），校验/响应组装
    语义。"""
    import asyncio

    file_sha256 = _sha256(file_bytes)
    out = await asyncio.to_thread(parse_manifest, file_bytes)
    order = out.order

    # 箱型白名单/箱型缺失/多提单号文件级校验（纯内存，preview 亦拒绝；对齐同步版顺序）
    _mark_box_type_rejection(order)
    _mark_box_missing_rejection(order)
    _mark_multi_bl_no_rejection(order, out.bl_nos)

    # order_data 展示口径（每箱运价恒 0，build_order_data 内冻结）
    order.order_data = build_order_data(order)

    if create_order and order.create_result is None:
        if order.missing_fields:
            _mark_not_ready(order)
        else:
            payload = to_submit_payload(build_order_data(order))
            result = await submit_manifest_async(payload, sk)
            order.create_result = result
            order.order_data = payload  # create：order_data 回显实际提交体

    summary = None
    upstream = None
    if create_order:
        result = order.create_result or {}
        is_success = bool(result.get("success"))
        summary = {
            "total": 1,
            "success": 1 if is_success else 0,
            "failed": 0 if is_success else 1,
            "skipped": 0,
            "created": 1 if is_success else 0,
            "success_sns": [result["sn"]] if is_success and result.get("sn") else [],
            "failed_details": (
                []
                if is_success
                else [
                    {
                        "bl_no": order.bl_no,
                        "error_code": (result.get("error") or {}).get("code"),
                        "error_message": (result.get("error") or {}).get("message"),
                    }
                ]
            ),
        }
        if is_success:
            upstream = {
                "code": "200",
                "msg": "成功",
                "data": [result.get("upstream")] if result.get("upstream") else [],
            }
        elif result:
            upstream = {"code": "204", "msg": "添加失败", "data": []}

    return ManifestParseResult(
        file=filename,
        create_order=create_order,
        orders=[order],
        summary=summary,
        upstream=upstream,
        meta={
            "source_sha256": file_sha256,
            "source_bytes": len(file_bytes),
            "parsed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "parser": out.engine,
            "family": out.family,
        },
    )
