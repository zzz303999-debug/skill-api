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

from collections import Counter
from typing import Any

from app.core.logging_conf import get_logger

# 本模块 import 仅自用；跨包引用 config/store/keys 符号请直引定义模块，此处不做转口
# （2026-09-07 P-A 口径：防止“定义在 A、引用从 B 转口”再生）
from ..schema import CanonicalOrder
from ..submission.imported_registry import owner_key
from .config import (
    ARCHIVE_ORDER,
    KIND_BAILOR,
    KIND_CLIENT,
    KIND_DRIVER,
    KIND_FACTORY,
    KIND_PRICE,
    KIND_TRUCK,
    MasterDataCandidate,
    endpoint_for,
    kind_label,
    load_config,
    sn_for,
)
from .keys import (
    client_key,
    driver_key,
    factory_key,
    plate_key,
)
from .store import DEFAULT_OWNER, get_master_data_store

log = get_logger(__name__)

# 计数档案类（collect_candidates 会收集的候选类型）
COUNT_KINDS: tuple[str, ...] = (KIND_CLIENT, KIND_FACTORY, KIND_DRIVER)

# 报告未达阈值 TOP 清单条数上限
PENDING_TOP_N = 10


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






# ---- 阈值编排（service 管线挂点入口） ----

def _owner_for(sk: str, create_order: bool) -> str | None:
    """建档维度键（2026-09 统一按 sk 判断，与去重注册表同口径）：
    create 有 sk → owner_key(sk)；无 sk（测试直调）→ DEFAULT_OWNER 槽；
    preview 无 sk → None（只读探测无归属：不标注、不列 pending）。"""
    if sk:
        return owner_key(sk)
    return DEFAULT_OWNER if create_order else None


def failure_reason(outcome: dict[str, Any]) -> str:
    """建档失败原因摘要（error 三元组 → 可读串；公开口径：fees 报告复用）。"""
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


# ---- 建档实现（单批会话 + 单键建档；网络段走 create_archives_async）----


class _ArchiveSession:
    """单批建档会话（M1a，2026-09-09）：收集器（attempted/archived/failed/
    exists_external）与 store/owner/sk/建档网络注入收口——单键建档与车辆前置
    子流程共用同一批内去重与报告收集，消除跨函数手工传递。"""

    def __init__(self, store, owner: str, sk: str, create_archives_fn):
        self.store = store
        self.owner = owner
        self.sk = sk
        self.create_archives_fn = create_archives_fn
        self.attempted: set[tuple[str, str]] = set()
        self.archived: dict[tuple[str, str], dict[str, Any]] = {}
        self.failed: list[dict[str, Any]] = []
        self.exists_external: list[dict[str, Any]] = []

    def mark_attempted(self, kind: str, key: str) -> None:
        """登记本批已尝试键（成功失败均不重试）。"""
        self.attempted.add((kind, key))

    def record_archive(self, kind: str, key: str, display: str, archive_id: str) -> None:
        self.archived[(kind, key)] = {"display": display, "archive_id": archive_id}

    def record_fail(self, kind: str, key: str, display: str, reason: str) -> None:
        self.failed.append(
            {"kind": kind, "key": key, "display": display, "reason": reason}
        )

    def record_exists_external(
        self, kind: str, key: str, display: str, message: str
    ) -> None:
        self.exists_external.append(
            {"kind": kind, "key": key, "display": display, "message": message}
        )


async def _ensure_truck_archive_async(
    candidate: MasterDataCandidate, session: _ArchiveSession
) -> str:
    """车辆前置建档（依赖序：车辆 → 司机；M1b 自 driver 分支抽取，M5 补终态）。

    同车牌已达终态（本侧建档 archive_id → 复用 id；TMS 已存在 exists_external →
    不再重发）直接返回；否则端点可用且本批未试 → 建档登记（成功进 archived /
    已存在 duplicate → 登记 exists_external / 其余失败进 failed）；返回
    truck_archive_id（无则空串——truck_id 可空，建车失败不阻塞建司机）。
    """
    plate = str(candidate.plate or "").strip()
    if not plate:
        return ""
    plate_key_ = plate_key(plate)
    truck_rec = session.store.get(KIND_TRUCK, plate_key_, session.owner)
    if truck_rec:
        if truck_rec.get("archive_id"):
            return str(truck_rec["archive_id"])
        if truck_rec.get("exists_external"):
            # M5（2026-09-09）：TMS 已存在（无查询接口取 id）→ 不再重发建车
            # 请求，司机照常建档（truck_id 可空，不阻塞）
            return ""
    if (
        endpoint_for(KIND_TRUCK) is None
        or (KIND_TRUCK, plate_key_) in session.attempted
    ):
        return ""
    session.mark_attempted(KIND_TRUCK, plate_key_)
    from .client import build_truck_form

    truck_form = build_truck_form(
        plate, sn_for(KIND_TRUCK, int((truck_rec or {}).get("count") or 0) + 1)
    )
    truck_result = await session.create_archives_fn(
        {KIND_TRUCK: {plate_key_: truck_form}}, session.sk
    )
    truck_out = (truck_result.get(KIND_TRUCK) or {}).get(plate_key_) or {}
    if truck_out.get("success"):
        truck_archive_id = str(truck_out["archive_id"])
        session.store.set_archive(
            KIND_TRUCK, plate_key_, truck_archive_id, session.owner
        )
        session.record_archive(KIND_TRUCK, plate_key_, plate, truck_archive_id)
        return truck_archive_id
    if truck_out.get("duplicate") or truck_out.get("no_id_created"):
        # M5（2026-09-09）：TMS 已存在拒单/无主键回值 → 登记 exists_external
        # 终态（此前只进 failed，存量车牌跨批每批重发注定被拒的建车请求——
        # 对齐主建档路径 _create_one_async 三态处理）
        session.store.mark_exists_external(KIND_TRUCK, plate_key_, session.owner)
        session.record_exists_external(
            KIND_TRUCK,
            plate_key_,
            plate,
            (truck_out.get("error") or {}).get("message") or "已存在",
        )
        return ""
    if truck_out.get("error"):
        session.record_fail(KIND_TRUCK, plate_key_, plate, failure_reason(truck_out))
    return ""


async def _create_one_async(
    kind: str, candidate: MasterDataCandidate, session: _ArchiveSession
) -> dict[str, Any] | None:
    """建档一次（M1a 签名收敛 12 → 3）：依赖前置检查 + 建档调用 + 结果登记；
    失败进会话 failed 不抛断。rec 由会话 store 重取（主循环已判终态/阈值）。

    T27b：响应带 duplicate 标记（TMS 已存在拒单）→ store 登记 exists_external
    （不再重试，archive_id 保持 null），进 exists_external 报告段（与 failed 区分）；
    no_id_created（已添加但无主键回值）同登记终态防重复建档。
    """
    rec = session.store.get(kind, candidate.key, session.owner)
    forms: dict[str, dict[str, str]] = {}
    no_client_id = False  # 工厂建档无客户 id（客户已存在但无查询接口），失败需登记终态
    if kind == KIND_CLIENT:
        from .client import build_client_form

        forms[kind] = {candidate.key: build_client_form(candidate, rec)}
    elif kind == KIND_FACTORY:
        from .client import build_factory_form

        client_rec = _client_record(candidate, session.store, session.owner)
        # 前置满足 = 所属客户已达终态（本侧建档 archive_id / TMS 已存在
        # exists_external）；仅计数（从未建档成功）→ 依赖未就绪，工厂缓建
        # （计数保留下批重试）
        if not client_rec or not (
            client_rec.get("archive_id") or client_rec.get("exists_external")
        ):
            session.record_fail(
                kind,
                candidate.key,
                candidate.display,
                "所属客户未建档（依赖前置：客户 → 工厂）",
            )
            return None
        # 2026-09-03 修复：客户 exists_external（TMS 已存在、无本地 id）不再本地
        # 拦截——工厂照常尝试（client_id 空值省略键，仅带 client_name；TMS 是否
        # 接受由响应登记：成功→archive；已存在→exists_external；其余拒绝→
        # skip_archive 防每批重发，见下）
        client_archive_id = str(client_rec.get("archive_id") or "")
        forms[kind] = {
            candidate.key: build_factory_form(candidate, rec, client_archive_id)
        }
        no_client_id = not client_archive_id
    elif kind == KIND_DRIVER:
        from .client import build_driver_form

        if not candidate.plate:
            # TMS AddCarDriver 必填 num（车牌），无车牌司机永久无法建档（2026-08-14
            # live 实证拒单「请重新选择车牌」）→ 登记 skip_archive 终态不再重试
            # （避免每批都发注定被拒的请求）；订单保留「未建档」标注，计数照常
            # （后续订单带车牌 → 新计数键 → 恢复正常建档）
            session.store.mark_skip_archive(kind, candidate.key, session.owner)
            session.record_fail(
                kind,
                candidate.key,
                candidate.display,
                "司机无车牌（TMS AddCarDriver 必填 num），无法建档",
            )
            return None
        if not candidate.phone:
            # TMS CarDriver.php 校验 phone 必填（缺键 500 / 空串 no: phone，2026-08-14
            # live 实证）——无手机号司机同样永久无法建档 → skip_archive 终态
            session.store.mark_skip_archive(kind, candidate.key, session.owner)
            session.record_fail(
                kind,
                candidate.key,
                candidate.display,
                "司机无手机号（TMS AddCarDriver 必填 phone），无法建档",
            )
            return None
        # 依赖序：车辆 → 司机（同车牌建过不再建；建车失败不阻塞建司机——truck_id 可空）
        truck_archive_id = await _ensure_truck_archive_async(candidate, session)
        forms[kind] = {
            candidate.key: build_driver_form(candidate, rec, truck_archive_id)
        }
    elif kind in (KIND_BAILOR, KIND_PRICE):
        return None  # 委托人只计数不建档（来源字段缺失）；费目建档在自举管线（fee_bootstrap）

    results = await session.create_archives_fn(forms, session.sk)
    outcome = (results.get(kind) or {}).get(candidate.key) or {}
    if outcome.get("success"):
        session.store.set_archive(
            kind, candidate.key, outcome["archive_id"], session.owner
        )
        return {"display": candidate.display, "archive_id": outcome["archive_id"]}
    if outcome.get("duplicate") or outcome.get("no_id_created"):
        # T27b：TMS 已存在（唯一约束拒单）/ 已添加但响应无主键（no_id_created）→
        # 档案已在 TMS（无查询接口无法取 id）→ 登记 exists_external 不再重试
        # （后者若不登记，每次重试都会再建一条档案——AddCarFactory 实证）
        session.store.mark_exists_external(kind, candidate.key, session.owner)
        session.record_exists_external(
            kind,
            candidate.key,
            candidate.display,
            (outcome.get("error") or {}).get("message") or "已存在",
        )
        return None
    session.record_fail(
        kind, candidate.key, candidate.display, failure_reason(outcome) or "未知错误"
    )
    if kind == KIND_FACTORY and no_client_id:
        # 空 client_id（省略键）被 TMS 拒（非「已存在」类）→ 本侧无法补齐客户 id
        # （无查询接口）→ 登记 skip_archive 终态，避免每批重发注定被拒的请求
        session.store.mark_skip_archive(kind, candidate.key, session.owner)
    return None


async def run_master_data_async(
    orders: list[CanonicalOrder], *, create_order: bool, sk: str = ""
) -> dict[str, Any] | None:
    """阈值建档总编排：create=true 计数 → 按依赖序建档（sk 由调用方登录 TMS 后
    透传）→ 当批回填 + 订单标注；false 只读探测（不计数不建档，展示当前计数状态）。

    disabled → None（不产生报告段）。建档保持依赖序串行（客户→工厂/司机跨档案
    依赖，不能并行）；建档失败/端点 TODO → 结构化进报告，不抛断订单流程。
    """
    config = load_config()
    if not config.get("enabled"):
        return None
    from .client import create_archives_async

    store = get_master_data_store()
    owner = _owner_for(sk, create_order)
    threshold = int(config["threshold"])
    candidates = collect_candidates(orders)
    kind_counts = dict(Counter(c.kind for c in candidates))
    report: dict[str, Any] = {
        "enabled": True,
        "threshold": threshold,
        "mode": "create" if create_order else "preview",
        "candidates": kind_counts,
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
    report["incremented"] = kind_counts

    # 2) 建档（依赖序串行；每键本批只尝试一次，成功失败均不重试——失败下批重试；
    #    T27b：exists_external（TMS 已存在）登记后不再重试）
    session = _ArchiveSession(store, owner, sk, create_archives_async)
    for kind in ARCHIVE_ORDER:
        for candidate in _unique_by_key(candidates, kind):
            key = candidate.key
            if (kind, key) in session.attempted:
                continue
            session.mark_attempted(kind, key)
            rec = store.get(kind, key, owner)
            if store.is_final(kind, key, owner):
                continue  # 历史终态（本侧已建档 / TMS 已存在 T27b / 不可建档）——不再重试
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
            result = await _create_one_async(kind, candidate, session)
            if result:
                session.archived[(kind, key)] = result

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
        for (kind, key), result in session.archived.items()
    ]
    report["failed"] = session.failed
    report["exists_external"] = session.exists_external
    report["pending_top"] = _pending_top(store, threshold, owner)
    return report
