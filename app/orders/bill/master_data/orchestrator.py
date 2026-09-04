"""阶段三：基础资料阈值建档（T17-T21）——配置/归一键/候选收集/阈值编排。

口径（2026-08-14 用户拍板，见 docs/竞品/阶段三-基础资料阈值录入设计.md）：
- N 默认 5（config/master_data.{env}.yaml `master_data.threshold`），不进代码；
- 计数键：客户=归一名；工厂=「名+地址」复合键；司机+车辆=「司机名+车牌」组合键；
- 计数范围：跨全部模板家族全局累计（master_data_store 单文件持久化）；
- 未达阈值：订单照常带文本提交，订单 unmapped_note 标注「未建档(x/N)」，不阻塞；
- 达阈值：当批建档、当批回填（依赖序：客户 → 工厂；车辆 → 司机）；
- 同批去重：按单计（一单一次），但同批内建档调用每键只发一次（成功失败均不重试）；
- 建档失败：保持计数、下批重试、进报告；任何建档异常不使订单丢失；
- 任一档案类 endpoint 为 TODO/空 → 该档案类降级为只计数不建档（端点是运维补给）；
- preview（create_order=false）只读探测：不计数、不建档，报告展示当前计数状态。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.config import settings
from app.core.logging_conf import get_logger

from ..schema import CanonicalOrder
from ..submission.imported_registry import owner_key
from .store import DEFAULT_OWNER, get_master_data_store

log = get_logger(__name__)

# 档案类常量（代码零竞品名零 TMS 值硬编码；分组/部门等默认值全走配置）
KIND_CLIENT = "client"      # 客户
KIND_FACTORY = "factory"    # 工厂地址
KIND_BAILOR = "bailor"      # 委托人（本阶段只计数不建档，留配置位）
KIND_TRUCK = "truck"        # 车辆（按车牌登记，随司机组合键触发建档）
KIND_DRIVER = "driver"      # 司机（组合键「司机名+车牌」）
KIND_PRICE = "price"        # 价格费目（建档走 fee_bootstrap 自举管线 T24/T25，不在此编排）

# 建档依赖序（逆推规范 §14）：客户 → 工厂；车辆 → 司机；委托人/费目独立
ARCHIVE_ORDER: tuple[str, ...] = (
    KIND_CLIENT,
    KIND_FACTORY,
    KIND_TRUCK,
    KIND_DRIVER,
    KIND_BAILOR,
)

# 计数档案类（collect_candidates 会收集的候选类型）
COUNT_KINDS: tuple[str, ...] = (KIND_CLIENT, KIND_FACTORY, KIND_DRIVER)

# 报告未达阈值 TOP 清单条数上限
PENDING_TOP_N = 10

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "config"
# 按环境解析：config/master_data.{env}.yaml（APP_ENV 选择，与 fee_price_map 同模式）。
# 双份随镜像分发，环境切换零 Git 改动——本地联调永远 test，生产永远 prod。
_CONFIG_PATH = _CONFIG_DIR / f"master_data.{settings.env}.yaml"

# 空白归一：全部空白（含全角空格/连续空白）一律删除——任何空白差异都不产生
# 新计数键（防「锦煦 」/「锦　煦」/「锦 煦」算两个；宁合并不拆分）
_WHITESPACE_RE = re.compile(r"\s+")
_FULL_WIDTH_RE = re.compile(r"[\uFF01-\uFF5E]")

# 档案类 → 中文展示名（报告/标注用；通用业务语义，非 TMS 内部值）
_KIND_LABELS: dict[str, str] = {
    KIND_CLIENT: "客户",
    KIND_FACTORY: "工厂",
    KIND_BAILOR: "委托人",
    KIND_TRUCK: "车辆",
    KIND_DRIVER: "司机",
    KIND_PRICE: "费目",
}


def _to_half_width(text: str) -> str:
    """全角 → 半角（全角字母/数字/符号；全角空格单独处理）。"""

    def _sub(ch: str) -> str:
        code = ord(ch)
        return chr(code - 0xFEE0) if 0xFF01 <= code <= 0xFF5E else ch

    return "".join(_sub(ch) for ch in text)


def normalize_key(text: str) -> str:
    """归一名：全角→半角 + 删除全部空白（计数键共用口径；见 _WHITESPACE_RE 注释）。"""
    if not text:
        return ""
    return _WHITESPACE_RE.sub("", _to_half_width(str(text))).strip()


def plate_key(plate: str | None) -> str:
    """车牌归一：全大写 + 去空白（T17 明确：车牌额外做全大写+去空格归一）。"""
    if not plate:
        return ""
    return normalize_key(str(plate)).upper()


def client_key(name: str | None) -> str:
    """客户计数键：归一名。"""
    return normalize_key(name)


def factory_key(name: str | None, address: str | None) -> str:
    """工厂计数键：「名+地址」复合键（口径 1；地址缺失时退化为按名）。"""
    return f"{normalize_key(name)}|{normalize_key(address)}"


def driver_key(name: str | None, plate: str | None) -> str:
    """司机+车辆计数键：「司机名+车牌」组合键（口径 2——同人换车/同车换人
    算不同档案；车牌缺失时退化为按司机名）。"""
    return f"{normalize_key(name)}|{plate_key(plate)}"


@dataclass
class MasterDataCandidate:
    """一个计数候选（一单一候选，按单计）。"""

    kind: str                                  # 档案类（client/factory/driver）
    order: CanonicalOrder                      # 来源订单（回填/标注用）
    key: str                                   # 计数键（归一键）
    display: str                               # 展示名（报告/标注用）
    phone: str | None = None                   # 司机手机（建档用）
    plate: str | None = None                   # 车牌原文（建档用）
    client_key: str | None = None              # 工厂候选：所属客户归一键（依赖前置）

    def __hash__(self) -> int:
        return hash((self.kind, self.key))


def collect_candidates(orders: list[CanonicalOrder]) -> list[MasterDataCandidate]:
    """从本批订单收集基础资料候选（聚合后、payload 构造前）。

    - 客户：customer_name（归一名）；
    - 工厂：door_point 名 + load_address 地址（复合键；名缺失不构成候选）；
    - 司机+车辆：driver_name + plate_no（组合键；司机名缺失不构成候选）。
    已标记的单（create_result 非 None：去重 skipped / 箱型拒绝等）不构成候选
    （2026-08-26 防御：即使调用方误传被拒单也不建档）。
    """
    candidates: list[MasterDataCandidate] = []
    for order in orders:
        if getattr(order, "create_result", None) is not None:
            continue  # 已标记（skipped/被拒）的单不建档
        if order.customer_name:
            candidates.append(
                MasterDataCandidate(
                    kind=KIND_CLIENT,
                    order=order,
                    key=client_key(order.customer_name),
                    display=str(order.customer_name).strip(),
                )
            )
        if order.door_point:
            candidates.append(
                MasterDataCandidate(
                    kind=KIND_FACTORY,
                    order=order,
                    key=factory_key(order.door_point, order.load_address),
                    display=str(order.door_point).strip(),
                    client_key=(
                        client_key(order.customer_name) if order.customer_name else None
                    ),
                )
            )
        if order.driver_name:
            plate = str(order.plate_no).strip() if order.plate_no else None
            display = str(order.driver_name).strip()
            if plate:
                display = f"{display}/{plate}"
            candidates.append(
                MasterDataCandidate(
                    kind=KIND_DRIVER,
                    order=order,
                    key=driver_key(order.driver_name, plate),
                    display=display,
                    phone=(
                        str(order.driver_phone).strip() if order.driver_phone else None
                    ),
                    plate=plate,
                )
            )
    return candidates


def kind_label(kind: str) -> str:
    """档案类 → 中文展示名（报告/标注）。"""
    return _KIND_LABELS.get(kind, kind)


# ---- 配置加载（config/master_data.{env}.yaml；缺文件 → 全局禁用 + warning，不 fail fast） ----

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "threshold": 5,
    "sn_prefix": {},
    "endpoints": {},
    "defaults": {},
    "duplicate_markers": ["已存在"],
}
_CACHE: dict[str, Any] | None = None


def load_config() -> dict[str, Any]:
    """加载主数据配置（模块缓存；测试可用 reload_config 重置）。

    缺文件/YAML 错误 → 全局禁用（enabled: false）+ warning：阶段三是渐进式
    增强，配置缺失不应阻塞既有导入（与 fee_price_map 的 fail fast 语义不同）。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    config = dict(_DEFAULTS)
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("master_data_config_missing", extra={"path": str(_CONFIG_PATH), "error": str(exc)})
        config["enabled"] = False
        _CACHE = config
        return config
    section = data.get("master_data") or {}
    if isinstance(section, dict):
        for key in _DEFAULTS:
            if key in section:
                config[key] = section[key]
    if not isinstance(config.get("threshold"), int) or config["threshold"] < 1:
        log.warning("master_data_config_invalid_threshold", extra={"threshold": config.get("threshold")})
        config["enabled"] = False
    _CACHE = config
    return config


def reload_config() -> dict[str, Any]:
    """重置配置缓存（测试用）。"""
    global _CACHE
    _CACHE = None
    return load_config()


def endpoint_for(kind: str) -> str | None:
    """档案类 → 建档端点 URL；TODO/空/未配置 → None（该档案类降级只计数不建档）。"""
    url = str((load_config().get("endpoints") or {}).get(f"{kind}_create") or "").strip()
    if not url or url.upper() == "TODO":
        return None
    return url


def sn_for(kind: str, count: int) -> str:
    """建档编码：{prefix}{5 位序号}（序号取该键累计计数；防存量撞名避让位）。"""
    prefix = str((load_config().get("sn_prefix") or {}).get(kind) or kind[:3].upper())
    return f"{prefix}{int(count):05d}"


def defaults_for(kind: str) -> dict[str, Any]:
    """档案类默认值（defaults 段；缺省空字典——未配键不发送）。"""
    section = load_config().get("defaults") or {}
    return dict(section.get(kind) or {})


# ---- 阈值编排（service 管线挂点入口） ----

def _owner_for(sk: str, create_order: bool) -> str | None:
    """建档维度键（2026-09 统一按 sk 判断，与去重注册表同口径）：
    create 有 sk → owner_key(sk)；无 sk（测试直调）→ DEFAULT_OWNER 槽；
    preview 无 sk → None（只读探测无归属：不标注、不列 pending）。"""
    if sk:
        return owner_key(sk)
    return DEFAULT_OWNER if create_order else None


def _failure_reason(outcome: dict[str, Any]) -> str:
    """建档失败原因摘要（error 三元组 → 可读串）。"""
    error = outcome.get("error") or {}
    if not isinstance(error, dict):
        return str(error)
    message = error.get("message") or ""
    code = error.get("code") or ""
    parts = [p for p in (code, message) if p]
    return "：".join(parts) or "未知错误"


def _unique_by_key(
    candidates: list[MasterDataCandidate], kind: str
) -> list[MasterDataCandidate]:
    """同档案类按计数键去重（建档调用每键一次；返回首个候选作代表）。"""
    seen: set[str] = set()
    unique: list[MasterDataCandidate] = []
    for candidate in candidates:
        if candidate.kind != kind or candidate.key in seen:
            continue
        seen.add(candidate.key)
        unique.append(candidate)
    return unique


def _client_record(candidate: MasterDataCandidate, store, owner: str):
    """工厂候选的所属客户记录（同单客户归一键查登记；无记录 → None）。

    按 owner 隔离查询：建档维度与计数一致，不同用户互不串扰。
    返回记录含终态：archive_id（本侧建档）/ exists_external（TMS 已存在）。"""
    if not candidate.client_key:
        return None
    rec = store.get(KIND_CLIENT, candidate.client_key, owner)
    return rec if rec else None


def _client_finalized(candidate: MasterDataCandidate, store, owner: str) -> bool:
    """工厂候选的所属客户是否已达终态：本侧建档（archive_id）/ TMS 已存在
    （exists_external）。仅计数（从未建档成功）不算就绪。"""
    client_rec = _client_record(candidate, store, owner)
    return bool(
        client_rec
        and (client_rec.get("archive_id") or client_rec.get("exists_external"))
    )


def _factory_dependency_candidate(
    candidate: MasterDataCandidate,
    candidates: list[MasterDataCandidate],
    store,
    owner: str,
) -> MasterDataCandidate:
    """工厂依赖委托（2026-09-03 止血修复）：同键任一所属客户就绪即可建档。

    工厂键按「名+地址」聚合（地址缺失退化按名）→ 跨客户同名门点合并一键；
    若仅以代表候选（首单）的客户判依赖，该客户永不达阈值（低频）时整键每批
    被依赖前置拦截、永不建档（生产「工厂宜兴 764/5」同构案例）。委托：代表
    未就绪时在同键候选中取首个所属客户达终态的候选建档（client_id 挂该客户）；
    全部未就绪回退代表（failed 缓建，下批重试——与既有语义一致）。"""
    if _client_finalized(candidate, store, owner):
        return candidate
    for alt in candidates:
        if alt.kind != KIND_FACTORY or alt.key != candidate.key:
            continue
        if _client_finalized(alt, store, owner):
            return alt
    return candidate


def _annotate_pending(
    candidates: list[MasterDataCandidate], store, threshold: int, owner: str
) -> None:
    """订单标注：未建档（有计数、未达阈值）→ unmapped_note 追加「未建档(x/N)」；
    T27b：exists_external（TMS 已存在）不标注（档案已在，仅无 id 可回填）。
    按 owner 隔离：不同 sk 各自状态（preview 无 sk 不调用本函数）。"""
    for candidate in candidates:
        rec = store.get(candidate.kind, candidate.key, owner)
        if not rec or rec.get("archive_id") or rec.get("exists_external"):
            continue
        count = int(rec.get("count") or 0)
        note = f"{kind_label(candidate.kind)}「{candidate.display}」未建档({count}/{threshold})"
        existing = candidate.order.unmapped_note or ""
        if note not in existing:
            candidate.order.unmapped_note = "；".join(p for p in (existing, note) if p)


def _pending_top(store, threshold: int, owner: str) -> list[dict[str, Any]]:
    """未达阈值 TOP 清单（count 降序，上限 PENDING_TOP_N；预览与创建共用；
    T27b：exists_external 已存在外部，不列入 pending）。

    按 owner 过滤：只列当前 sk 维度记录（owner 化，2026-09）。"""
    rows: list[dict[str, Any]] = []
    for kind, entries in store.snapshot().items():
        for key, owners in entries.items():
            rec = owners.get(owner)
            if not rec:
                continue
            count = int(rec.get("count") or 0)
            if count <= 0 or count >= threshold or rec.get("archive_id") or rec.get("exists_external"):
                continue
            rows.append({"kind": kind, "key": key, "count": count, "threshold": threshold})
    rows.sort(key=lambda r: (-r["count"], r["kind"], r["key"]))
    return rows[:PENDING_TOP_N]


# ---- 异步实现（2026-09 异步化改造后为生产唯一入口；网络段走 create_archives_async）----


async def _create_one_async(
    kind: str,
    candidate: MasterDataCandidate,
    rec: dict[str, Any],
    store,
    create_archives_fn,
    attempted: set[tuple[str, str]],
    archived: dict[tuple[str, str], dict[str, Any]],
    failed: list[dict[str, Any]],
    exists_external: list[dict[str, Any]] | None = None,
    sk: str = "",
    *,
    owner: str | None = None,
) -> dict[str, Any] | None:
    """建档一次（依赖前置检查 + 建档调用 + 结果登记）；失败进 failed 不抛断。

    T27b：响应带 duplicate 标记（TMS 已存在拒单）→ store 登记 exists_external
    （不再重试，archive_id 保持 null），进 exists_external 报告段（与 failed 区分）。
    2026-09 异步化改造后为生产唯一入口：建档调用走 await create_archives_fn
    （async 版）；依赖前置/终态登记/报告语义逐行一致（司机前置建车同样 await）。
    """
    forms: dict[str, dict[str, str]] = {}
    no_client_id = False  # 工厂建档无客户 id（客户已存在但无查询接口），失败需登记终态
    if kind == KIND_CLIENT:
        from .client import build_client_form

        forms[kind] = {candidate.key: build_client_form(candidate, rec)}
    elif kind == KIND_FACTORY:
        from .client import build_factory_form

        client_rec = _client_record(candidate, store, owner)
        # 前置满足 = 所属客户已达终态（本侧建档 archive_id / TMS 已存在
        # exists_external）；仅计数（从未建档成功）→ 依赖未就绪，工厂缓建
        # （计数保留下批重试）
        if not client_rec or not (
            client_rec.get("archive_id") or client_rec.get("exists_external")
        ):
            failed.append(
                {
                    "kind": kind,
                    "key": candidate.key,
                    "display": candidate.display,
                    "reason": "所属客户未建档（依赖前置：客户 → 工厂）",
                }
            )
            return None
        # 2026-09-03 修复：客户 exists_external（TMS 已存在、无本地 id）不再本地
        # 拦截——工厂照常尝试（client_id 空值省略键，仅带 client_name；TMS 是否
        # 接受由响应登记：成功→archive；已存在→exists_external；其余拒绝→
        # skip_archive 防每批重发，见下）
        client_archive_id = str(client_rec.get("archive_id") or "")
        forms[kind] = {candidate.key: build_factory_form(candidate, rec, client_archive_id)}
        no_client_id = not client_archive_id
    elif kind == KIND_DRIVER:
        from .client import build_driver_form, build_truck_form

        if not candidate.plate:
            # TMS AddCarDriver 必填 num（车牌），无车牌司机永久无法建档（2026-08-14
            # live 实证拒单「请重新选择车牌」）→ 登记 skip_archive 终态不再重试
            # （避免每批都发注定被拒的请求）；订单保留「未建档」标注，计数照常
            # （后续订单带车牌 → 新计数键 → 恢复正常建档）
            store.mark_skip_archive(kind, candidate.key, owner)
            failed.append(
                {
                    "kind": kind,
                    "key": candidate.key,
                    "display": candidate.display,
                    "reason": "司机无车牌（TMS AddCarDriver 必填 num），无法建档",
                }
            )
            return None
        if not candidate.phone:
            # TMS CarDriver.php 校验 phone 必填（缺键 500 / 空串 no: phone，2026-08-14
            # live 实证）——无手机号司机同样永久无法建档 → skip_archive 终态
            store.mark_skip_archive(kind, candidate.key, owner)
            failed.append(
                {
                    "kind": kind,
                    "key": candidate.key,
                    "display": candidate.display,
                    "reason": "司机无手机号（TMS AddCarDriver 必填 phone），无法建档",
                }
            )
            return None
        # 依赖序：车辆 → 司机（同车牌建过不再建；建车失败不阻塞建司机——truck_id 可空）
        truck_archive_id = ""
        if candidate.plate:
            plate_key_ = plate_key(candidate.plate)
            truck_rec = store.get(KIND_TRUCK, plate_key_, owner)
            if truck_rec and truck_rec.get("archive_id"):
                truck_archive_id = truck_rec["archive_id"]
            elif endpoint_for(KIND_TRUCK) is not None and (
                KIND_TRUCK,
                plate_key_,
            ) not in attempted:
                attempted.add((KIND_TRUCK, plate_key_))
                truck_form = build_truck_form(
                    candidate.plate, sn_for(KIND_TRUCK, int((truck_rec or {}).get("count") or 0) + 1)
                )
                truck_result = await create_archives_fn(
                    {KIND_TRUCK: {plate_key_: truck_form}}, sk
                )
                truck_out = (truck_result.get(KIND_TRUCK) or {}).get(plate_key_) or {}
                if truck_out.get("success"):
                    truck_archive_id = str(truck_out["archive_id"])
                    store.set_archive(KIND_TRUCK, plate_key_, truck_archive_id, owner)
                    archived[(KIND_TRUCK, plate_key_)] = {
                        "display": candidate.plate,
                        "archive_id": truck_archive_id,
                    }
                elif truck_out.get("error"):
                    failed.append(
                        {
                            "kind": KIND_TRUCK,
                            "key": plate_key_,
                            "display": candidate.plate,
                            "reason": _failure_reason(truck_out),
                        }
                    )
        forms[kind] = {
            candidate.key: build_driver_form(candidate, rec, truck_archive_id)
        }
    elif kind in (KIND_BAILOR, KIND_PRICE):
        return None  # 委托人只计数不建档（来源字段缺失）；费目建档在自举管线（fee_bootstrap）

    results = await create_archives_fn(forms, sk)
    outcome = (results.get(kind) or {}).get(candidate.key) or {}
    if outcome.get("success"):
        store.set_archive(kind, candidate.key, outcome["archive_id"], owner)
        return {"display": candidate.display, "archive_id": outcome["archive_id"]}
    if outcome.get("duplicate") or outcome.get("no_id_created"):
        # T27b：TMS 已存在（唯一约束拒单）/ 已添加但响应无主键（no_id_created）→
        # 档案已在 TMS（无查询接口无法取 id）→ 登记 exists_external 不再重试
        # （后者若不登记，每次重试都会再建一条档案——AddCarFactory 实证）
        store.mark_exists_external(kind, candidate.key, owner)
        if exists_external is not None:
            exists_external.append(
                {
                    "kind": kind,
                    "key": candidate.key,
                    "display": candidate.display,
                    "message": (outcome.get("error") or {}).get("message") or "已存在",
                }
            )
        return None
    failed.append(
        {
            "kind": kind,
            "key": candidate.key,
            "display": candidate.display,
            "reason": _failure_reason(outcome) or "未知错误",
        }
    )
    if kind == KIND_FACTORY and no_client_id:
        # 空 client_id（省略键）被 TMS 拒（非「已存在」类）→ 本侧无法补齐客户 id
        # （无查询接口）→ 登记 skip_archive 终态，避免每批重发注定被拒的请求
        store.mark_skip_archive(kind, candidate.key, owner)
    return None


async def run_master_data_async(
    orders: list[CanonicalOrder], *, create_order: bool, sk: str = ""
) -> dict[str, Any] | None:
    """run_master_data（2026-09 异步化改造后为生产唯一入口）：建档段走 create_archives_async，其余逻辑逐行一致。

    create_order=true：计数 → 按依赖序建档（sk 由调用方登录 TMS 后透传）→ 当批
    回填 + 订单标注；false：只读探测（不计数不建档，展示当前计数状态）。
    disabled → None（不产生报告段）。
    建档保持依赖序串行（客户→工厂/司机跨档案依赖，不能并行）；
    建档失败/端点 TODO → 结构化进报告，不抛断订单流程。
    """
    config = load_config()
    if not config.get("enabled"):
        return None
    from .client import create_archives_async

    store = get_master_data_store()
    owner = _owner_for(sk, create_order)
    threshold = int(config["threshold"])
    candidates = collect_candidates(orders)
    report: dict[str, Any] = {
        "enabled": True,
        "threshold": threshold,
        "mode": "create" if create_order else "preview",
        "candidates": dict(Counter(c.kind for c in candidates)),
        "incremented": {},
        "archived": [],
        "failed": [],
        "exists_external": [],
        "degraded": [
            kind
            for kind in (KIND_CLIENT, KIND_FACTORY, KIND_TRUCK, KIND_DRIVER, KIND_BAILOR)
            if endpoint_for(kind) is None
        ],
    }

    if not create_order:
        # 只读探测：标注基于当前累计计数（不含本批），不写存储；
        # 无 sk → 无归属维度，不标注不列 pending（2026-09 owner 化）
        if owner is not None:
            _annotate_pending(candidates, store, threshold, owner)
            report["pending_top"] = _pending_top(store, threshold, owner)
        else:
            report["pending_top"] = []
        return report

    # 1) 计数：按单计（一单一次）；计数先于建档（当批累计、当批判定）；owner 隔离
    for candidate in candidates:
        store.record(candidate.kind, candidate.key, owner)
    report["incremented"] = dict(
        Counter(c.kind for c in candidates)
    )

    # 2) 建档（依赖序串行；每键本批只尝试一次，成功失败均不重试——失败下批重试；
    #    T27b：exists_external（TMS 已存在）登记后不再重试）
    attempted: set[tuple[str, str]] = set()
    archived: dict[str, dict[str, Any]] = {}
    failed: list[dict[str, Any]] = []
    exists_external: list[dict[str, Any]] = []
    for kind in ARCHIVE_ORDER:
        for candidate in _unique_by_key(candidates, kind):
            key = candidate.key
            if (kind, key) in attempted:
                continue
            attempted.add((kind, key))
            rec = store.get(kind, key, owner)
            if rec and (rec.get("archive_id") or rec.get("exists_external") or rec.get("skip_archive")):
                continue  # 历史已建档 / TMS 已存在（T27b）/ 本侧不可建档终态（无车牌司机）——均不再重试
            if int(rec.get("count") or 0) < threshold:
                continue  # 未达阈值
            url = endpoint_for(kind)
            if url is None:
                continue  # 端点 TODO → 只计数不建档（degraded 已在报告）
            if kind == KIND_FACTORY:
                # 依赖委托（2026-09-03 止血）：代表候选（首单）所属客户未达终态
                # 时换同键首个已就绪客户候选建档——防低频客户代表永久拦死整键
                candidate = _factory_dependency_candidate(
                    candidate, candidates, store, owner
                )
            result = await _create_one_async(
                kind, candidate, rec, store, create_archives_async, attempted, archived, failed, exists_external, sk,
                owner=owner,
            )
            if result:
                archived[(kind, key)] = result

    # 3) 当批回填 + 订单标注（全部候选，按当前记录取数；owner 隔离）
    for candidate in candidates:
        rec = store.get(candidate.kind, candidate.key, owner)
        if rec and rec.get("archive_id"):
            candidate.order._archive_refs[candidate.kind] = {
                "archive_id": rec["archive_id"],
                "key": candidate.key,
            }
    _annotate_pending(candidates, store, threshold, owner)

    report["archived"] = [
        {"kind": kind, "key": key, "display": result["display"], "archive_id": result["archive_id"]}
        for (kind, key), result in archived.items()
    ]
    report["failed"] = failed
    report["exists_external"] = exists_external
    report["pending_top"] = _pending_top(store, threshold, owner)
    return report
