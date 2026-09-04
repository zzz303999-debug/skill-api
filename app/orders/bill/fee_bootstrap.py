"""费目自举编排（T25）：缺失费目码自动建档 → registry 登记 → 当批回填。

- 懒创建：只建「真实导入中命中且解析为 null」的费目码，不批量预建（防污染价格表）；
- 时机：费用归一完成后、payload 构造前（service._build_fee_reports 挂点）；
- **preview 零副作用**（与阶段三 preview 只读语义一致）：create_order=false 只输出
  「计划创建清单」（planned）不发建档请求；真实导入（create_order=true）才建档；
- 幂等：registry 命中即复用，不重发建档；同批同码只调一次；建档失败 → 当批降级
  skip_report（registry 不记失败，下批重试）；
- 环境开关：fee_bootstrap.enabled 按环境配置（test=true，prod 默认 false——生产
  价格表由运维管控，自举属越权行为需显式开启）；
- 不阻塞：自举任何失败不得影响下单（沿用既有降级语义）；
- 「其它费」特判：other 码建档额外发 is_other=1（逆推规范 §8.2 实证响应含 is_other
  字段，语义待验证）——创建后需人工在 UI 确认归类。
"""

from __future__ import annotations

import hashlib
from typing import Any

import yaml

from app.logging_conf import get_logger

from . import fee_price_map as _fee_price_map
from .fee_price_map import load_price_map, resolve_price_id
from .fee_registry import get_registry

log = get_logger(__name__)

# 配置缺省（enabled 恒 False：未配置/配置错误 → 不自举，走现状降级语义）
_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "endpoint_key": "price_create",
    "create_defaults": {},
}
_CACHE: dict[str, Any] | None = None


def load_bootstrap_config() -> dict[str, Any]:
    """加载费目自举配置（fee_price_map.{env}.yaml 顶级段 fee_bootstrap）。

    缺段/缺文件/YAML 错误 → disabled + warning：自举只是降级路径的增强，
    不 fail fast（映射表本体 fail fast 由 fee_price_map 负责）。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    config = dict(_DEFAULTS)
    path = _fee_price_map.price_map_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning(
            "fee_bootstrap_config_load_failed",
            extra={"path": str(path), "error": str(exc)},
        )
        _CACHE = config
        return config
    section = data.get("fee_bootstrap") or {}
    if isinstance(section, dict):
        if "enabled" in section:
            config["enabled"] = bool(section.get("enabled", False))
        if section.get("endpoint_key"):
            config["endpoint_key"] = str(section["endpoint_key"]).strip()
        config["create_defaults"] = dict(section.get("create_defaults") or {})
    _CACHE = config
    return config


def reload_bootstrap_config() -> dict[str, Any]:
    """重置自举配置缓存（测试用）。"""
    global _CACHE
    _CACHE = None
    return load_bootstrap_config()


def bootstrap_endpoint() -> str | None:
    """自举建档端点：复用 master_data endpoints（endpoint_key 引用键名，不重复配 URL）；
    TODO/空/未配置 → None（不建档，全部走现状降级语义，不 fail fast）。"""
    from .master_data import load_config as load_master_config

    key = str(load_bootstrap_config().get("endpoint_key") or "price_create")
    url = str((load_master_config().get("endpoints") or {}).get(key) or "").strip()
    if not url or url.upper() == "TODO":
        return None
    return url


def build_price_form(code: str, tms_name: str) -> dict[str, str]:
    """费目建档 form：create_defaults 打底 + 代码生成值覆盖（name=tms_name、
    sn={sn_prefix}_{CODE大写}，逆推规范 §8 最小字段集）。

    YAML 布尔归一（checkbox 语义）：True → "on"、False → 省略键（不发送）；
    「其它费」特判：other 码额外发 is_other=1。
    """
    defaults = dict(load_bootstrap_config().get("create_defaults") or {})
    sn_prefix = str(defaults.pop("sn_prefix", "AUTO") or "AUTO")
    form: dict[str, str] = {}
    for key, value in defaults.items():
        text = "on" if value is True else "" if value is False else str(value).strip()
        if text:
            form[str(key)] = text
    form["name"] = str(tms_name).strip()
    form["sn"] = f"{sn_prefix}_{code.upper()}"
    if code == "other":
        form["is_other"] = "1"
    return form


def _collect_missing(orders) -> list[tuple[str, str]]:
    """本批「解析为 null」的费目码：(code, tms_name)。

    判定：YAML+registry 两级解析后仍无 price_id、非 excluded（import:false 如税金
    不建档）、有 tms_name 可命名（来自 fee_price_map YAML）；同批同码只收集一次，
    保持出现顺序。
    """
    price_map = load_price_map()
    registry = get_registry()
    seen: set[str] = set()
    missing: list[tuple[str, str]] = []
    for order in orders:
        for fee in order.fees:
            code = str(fee.code or "")
            if fee.excluded or not code or code in seen:
                continue
            if resolve_price_id(code) is not None:
                continue  # YAML/registry 已有 id（幂等：不重发建档）
            if registry.exists_external(code):
                continue  # T27b：TMS 已存在（204 已存在）→ 不再重试自举
            entry = price_map.get(code) or {}
            tms_name = str(entry.get("tms_name") or "").strip()
            if not tms_name:
                log.warning("fee_bootstrap_no_tms_name", extra={"code": code})
                continue  # 未登记 tms_name 无法命名 → 不建档（降级语义）
            seen.add(code)
            missing.append((code, tms_name))
    return missing


def _failure_reason(outcome: dict[str, Any]) -> str:
    """建档失败原因摘要（error 三元组 → 可读串；复用 master_data 口径）。"""
    from .master_data import _failure_reason as master_failure_reason

    return master_failure_reason(outcome)


def _dynamic_fee_code(name: str) -> str:
    """模板外费目动态码：ASCII 稳定唯一（registry 键 + 建档 sn 用）。

    中文费目名不能直接进 sn（{prefix}_{code} 拼写），拼音不可靠（多音字）；
    取 sha1 前 8 位 hex 作后缀，同名恒同码（幂等），跨批复用 registry。
    """
    return "x" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def _collect_billrow_missing(orders) -> list[tuple[str, str]]:
    """BillRow 链（订单费用为直传中文名）建档候选：(code, tms_name)。

    判定：别名字典（中文→码）命中且已有 price_id（YAML/registry 两级解析）
    → 档案已建档，跳过（幂等）；字典未命中（模板外新名，如 加班费）或命中
    但无 price_id → 建档候选（code=字典码或动态码，tms_name=原名）。
    同批同名只收集一次，保持出现顺序。
    """
    from .fee_map import fee_alias_dictionary

    alias = fee_alias_dictionary()
    registry = get_registry()
    seen: set[str] = set()
    missing: list[tuple[str, str]] = []
    for order in orders:
        for entry in order.order_data.get("shou") or []:
            for name in entry:
                name = str(name).strip()
                if not name or name in seen:
                    continue
                # 字典码与动态码统一幂等判定：YAML/registry 已有 id → 跳过；
                # TMS 已存在（204）→ 跳过不重试（动态码同规则，否则每次重试再撞 204）
                code = alias.get(name)
                eff_code = code or _dynamic_fee_code(name)
                if resolve_price_id(eff_code) is not None:
                    continue  # 档案已建档（幂等：不重发）
                if registry.exists_external(eff_code):
                    continue  # T27b：TMS 已存在（204 已存在）→ 不再重试自举
                seen.add(name)
                missing.append((eff_code, name))
    return missing


def run_billrow_fee_bootstrap(
    orders, *, create_order: bool, sk: str = ""
) -> dict[str, Any] | None:
    """BillRow 链（jinxin 直传名）模板外费用建档（2026-09-04 用户拍板）。

    背景：jinxin 链费用以中文名直传 AddWork，订单侧可录（TMS 不校验档案），
    但 TMS「费用管理」只有 AddCarPrice 建档过的费目——模板外新费目（加班费/报关费/
    查验费等）订单有、费用管理无档案 → 本函数在 create 时自动建档同名档案。

    - 候选 = 本批订单 shou 键名中「无已建档档案」者（模板 6 名已建档 → 跳过）；
    - **preview 零副作用**：create_order=false 只输出 planned 计划清单，不发请求；
    - 建档失败/端点未配：**不阻塞下单**（直传不依赖 price_id），仅进报告，下批重试
      ——与 canonical 链（建档失败降级 skip_report）不同：此处档案是费用管理侧
      补齐，订单费用照常按名直传；
    - 幂等：registry 登记后复用；同批同名只调一次；TMS 已存在（204）→ exists_external。
    """
    config = load_bootstrap_config()
    if not config.get("enabled"):
        return None
    orders = [o for o in orders if getattr(o, "create_result", None) is None]
    missing = _collect_billrow_missing(orders)
    if not missing:
        return None
    if not create_order:
        return {
            "enabled": True,
            "mode": "preview",
            "kind": "billrow_named",
            "planned": [
                {"code": code, "tms_name": tms_name} for code, tms_name in missing
            ],
            "created": [],
            "failed": [],
            "exists_external": [],
        }
    url = bootstrap_endpoint()
    if url is None:
        log.warning(
            "fee_bootstrap_endpoint_missing",
            extra={"endpoint_key": config.get("endpoint_key"), "kind": "billrow_named"},
        )
        return None

    from .master_data import KIND_PRICE
    from .master_data_client import create_archives

    forms = {
        KIND_PRICE: {
            code: build_price_form(code, tms_name) for code, tms_name in missing
        }
    }
    try:
        results = create_archives(forms, sk)
    except Exception as exc:  # 防御：建档层意外异常也不使订单丢失（降级语义）
        log.warning(
            "fee_bootstrap_create_unexpected",
            extra={"error_type": exc.__class__.__name__, "kind": "billrow_named"},
        )
        return {
            "enabled": True,
            "mode": "create",
            "kind": "billrow_named",
            "planned": [],
            "created": [],
            "failed": [
                {
                    "code": code,
                    "tms_name": tms_name,
                    "reason": f"unexpected error: {exc.__class__.__name__}",
                }
                for code, tms_name in missing
            ],
            "exists_external": [],
        }
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    exists_external: list[dict[str, Any]] = []
    for code, tms_name in missing:
        outcome = (results.get(KIND_PRICE) or {}).get(code) or {}
        if outcome.get("success"):
            price_id = int(outcome["archive_id"])
            get_registry().register(code, price_id, tms_name)
            log.info(
                "fee_bootstrap_created",
                extra={"code": code, "price_id": price_id, "kind": "billrow_named"},
            )
            created.append(
                {"code": code, "tms_name": tms_name, "price_id": price_id}
            )
        elif outcome.get("duplicate"):
            get_registry().mark_exists_external(code, tms_name)
            log.info(
                "fee_bootstrap_exists_external",
                extra={"code": code, "message": (outcome.get("error") or {}).get("message")},
            )
            exists_external.append(
                {
                    "code": code,
                    "tms_name": tms_name,
                    "message": (outcome.get("error") or {}).get("message") or "已存在",
                }
            )
        else:
            failed.append(
                {
                    "code": code,
                    "tms_name": tms_name,
                    "reason": _failure_reason(outcome) or "未知错误",
                }
            )
    return {
        "enabled": True,
        "mode": "create",
        "kind": "billrow_named",
        "planned": [],
        "created": created,
        "failed": failed,
        "exists_external": exists_external,
    }


def run_fee_bootstrap(orders, *, create_order: bool, sk: str = "") -> dict[str, Any] | None:
    """自举编排：本批缺失费目码逐码建档（sk 由调用方登录 TMS 后透传）→ 成功
    registry 登记（当批回填由后续 apply_price_map 经 registry 命中完成）；失败进
    报告（当批降级 skip_report 语义不变）。

    **preview 零副作用**：create_order=false 只输出「计划创建清单」（planned）
    不发请求；真实导入（create_order=true）才建档。disabled / 本批无缺失 → None
    （不产生报告段）；create 模式端点未配 → None（不建档，全部走现状降级语义）。

    任何失败不抛断订单流程（建档异常不使费用丢失，降级语义与 T12 一致）。
    """
    config = load_bootstrap_config()
    if not config.get("enabled"):
        return None
    # 防御（2026-08-26）：跳过已标记的单（去重 skipped / 箱型拒绝），
    # 被拒单不参与费目自举（避免被拒文件仍触发 AddCarPrice 建档）
    orders = [o for o in orders if getattr(o, "create_result", None) is None]
    missing = _collect_missing(orders)
    if not missing:
        return None
    if not create_order:
        # preview：只报告计划创建清单，零副作用（不发请求、不查端点、不写 registry）
        return {
            "enabled": True,
            "mode": "preview",
            "planned": [
                {"code": code, "tms_name": tms_name} for code, tms_name in missing
            ],
            "created": [],
            "failed": [],
            "exists_external": [],
        }
    url = bootstrap_endpoint()
    if url is None:
        log.warning(
            "fee_bootstrap_endpoint_missing",
            extra={"endpoint_key": config.get("endpoint_key")},
        )
        return None

    from .master_data import KIND_PRICE
    from .master_data_client import create_archives

    forms = {
        KIND_PRICE: {
            code: build_price_form(code, tms_name) for code, tms_name in missing
        }
    }
    try:
        results = create_archives(forms, sk)
    except Exception as exc:  # 防御：建档层意外异常也不使订单丢失（降级语义）
        log.warning(
            "fee_bootstrap_create_unexpected",
            extra={"error_type": exc.__class__.__name__},
        )
        return {
            "enabled": True,
            "mode": "create",
            "planned": [],
            "created": [],
            "failed": [
                {
                    "code": code,
                    "tms_name": tms_name,
                    "reason": f"unexpected error: {exc.__class__.__name__}",
                }
                for code, tms_name in missing
            ],
            "exists_external": [],
        }
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    exists_external: list[dict[str, Any]] = []
    for code, tms_name in missing:
        outcome = (results.get(KIND_PRICE) or {}).get(code) or {}
        if outcome.get("success"):
            price_id = int(outcome["archive_id"])
            get_registry().register(code, price_id, tms_name)
            log.info(
                "fee_bootstrap_created",
                extra={"code": code, "price_id": price_id},
            )
            created.append(
                {"code": code, "tms_name": tms_name, "price_id": price_id}
            )
        elif outcome.get("duplicate"):
            # T27b：费目在 TMS 价格表已存在（204 已存在拒单）→ 登记 exists_external
            # 不再重试自举；price_id 无（无查询接口），费用继续降级不录入仅对账
            get_registry().mark_exists_external(code, tms_name)
            log.info(
                "fee_bootstrap_exists_external",
                extra={"code": code, "message": (outcome.get("error") or {}).get("message")},
            )
            exists_external.append(
                {
                    "code": code,
                    "tms_name": tms_name,
                    "message": (outcome.get("error") or {}).get("message") or "已存在",
                }
            )
        else:
            failed.append(
                {
                    "code": code,
                    "tms_name": tms_name,
                    "reason": _failure_reason(outcome) or "未知错误",
                }
            )
    return {
        "enabled": True,
        "mode": "create",
        "planned": [],
        "created": created,
        "failed": failed,
        "exists_external": exists_external,
    }
