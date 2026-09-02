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

async def run_fee_bootstrap_async(orders, *, create_order: bool, sk: str = "") -> dict[str, Any] | None:
    """run_fee_bootstrap（2026-09 异步化改造后为生产唯一入口）：建档段走 create_archives_async，其余逻辑逐行一致。

    **preview 零副作用**、registry 登记与降级语义不变；
    建档异常不使订单丢失（防御分支同步保留）。
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
    from .master_data_client import create_archives_async

    forms = {
        KIND_PRICE: {
            code: build_price_form(code, tms_name) for code, tms_name in missing
        }
    }
    try:
        results = await create_archives_async(forms, sk)
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
