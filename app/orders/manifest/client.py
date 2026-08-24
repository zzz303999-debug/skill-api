"""舱单 addBill 提交：POST JSON body + sk 头（契约见接口文档 §2.2）。

- 端点：settings.jxt_manifest_addbill_url（2026-08-19 抓包确认）；
- 鉴权：sk 请求头（调用方登录 TMS 获取，create 模式经请求头透传，不落盘）；
- 响应判定（2026-08-20 live 实证）：成功 = `code` **数字** 200 即已创建
  （服务端通道 `data` 恒 null 但订单真实落库，TMS UI 7 条为证）；
  `data[0].bId` 回执仅浏览器通道可得 → bId 缺席时 sn 记空串、去重靠
  本地注册表；code != 200 → 失败，msg/errorMsg 透传；
- 失败隔离：单失败返回 create_result 不抛异常（调用方按单处理，不中断）；
- 不自动重试（防重复录入）；超时/网络错误按单记 error。
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

_ERROR_DESCRIPTION = "舱单系统拒绝了请求或不可达，请稍后重试"


def _parse_response(response: httpx.Response) -> dict:
    """addBill 响应 → create_result。

    成功：{success, sn(bId), error: None, upstream: data[0] 原始回显}；
    失败：{success: False, error.details.upstream 透传 {code, msg, data}}。
    """
    try:
        body = response.json()
    except ValueError:
        return {
            "success": False,
            "sn": None,
            "error": {
                "code": "order_upstream_error",
                "message": f"addBill returned non-JSON response: {response.text[:200]!r}",
                "description": _ERROR_DESCRIPTION,
                "details": {"status_code": response.status_code},
            },
        }
    if not isinstance(body, dict):
        return {
            "success": False,
            "sn": None,
            "error": {
                "code": "order_upstream_error",
                "message": f"addBill returned unexpected payload: {str(body)[:200]!r}",
                "description": _ERROR_DESCRIPTION,
                "details": {"status_code": response.status_code},
            },
        }
    # code 数字 200（实测）；防御性兼容字符串 "200"（与 AddWork 判定不同勿混用）
    # 2026-08-20 live 实证（TMS UI 7 条为证）：服务端通道 200 + msg "成功" +
    # data null 时**订单已真实创建**——data[0].bId 回执仅浏览器通道可得，
    # 故 code 200 即成功；bId 缺席时 sn 记空串，去重靠本地注册表
    code = body.get("code")
    success = code == 200 or code == "200"
    data = body.get("data")
    record = None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        record = data[0]
    if success:
        return {
            "success": True,
            "sn": str(record.get("bId") or "") if record else "",
            "error": None,
            "upstream": record,
        }
    return {
        "success": False,
        "sn": None,
        "error": {
            "code": "order_upstream_error",
            "message": f"addBill rejected order: {body.get('msg') or body.get('errorMsg') or code}",
            "description": _ERROR_DESCRIPTION,
            "details": {
                "status_code": response.status_code,
                # 全场景业务码统一可达：失败对齐 TMS「新建全部失败 → 204」口径
                "upstream": {"code": str(code), "msg": body.get("msg"), "data": data or []},
                # errorMsg 独立透传（msg 有值时 errorMsg 不并进 message，排查用）
                "upstream_error_msg": body.get("errorMsg"),
            },
        },
    }


def submit_manifest(payload: dict, sk: str) -> dict:
    """POST addBill 一单（JSON body + sk 头，不自动重试）。

    payload 构造见 payload.build_order_data/to_submit_payload；网络异常按单记
    error（不抛），调用方据此隔离失败单。
    """
    try:
        response = httpx.post(
            settings.jxt_manifest_addbill_url,
            json=payload,
            headers={"sk": sk},
            timeout=settings.jxt_timeout_seconds,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning(
            "jxt_manifest_network_error",
            extra={"error_type": exc.__class__.__name__},
        )
        return {
            "success": False,
            "sn": None,
            "error": {
                "code": "order_upstream_error",
                "message": f"addBill network error: {exc.__class__.__name__}",
                "description": _ERROR_DESCRIPTION,
                "details": {"error_type": exc.__class__.__name__},
            },
        }
    return _parse_response(response)
