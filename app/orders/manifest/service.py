"""舱单导入编排层：解析 → 箱型白名单 → 预览/创建 → 响应组装。

管线（一文件一票，对齐《舱单导入接口文档》）：
- 解析：parse_manifest → ManifestOrder（家族识别/字段提取/必填缺失标记）；
- 箱型白名单：复用账单导入同一份 `config/box_type_whitelist.yaml` 与
  check_unknown_box_types（用户确认「箱型复用账单录入的」）；文件级拒绝
  对齐账单语义——任一单含白名单外标准码箱型 → 全部未决单 create_result
  标记 unknown_box_type（preview 亦拒绝，不调下游，upstream 204 口径）；
- 每箱运价恒 0（build_order_data 内冻结；模版无运价字段，TMS 误设必填）；
- create 模式必填缺失（MANIFEST_REQUIRED 任意项）拦截该单
  manifest_order_not_ready；
- 去重：per-bl_no 锁包住「查重→提交→登记」临界区；成功单登记注册表，
  重导命中 skipped；失败单不登记；
- preview 零副作用（不触达注册表读写、无下游调用）。

响应：orders[].order_data = 展示口径请求体回显（preview=待提交、create=实际
提交）；summary/upstream 仅 create 模式填充（口径对齐账单导入）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.logging_conf import get_logger

from .client import submit_manifest
from .parser import parse_manifest
from .payload import build_order_data, to_submit_payload
from .registry import get_manifest_registry, lock_for
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


def _mark_skipped(order, sn: str) -> None:
    """去重命中（已成功创建过）：success + skipped 标记。"""
    order.create_result = {"success": True, "skipped": True, "sn": sn, "error": None}


def _create_one(order, sk: str, file_sha256: str, force: bool = False) -> None:
    """单舱单创建管线：必填拦截 → 去重查 → 提交 → 登记（per-bl_no 锁临界区）。

    已在解析/白名单阶段标记 create_result 的单（箱型拒绝）直接跳过提交。
    force=True 跳过去重查重（本地注册表不知晓 TMS 侧删除：TMS 删单后重录
    场景由调用方显式强制，重复风险自负）。
    """
    if order.create_result is not None:
        return
    if order.missing_fields:
        _mark_not_ready(order)
        return
    with lock_for(order.bl_no):
        if not force:
            rec = get_manifest_registry().lookup(order.bl_no)
            if rec:
                _mark_skipped(order, str(rec.get("sn") or ""))
                return
        payload = to_submit_payload(build_order_data(order))
        result = submit_manifest(payload, sk)
        order.create_result = result
        order.order_data = payload  # create：order_data 回显实际提交体
        if result.get("success") and not result.get("skipped"):
            get_manifest_registry().register(
                order.bl_no, sn=result.get("sn"), source_sha256=file_sha256
            )


def build_manifest_result(
    filename: str,
    file_bytes: bytes,
    create_order: bool = False,
    sk: str = "",
    force: bool = False,
) -> ManifestParseResult:
    """编排入口：解析 → 白名单 → preview/create → 响应组装（force 见 _create_one）。"""
    file_sha256 = _sha256(file_bytes)
    out = parse_manifest(file_bytes)
    order = out.order

    # 箱型白名单文件级校验（preview 亦拒绝；对齐账单语义）
    _mark_box_type_rejection(order)

    # order_data 展示口径（每箱运价恒 0，build_order_data 内冻结）
    order.order_data = build_order_data(order)

    if create_order:
        _create_one(order, sk, file_sha256, force=force)

    summary = None
    upstream = None
    if create_order:
        result = order.create_result or {}
        # 账单导入口径：success_sns 含全部 success=True 的单（含 skipped，
        # 便于调用方在 409 时看到已创建回执）；created 仅计非 skipped 新建
        is_success = bool(result.get("success"))
        is_created = is_success and not result.get("skipped")
        summary = {
            "total": 1,
            "success": 1 if is_success else 0,
            "failed": 0 if is_success else 1,
            "skipped": 1 if result.get("skipped") else 0,
            "created": 1 if is_created else 0,
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
        # 上游回显：确有新建成功单 → code 200 + data[0] 回显；全失败 → 204；
        # 全部 skipped（无新建动作）保持 None（路由层转 409 duplicate_manifest）
        if is_created:
            upstream = {
                "code": "200",
                "msg": "成功",
                "data": [result.get("upstream")] if result.get("upstream") else [],
            }
        elif result and not result.get("skipped"):
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
