"""建档调用器（T18）：sk 由调用方登录 TMS 后透传 + 六档案 builder。

- 端点 URL 从配置取（config/master_data.{env}.yaml `endpoints`）；TODO/空 → endpoint_for
  返回 None，编排侧降级为只计数不建档（端点是运维补给，不 fail fast）；
- 每档案一个 builder：只发最小字段集（逆推规范 §14 表）+ defaults 合并，禁止抓包
  测试值（test/11111/章家俊等）进代码/测试——全部走配置 defaults；
- 司机建档日期区间双写（拆分字段 + 区间串）按抓包同发（本阶段无证件日期，恒空）；
- 司机端点 2026-08-14 实证启用：/Car/CarDriver/AddCarDriver（与客户/工厂/车辆同族），
  bailor_title/bailor_id 可空（不发送），响应带主键 data.id；早前 AddDriverGroup 端点
  方案 A（bailor_title 必填 / bailor_id 主键唯一 / 响应无主键）作废；
- 响应解析：code 字符串 "200" 判定；主键按档案类取 client_id/factory_id/bailor_id/
  truck_id/id（司机是 id，建映射表常量）；失败（非 200/超时/解析不到主键）→
  结构化结果，不抛断订单流程。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.logging_conf import get_logger

from ..http_client import post_form, post_form_async, unpack_json
from .master_data import (
    KIND_BAILOR,
    KIND_CLIENT,
    KIND_DRIVER,
    KIND_FACTORY,
    KIND_PRICE,
    KIND_TRUCK,
    MasterDataCandidate,
    defaults_for,
    endpoint_for,
    kind_label,
    sn_for,
)

log = get_logger(__name__)

# 建档响应主键回值键名（逆推规范 §14 表：司机是 `id`，其余按档案类命名）
_PRIMARY_KEY_MAP: dict[str, str] = {
    KIND_CLIENT: "client_id",
    KIND_FACTORY: "factory_id",
    KIND_BAILOR: "bailor_id",
    KIND_TRUCK: "truck_id",
    KIND_DRIVER: "id",
    KIND_PRICE: "price_id",
}


def _duplicate_markers() -> list[str]:
    """T27b 重复语义匹配串（配置化：master_data.duplicate_markers；缺省兜底
    ["已存在"]，不写死单串——TMS 各端点拒单文案可能不同）。"""
    from .master_data import load_config

    markers = (load_config().get("duplicate_markers") or []) or ["已存在"]
    return [str(m).strip() for m in markers if str(m).strip()]


def _is_duplicate_message(message: str) -> bool:
    """建档拒单消息是否命中「已存在」语义（任一标记子串命中）。"""
    return any(marker in message for marker in _duplicate_markers())


def _merge_defaults(kind: str, extra: dict[str, Any]) -> dict[str, str]:
    """默认值合并：defaults.{kind} 打底 + extra 覆盖；仅保留非空字符串值。"""
    merged: dict[str, str] = {}
    for key, value in {**defaults_for(kind), **extra}.items():
        text = str(value).strip() if value is not None else ""
        if text:
            merged[str(key)] = text
    return merged


def _emit(form: dict[str, str], fields: dict[str, Any]) -> None:
    """非空值发射（null/空串省略键；「非必填空值省略键」语义与 payload 一致）。"""
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            form[str(key)] = text


# ---- 六个档案 builder（最小字段集，逆推规范 §14 表） ----


def build_client_form(
    candidate: MasterDataCandidate, rec: dict[str, Any]
) -> dict[str, str]:
    """客户建档最小字段集（§9.3）：client_name/sn/cg_id(+cg_name)/sys_type/su_id/
    data[0][n]+data[0][p]（联系人）；随建价格字段（price_*）一律不提交（防污染）。"""
    form = _merge_defaults(
        KIND_CLIENT,
        {"client_name": candidate.display, "sn": sn_for(KIND_CLIENT, int(rec.get("count") or 0))},
    )
    contact = candidate.order.customer_contact
    phone = candidate.order.contact_phone
    if contact or phone:
        form["data[0][n]"] = str(contact or "").strip()
        form["data[0][p]"] = str(phone or "").strip()
    return form


def build_factory_form(
    candidate: MasterDataCandidate, rec: dict[str, Any], client_id: str
) -> dict[str, str]:
    """工厂建档最小字段集（§13）：前置 client_id/client_name + name/sn/address；
    四级区划（province/city/district/town/adcode）未实证必填性，defaults 未配不发送。"""
    form = _merge_defaults(
        KIND_FACTORY,
        {
            "client_id": client_id,
            "client_name": (
                str(candidate.order.customer_name).strip()
                if candidate.order.customer_name
                else ""
            ),
            "name": candidate.display,
            "sn": sn_for(KIND_FACTORY, int(rec.get("count") or 0)),
            "address": (
                str(candidate.order.load_address).strip()
                if candidate.order.load_address
                else ""
            ),
        },
    )
    return form


def build_bailor_form(
    candidate: MasterDataCandidate, rec: dict[str, Any]
) -> dict[str, str]:
    """委托人建档最小字段集（§10）：bailor_title/sn/name/tel/address。

    本阶段只计数不建档（账单侧无委托人参数字段），builder 留作端点/来源补齐后启用。
    """
    return _merge_defaults(
        KIND_BAILOR,
        {"bailor_title": candidate.display, "sn": sn_for(KIND_BAILOR, int(rec.get("count") or 0))},
    )


def build_truck_form(plate: str, sn: str) -> dict[str, str]:
    """车辆建档最小字段集（§12）：num=车牌 + section_id/remind_id 归属 + 证件/保险可空。"""
    return _merge_defaults(KIND_TRUCK, {"num": plate, "sn": sn})


def build_driver_form(
    candidate: MasterDataCandidate, rec: dict[str, Any], truck_id: str
) -> dict[str, str]:
    """司机建档最小字段集（§11，AddCarDriver 实证）：name/phone/num(车牌)/sn/sinout
    + 关联 truck_id；bailor_title/bailor_id 可空（抓包实证，不发送）；
    日期区间双写（license_valid_date/valid_date）本阶段无证件日期，恒空省略。"""
    form = _merge_defaults(
        KIND_DRIVER,
        {
            "name": str(candidate.display).split("/", 1)[0],
            "sn": sn_for(KIND_DRIVER, int(rec.get("count") or 0)),
            "phone": candidate.phone or "",
            "num": candidate.plate or "",
        },
    )
    # TMS CarDriver.php 硬读 phone 键（缺键 → 500 Undefined index，2026-08-14 live
    # 实证）：恒发键（无手机号时发空串兜底，键存在即不触发 Undefined index）
    form.setdefault("phone", "")
    if truck_id:
        form["truck_id"] = truck_id
    return form


# ---- 建档调用（sk 透传 + 逐条串行；失败结构化，不抛断） ----


def _failure_result(message: str, *, details: dict[str, Any]) -> dict[str, Any]:
    """单条建档失败结果（结构化；调用方进报告，不抛异常）。"""
    return {
        "success": False,
        "archive_id": None,
        "error": {
            "code": "master_data_create_error",
            "message": message,
            "details": details,
        },
    }


def _parse_archive_response(response: httpx.Response, kind: str) -> dict[str, Any]:
    """建档响应判定：code 字符串 "200" → 主键键名取 id（缺失仍失败，不做格式假设）；
    其余 → error 三元组。网络/HTTP 异常由调用方捕获。"""
    if response.status_code >= 400:
        return _failure_result(
            f"{kind_label(kind)} create API returned an HTTP error: {response.status_code}",
            details={
                "status_code": response.status_code,
                "upstream_response": unpack_json(response.text),
            },
        )
    try:
        raw = response.json()
    except ValueError:
        return _failure_result(
            f"{kind_label(kind)} create API returned a non-JSON response",
            details={"body_preview": response.text[:500]},
        )
    if not isinstance(raw, dict):
        return _failure_result(
            f"{kind_label(kind)} create API response is not a JSON object",
            details={"response_type": type(raw).__name__},
        )
    if str(raw.get("code")) != "200":
        log.warning("master_data_create_rejected", extra={"kind": kind, "upstream_code": raw.get("code")})
        # T27b：唯一约束拒单（msg 命中 duplicate_markers，如「客户名已存在」）→
        # 档案已存在于 TMS（多半存量档案）——返回 duplicate 标记，调用方登记
        # exists_external 后不再重试（无查询接口无法取 id，订单继续文本提交）
        msg = str(raw.get("msg") or "")
        if _is_duplicate_message(msg):
            log.info("master_data_duplicate_external", extra={"kind": kind, "upstream_message": msg})
            return {
                "success": False,
                "archive_id": None,
                "duplicate": True,
                "error": {
                    "code": "master_data_duplicate",
                    "upstream_message": msg,
                    "details": {
                        "upstream_code": raw.get("code"),
                        "upstream_message": msg,
                        "upstream_response": unpack_json(raw),
                    },
                },
            }
        return _failure_result(
            f"{kind_label(kind)} create API rejected the request: {msg}",
            details={
                "upstream_code": raw.get("code"),
                "upstream_message": raw.get("msg"),
                "upstream_response": unpack_json(raw),
            },
        )
    data = raw.get("data")
    archive_id = data.get(_PRIMARY_KEY_MAP[kind]) if isinstance(data, dict) else None
    if archive_id is None:
        # TMS 成功但无主键回值（2026-08-14 live 实证：AddCarFactory 返回
        # {"code":200,"msg":"添加成功","data":[]}，重复提交仍成功——每次重试都会
        # 再建一条档案）：档案已创建但无查询接口无法取 id → 返回 no_id_created
        # 标记，调用方登记 exists_external 终态不再重试（避免重复建档）
        log.warning(
            "master_data_create_no_primary_key",
            extra={
                "kind": kind,
                "response_keys": sorted(data) if isinstance(data, (dict, list)) else [],
            },
        )
        return {
            "success": False,
            "archive_id": None,
            "no_id_created": True,
            "error": {
                "code": "master_data_no_primary_key",
                "message": "已添加但响应未返回主键",
                "details": {
                    "primary_key": _PRIMARY_KEY_MAP[kind],
                    "upstream_response": unpack_json(raw),
                },
            },
        }
    log.info("master_data_create_ok", extra={"kind": kind, "archive_id": str(archive_id)})
    return {"success": True, "archive_id": str(archive_id), "error": None}


def create_archives(
    forms_by_kind: dict[str, dict[str, dict[str, str]]], sk: str
) -> dict[str, dict[str, dict[str, Any]]]:
    """逐档案类逐条串行建档（sk 由调用方透传）；返回 {kind: {key: result}}。

    单条失败不影响后续；任何情况不自动重试（防重复建档）。
    """
    if not forms_by_kind:
        return {}

    results: dict[str, dict[str, dict[str, Any]]] = {}
    for kind, forms in forms_by_kind.items():
        url = endpoint_for(kind)
        per_key: dict[str, dict[str, Any]] = {}
        for key, form in forms.items():
            if url is None:
                per_key[key] = _failure_result(
                    "endpoint TODO (config/master_data.{env}.yaml endpoints)",
                    details={"kind": kind},
                )
                continue
            try:
                response = post_form(
                    url,
                    form,
                    name=f"archive-{kind}",
                    headers={"sk": sk},
                    timeout=settings.jxt_timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                log.warning(
                    "master_data_create_network_error",
                    extra={"kind": kind, "error_type": exc.__class__.__name__},
                )
                per_key[key] = _failure_result(
                    f"{kind_label(kind)} create API network error: {exc.__class__.__name__}",
                    details={"error_type": exc.__class__.__name__},
                )
                continue
            per_key[key] = _parse_archive_response(response, kind)
        results[kind] = per_key
    return results


async def create_archives_async(
    forms_by_kind: dict[str, dict[str, dict[str, str]]], sk: str
) -> dict[str, dict[str, dict[str, Any]]]:
    """create_archives 的异步版：网络段走 post_form_async，其余逻辑逐行一致。

    档案间存在依赖（客户 → 工厂/司机），保持逐条串行与同步版一致；
    单条失败不影响后续；任何情况不自动重试（防重复建档）。
    """
    if not forms_by_kind:
        return {}

    results: dict[str, dict[str, dict[str, Any]]] = {}
    for kind, forms in forms_by_kind.items():
        url = endpoint_for(kind)
        per_key: dict[str, dict[str, Any]] = {}
        for key, form in forms.items():
            if url is None:
                per_key[key] = _failure_result(
                    "endpoint TODO (config/master_data.{env}.yaml endpoints)",
                    details={"kind": kind},
                )
                continue
            try:
                response = await post_form_async(
                    url,
                    form,
                    name=f"archive-{kind}",
                    headers={"sk": sk},
                    timeout=settings.jxt_timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                log.warning(
                    "master_data_create_network_error",
                    extra={"kind": kind, "error_type": exc.__class__.__name__},
                )
                per_key[key] = _failure_result(
                    f"{kind_label(kind)} create API network error: {exc.__class__.__name__}",
                    details={"error_type": exc.__class__.__name__},
                )
                continue
            per_key[key] = _parse_archive_response(response, kind)
        results[kind] = per_key
    return results
