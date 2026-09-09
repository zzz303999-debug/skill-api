"""费目自举编排（T25）：缺失费目码自动建档 → registry 登记 → 当批回填。

- 懒创建：只建「真实导入中命中且解析为 null」的费目码，不批量预建（防污染价格表）；
- preview 零副作用：create_order=false 只出 planned 计划清单（不发请求/查端点/
  写 registry）；建档失败不阻塞下单（canonical 链降级 skip_report / billrow 直传
  链仅报告，下批重试）；
- 幂等：registry 命中复用、同批同码只调一次；TMS 已存在（204）→ exists_external
  不再重试；registry 不记失败；
- 环境开关：fee_bootstrap.enabled（test=true；prod 默认 false——生产价格表运维
  管控需显式开启）；建档端点复用 master_data endpoints（endpoint_key 引用）；
- 双链（canonical 按 fee.code / BillRow 按 shou 键名收集）共享建档提交段
  _create_price_archives，billrow 链报告带 kind=billrow_named；
- 「其它费」特判：other 码建档额外发 is_other=1（逆推规范 §8.2）——创建后需
  人工在 UI 确认归类。
"""

from __future__ import annotations

from typing import Any

import yaml

from app.core.logging_conf import get_logger

from ..submission.imported_registry import owner_key
from . import fee_price_map as _fee_price_map
from .fee_name_map import _dynamic_fee_code, is_dynamic_code
from .fee_price_map import load_price_map, resolve_price_id
from .fee_registry import DEFAULT_OWNER, get_fee_registry

log = get_logger(__name__)


def _owner_for(sk: str) -> str:
    """费目建档维度键（与 master_data 同口径，2026-09-08 费目隔离拍板）：
    有 sk → owner_key(sk)（sha256 前 16 hex）；无 sk（测试直调/异常路径）→
    DEFAULT_OWNER 槽（preview planned 同槽只读判定，preview 仍零副作用）。"""
    return owner_key(sk) if sk else DEFAULT_OWNER


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
    from ..master_data.config import load_config as load_master_config

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


def _pending_orders(orders: list) -> list:
    """未决单过滤（2026-08-26 防御）：跳过已标记单（去重 skipped/箱型拒绝）——
    被拒单不参与费目自举（避免被拒文件仍触发 AddCarPrice 建档）。"""
    return [o for o in orders if getattr(o, "create_result", None) is None]


def _already_registered(code: str, owner: str) -> bool:
    """幂等跳过判定：当前 owner 槽解析已有 price_id（显式段/registry）或 TMS
    已存在（204，T27b）→ 不重发建档。"""
    return (
        resolve_price_id(code, owner) is not None
        or get_fee_registry().exists_external(code, owner)
    )


def _collect_missing(orders, owner: str) -> list[tuple[str, str]]:
    """本批「解析为 null」的费目码：(code, tms_name)——均限当前 owner 槽。

    判定：解析（显式段+registry）仍无 price_id、非 excluded（import:false 如
    税金不建档）、有 tms_name 可命名；同批同码只收集一次，保持出现顺序。
    命名来源两级：标准码取 YAML 条目 tms_name；模板外动态码（fee_map 按列名
    判定产出，2026-09-08 拍板）YAML 无条目 → 取 fee.note 原名。
    """
    price_map = load_price_map()
    seen: set[str] = set()
    missing: list[tuple[str, str]] = []
    for order in orders:
        for fee in order.fees:
            code = str(fee.code or "")
            if fee.excluded or not code or code in seen:
                continue
            if _already_registered(code, owner):
                continue
            entry = price_map.get(code) or {}
            tms_name = str(entry.get("tms_name") or "").strip()
            if not tms_name and is_dynamic_code(code):
                # 模板外动态码：YAML 无条目 → 以订单原名命名建档（聚合保留原名在
                # note）；标准码/其它费无条目时不落此分支（避免 crane 类码意外复活）
                tms_name = str(fee.note or "").strip()
            if not tms_name:
                log.warning("fee_bootstrap_no_tms_name", extra={"code": code})
                continue  # 无法命名 → 不建档（apply 降级归并其它费保底）
            seen.add(code)
            missing.append((code, tms_name))
    return missing


def _failure_reason(outcome: dict[str, Any]) -> str:
    """建档失败原因摘要（error 三元组 → 可读串；复用 master_data 口径）。"""
    from ..master_data.orchestrator import failure_reason as master_failure_reason

    return master_failure_reason(outcome)


async def run_fee_bootstrap_async(orders, *, create_order: bool, sk: str = "") -> dict[str, Any] | None:
    """canonical 链费目自举（建档提交段 _create_price_archives 共享；2026-09
    异步化改造后为生产唯一入口）。

    **preview 零副作用**：create_order=false 只出 planned 计划清单（不发请求/
    查端点/写 registry）；建档失败不阻塞下单（降级 skip_report，下批重试）。
    """
    config = load_bootstrap_config()
    if not config.get("enabled"):
        return None
    orders = _pending_orders(orders)
    owner = _owner_for(sk)
    missing = _collect_missing(orders, owner)
    if not missing:
        return None
    if not create_order:
        return _report(
            "preview", None,
            planned=[{"code": code, "tms_name": tms_name} for code, tms_name in missing],
            created=[], failed=[], exists_external=[],
        )
    return await _create_price_archives(missing, sk, owner)


def _report(
    mode: str,
    kind: str | None,
    planned: list,
    created: list,
    failed: list,
    exists_external: list,
) -> dict:
    """自举报告结构组装（preview/create/防御共用）；billrow 链带 kind 键。"""
    report: dict = {"enabled": True, "mode": mode}
    if kind:
        report["kind"] = kind
    report.update(
        planned=planned, created=created, failed=failed, exists_external=exists_external
    )
    return report


async def _create_price_archives(
    missing: list[tuple[str, str]], sk: str, owner: str, *, kind: str | None = None
) -> dict[str, Any] | None:
    """建档提交与结果整理（canonical/BillRow 双链共享；kind 仅 billrow 报告带键）。

    结果三态：success → registry 登记 + created；duplicate（204 已存在，T27b）
    → mark_exists_external（不重试自举，按 owner 隔离）；其余 → failed（registry
    不记失败，下批重试）；建档层意外异常不使订单丢失（防御，当批全 failed）。
    """
    url = bootstrap_endpoint()
    if url is None:
        log.warning(
            "fee_bootstrap_endpoint_missing",
            extra={
                "endpoint_key": str(load_bootstrap_config().get("endpoint_key") or ""),
                **({"kind": kind} if kind else {}),
            },
        )
        return None

    from ..master_data.client import create_archives_async
    from ..master_data.config import KIND_PRICE

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
            extra={"error_type": exc.__class__.__name__, **({"kind": kind} if kind else {})},
        )
        return _report(
            "create", kind,
            planned=[],
            created=[],
            failed=[
                {
                    "code": code,
                    "tms_name": tms_name,
                    "reason": f"unexpected error: {exc.__class__.__name__}",
                }
                for code, tms_name in missing
            ],
            exists_external=[],
        )
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    exists_external: list[dict[str, Any]] = []
    for code, tms_name in missing:
        outcome = (results.get(KIND_PRICE) or {}).get(code) or {}
        if outcome.get("success"):
            price_id = int(outcome["archive_id"])
            get_fee_registry().register(code, price_id, tms_name, owner)
            log.info(
                "fee_bootstrap_created",
                extra={"code": code, "price_id": price_id, **({"kind": kind} if kind else {})},
            )
            created.append(
                {"code": code, "tms_name": tms_name, "price_id": price_id}
            )
        elif outcome.get("duplicate"):
            # T27b：费目在当前 owner 的 TMS 价格表已存在（204 拒单）→ 登记
            # exists_external 不再重试自举；price_id 无（无查询接口），费用继续
            # 降级不录入仅对账（按 owner 隔离：A 账号撞名不影响 B）
            get_fee_registry().mark_exists_external(code, tms_name, owner)
            log.info(
                "fee_bootstrap_exists_external",
                # 键名不能用 message（logging LogRecord 保留字，extra 冲突直接
                # KeyError 使 204 已存在场景整请求 500——2026-09-08 实弹发现）
                extra={
                    "code": code,
                    "tms_message": (outcome.get("error") or {}).get("message"),
                    **({"kind": kind} if kind else {}),
                },
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
    return _report(
        "create", kind,
        planned=[], created=created, failed=failed, exists_external=exists_external,
    )


def _filter_missing_names(names, owner: str) -> list[tuple[str, str]]:
    """裸名清单的幂等过滤（建档候选 (code, tms_name)；B2 提取，两链共用；
    均限当前 owner 槽）。"""
    from .fee_name_map import fee_alias_dictionary

    alias = fee_alias_dictionary()
    seen: set[str] = set()
    missing: list[tuple[str, str]] = []
    for raw in names:
        name = str(raw).strip()
        if not name or name in seen:
            continue
        # 字典码与动态码统一幂等判定（同 owner 槽）
        eff_code = alias.get(name) or _dynamic_fee_code(name)
        if _already_registered(eff_code, owner):
            continue
        seen.add(name)
        missing.append((eff_code, name))
    return missing


def _collect_billrow_missing(orders, owner: str) -> list[tuple[str, str]]:
    """BillRow 链（订单费用为直传中文名）建档候选：(code, tms_name)。

    判定：名字 → 字典码（未命中 → 动态码，模板外新名如 加班费），已建档跳过；
    同批同名只收集一次，保持出现顺序。
    """
    names: list[str] = []
    for order in orders:
        for entry in order.order_data.get("shou") or []:
            names.extend(str(name).strip() for name in entry if str(name).strip())
    return _filter_missing_names(names, owner)


async def run_billrow_fee_bootstrap_async(
    orders, *, create_order: bool, sk: str = "", extra_names: list[str] | None = None
) -> dict[str, Any] | None:
    """BillRow 链（jinxin 直传名）模板外费用建档（2026-09-04 拍板；建档提交段共享）。

    背景：jinxin 链费用以中文名直传 AddWork（订单可录），但 TMS「费用管理」只有
    AddCarPrice 建档过的费目——模板外新费目（加班费/报关费等）订单有、费用管理
    无档案 → create 时自动建档同名档案。与 canonical 链差异：建档失败/端点未配
    **不阻塞下单**（直传不依赖 price_id，仅进报告，下批重试）。
    - 候选 = 本批订单 shou 键名中无已建档档案者；extra_names 为外部直传候选
      （保留供直调/未来链路），与 BillRow 候选同名去重（BillRow 链优先保序）；
    - preview 零副作用；幂等：registry 登记复用 / 同批同名只调一次 / TMS 已存在
      （204）→ exists_external。
    """
    config = load_bootstrap_config()
    if not config.get("enabled"):
        return None
    orders = _pending_orders(orders)
    owner = _owner_for(sk)
    missing = _collect_billrow_missing(orders, owner)
    if extra_names:
        have = {name for _, name in missing}
        missing.extend(
            pair for pair in _filter_missing_names(extra_names, owner) if pair[1] not in have
        )
    if not missing:
        return None
    if not create_order:
        return _report(
            "preview", "billrow_named",
            planned=[{"code": code, "tms_name": tms_name} for code, tms_name in missing],
            created=[], failed=[], exists_external=[],
        )
    return await _create_price_archives(missing, sk, owner, kind="billrow_named")
