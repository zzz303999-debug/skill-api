"""费目 price_id 映射表（T12/T24）：标准费目码 → {tms_name, import}，配置先行。

- 启动/首次使用加载 `config/fee_price_map.{settings.env}.yaml`；缺文件/YAML 错误
  → 启动 fail fast（RuntimeError，不允许裸跑）；环境切换只换文件，代码零环境名。
- owner 隔离（用户拍板：费目全链按 sk 判定，零全局路径）：price_id
  是归属敏感的 TMS 档案主键，**不再支持条目级全局显式 id**（残留旧键 fail fast
  防静默失效）——显式 id 按账号配在 `owner_price_ids: {<owner_key>: {<code>: id}}`
  段（键 = owner_key(sk) = sha256 前 16 hex，查询工具 scripts/fee_owner_key.py）；
  条目级（tms_name/import）是业务定义，全局共享无归属语义。
- 解析顺序四级（T24 费目自举，均限当前 owner 槽）：**owner_price_ids 显式 id →
  registry（自举产物）→ 自举创建 → 降级 skip_report**；显式优先（人工修正永远
  压过自动产物）；
- `price_id is None` → 该条降级（excluded，不录入、进对账报告），不阻塞整单；
- `other.price_id is None` 且存在 to_other 项 → 全部降级 skip_report + 显著 warning。
- price_id 跨通道通用（EX26081252/EX26080031 双重实证），单表服务三通道。
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import yaml

from app.core.config import settings
from app.core.logging_conf import get_logger

from .fee_name_map import is_dynamic_code
from .fee_registry import DEFAULT_OWNER, get_fee_registry

log = get_logger(__name__)

# 非费目条目的顶层段（不进条目缓存）：fee_bootstrap 为自举配置（另由
# fee_bootstrap.load_bootstrap_config 自读），owner_price_ids 为 per-owner 显式 id 段
_RESERVED_SECTIONS = {"fee_bootstrap", "owner_price_ids"}

_CACHE: dict[str, dict] | None = None
_OWNER_PRICE_IDS: dict[str, dict[str, int]] | None = None


def price_map_path() -> Path:
    """按环境解析映射表路径：config/fee_price_map.{settings.env}.yaml。"""
    return settings.config_dir / f"fee_price_map.{settings.env}.yaml"


def load_price_map() -> dict[str, dict]:
    """加载映射表（模块缓存；测试可用 reload_price_map 重置）。

    缺文件/YAML 错误 → RuntimeError（fail fast，不允许裸跑）——映射表是
    阶段二唯一外部依赖，缺表跑出的单费目无 price_id 无意义。
    """
    global _CACHE, _OWNER_PRICE_IDS
    # 双缓存同时就绪才早退：两段分别赋值会让 owner 段解析失败被 _CACHE 已缓存
    # 吞掉（永久静默），也让并发首载窗口丢显式 id（审查修复）
    if _CACHE is not None and _OWNER_PRICE_IDS is not None:
        return _CACHE
    path = price_map_path()
    if not path.exists():
        raise RuntimeError(
            f"fee price map missing: {path.name} (create config/{path.name})"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RuntimeError(f"fee price map invalid: {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"fee price map invalid: {path.name}: not a mapping")
    # 先算局部量、最后一次性赋值（任一解析失败都不留下半缓存状态）
    cache = {
        str(code): _normalize_entry(code, entry)
        for code, entry in data.items()
        if code not in _RESERVED_SECTIONS
    }
    owner_ids = _parse_owner_price_ids(data.get("owner_price_ids"), path.name)
    _CACHE, _OWNER_PRICE_IDS = cache, owner_ids
    return _CACHE


def _parse_owner_price_ids(
    section: object, source: str
) -> dict[str, dict[str, int]]:
    """解析 per-owner 显式 id 段：{owner_key: {code: id}}（null 值跳过）。

    结构/值非法 → RuntimeError（fail fast：显式 id 是运维预置，配错静默失效
    会导致该账号意外触发重建或降级）。
    """
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise RuntimeError(f"fee price map invalid: {source}: owner_price_ids not a mapping")
    result: dict[str, dict[str, int]] = {}
    for owner, ids in section.items():
        if not isinstance(ids, dict):
            raise RuntimeError(
                f"fee price map invalid: {source}: owner_price_ids.{owner} not a mapping"
            )
        entries: dict[str, int] = {}
        for code, value in ids.items():
            if value is None:
                continue
            # 只接受 int 与纯数字串：bool/float/非纯数字串拒绝（int() 会对
            # 820.9→820、true→1 静默截断，配错必须 fail fast——审查）
            if isinstance(value, int) and not isinstance(value, bool):
                num = value
            elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
                num = int(value)
            else:
                raise RuntimeError(
                    f"fee price map invalid: {source}: "
                    f"owner_price_ids.{owner}.{code}={value!r}"
                )
            entries[str(code)] = num
        result[str(owner)] = entries
    return result


def explicit_price_ids(owner: str) -> dict[str, int]:
    """当前 owner 的显式 id 段（缓存；无该 owner → 空字典）。"""
    if _OWNER_PRICE_IDS is None:
        load_price_map()
    return (_OWNER_PRICE_IDS or {}).get(owner) or {}


def _normalize_entry(code: str, entry) -> dict:
    """条目归一：{tms_name, import}；条目级 price_id 已废弃（owner 隔离）——
    显式 id 迁 owner_price_ids 段，残留旧键 fail fast 防静默失效。"""
    if not isinstance(entry, dict):
        raise RuntimeError(f"fee price map entry invalid: {code}")
    if entry.get("price_id") is not None:
        raise RuntimeError(
            f"fee price map entry invalid: {code}: entry-level price_id removed "
            f"(owner-scoped); move to owner_price_ids.<owner_key>.{code}"
        )
    return {
        "tms_name": str(entry.get("tms_name") or "") or None,
        "import": bool(entry.get("import", True)),
    }


def reload_price_map() -> dict[str, dict]:
    """重置映射表缓存（含显式 id 段；测试用）。"""
    global _CACHE, _OWNER_PRICE_IDS
    _CACHE = None
    _OWNER_PRICE_IDS = None
    return load_price_map()


def lookup(code: str, owner: str = DEFAULT_OWNER) -> dict | None:
    """标准费目码 → {tms_name, price_id, import}（显式段 → registry 两级，
    均限当前 owner；无 id → 返回条目本身供降级报告）。"""
    entry = load_price_map().get(code)
    price_id = resolve_price_id(code, owner)
    if price_id is not None:
        rec = get_fee_registry().lookup(code, owner) or {}
        return {
            "tms_name": str(rec.get("tms_name") or "") or (entry or {}).get("tms_name"),
            "price_id": price_id,
            "import": bool((entry or {}).get("import", True)),
        }
    return entry


def resolve_price_id(code: str, owner: str = DEFAULT_OWNER) -> int | None:
    """费目码 → 当前 owner 的 price_id：显式段 → registry（T24 四级解析前两级）。

    显式段优先（人工修正永远压过自举自动产物）；registry 由自举建档成功后
    按 owner 登记，命中即复用（幂等，不重发建档）。零全局路径：只查当前
    owner 槽，不回退其他 owner（费目隔离拍板）。
    """
    explicit = explicit_price_ids(owner)
    if explicit.get(code) is not None:
        return int(explicit[code])
    rec = get_fee_registry().lookup(code, owner)
    return int(rec["price_id"]) if rec else None


def apply_price_map(
    fees: list, owner: str = DEFAULT_OWNER
) -> tuple[list, list[dict]]:
    """FeeItem 列表回填 tms_name/price_id（显式段 → registry 两级解析，均限
    当前 owner 槽——订单费用只挂自己账号名下的档案）；返回 (原列表, 降级清单)。

    命名来源：标准码取 YAML 条目 tms_name；动态码（模板外费目，fee_bootstrap
    建档后）取 registry 记录 tms_name（缺省回退 note 原名）。

    降级规则（映射表文档 §4；动态码独立费目拍板）：
    - **模板外动态码（is_dynamic_code）建档失败/未触发**（price_id null）→ 降级归并
      本通道其它费（金额+原名 note 并入；不 excluded 不丢费）——与旧版 to_other 合并
      行为等价，registry 建档成功后下次上传即转独立发射；归并事件进 dropped 报告
      （reason=merged_to_other，service 不调整对账口径——金额仍在 recorded 内）；
    - 其余（标准码/其它费本身，含别名字典命中但映射表无条目的码）price_id null →
      该条不录入（excluded=True），进对账报告，不阻塞整单；
    - other.price_id null 且存在 to_other 项/动态码 → 全部降级 + 显著 warning
      （先补其它费 id）——保底归并以当前 owner 其它费已建档为前提（显式段/registry），
      缺档时回落 excluded+对账报告语义。
    返回的降级清单条目：{channel, code, money, reason}（report 用，含原名 note）。
    """
    price_map = load_price_map()
    other_entry = price_map.get("other") or {}
    other_price_id = resolve_price_id("other", owner)
    dropped: list[dict] = []
    # 其它费缺档告警统计源（审查 W2 修复）：只收循环内实际降级
    # excluded 的非 import:false 条目——原实现按「未 excluded」事后过滤恒为空
    # （降级分支已置 excluded），纯动态码账单/无真其它费列时告警恒不触发
    missing_codes: list[str] = []
    missing_other = False
    for fee in fees:
        entry = price_map.get(fee.code)
        if entry:
            fee.tms_name = entry.get("tms_name") or fee.tms_name
        fee.price_id = resolve_price_id(fee.code, owner)
        if fee.price_id is not None and not fee.tms_name:
            # 动态码（YAML 无条目）：registry 记录回填费目名（缺省回退 note 原名）
            rec = get_fee_registry().lookup(fee.code, owner)
            fee.tms_name = (rec or {}).get("tms_name") or fee.note
        if fee.excluded:
            continue  # import:false 项已不录入（税金等），无 price_id 也无需降级
        if fee.price_id is None and is_dynamic_code(fee.code) and other_price_id is not None:
            # 模板外动态码未建档成功 → 降级归并本通道其它费（保底录入不丢费；
            # note 原名保留——建档成功前以其它费名义可见，下次上传自动转独立）；
            # 归并事件进 dropped（reason=merged_to_other），仅报告不调整对账
            bucket = next(
                (
                    f
                    for f in fees
                    if f.channel == fee.channel and f.code == "other" and not f.excluded
                ),
                None,
            )
            if bucket is None:
                fee.code = "other"
                fee.tms_name = other_entry.get("tms_name") or fee.tms_name or "其它费"
                fee.price_id = other_price_id
                continue
            bucket.money += fee.money
            if fee.note:
                parts = bucket.note.split(",") if bucket.note else []
                for name in fee.note.split(","):
                    name = name.strip()
                    if name and name not in parts:
                        parts.append(name)
                bucket.note = ",".join(parts)
            dropped.append(
                {
                    "channel": fee.channel,
                    "code": fee.code,
                    "tms_name": fee.tms_name,
                    "money": str(fee.money),
                    "note": fee.note,
                    "reason": "merged_to_other",
                }
            )
            fee.excluded = True  # 金额已并入 bucket → 自身不发射（金额同步置零，
            # 防 fees 响应求和双算；原金额见 dropped 报告 merged_to_other 条目）
            fee.money = Decimal("0")
            continue
        if fee.price_id is None:
            if fee.code == "other":
                missing_other = True
            else:
                missing_codes.append(fee.code)
            fee.excluded = True
            dropped.append(
                {
                    "channel": fee.channel,
                    "code": fee.code,
                    "tms_name": fee.tms_name,
                    "money": str(fee.money),
                    "note": fee.note,
                    "reason": "price_id null",
                }
            )
    # 其它费缺档显著告警（审查补位 + W2 修复）：真其它费列自身或
    # 动态码归并保底无目标（其它费无 id）→ 未解析条目整批 excluded——原 to_other
    # warning 依赖 code=other 条目存在且未降级，动态码形态/纯动态码账单下恒不
    # 触发（死代码）；统计源改用循环内实际降级条目（missing_codes/missing_other）
    if other_price_id is None and (missing_codes or missing_other):
        log.warning(
            "fee_price_map_other_missing",
            extra={
                "reason": "other.price_id 未补：动态码归并保底无目标/其它费列自身无法落位，相关费目整批 excluded（仅对账）",
                "codes": missing_codes[:20],
            },
        )
    return fees, dropped


def money_str(money: Decimal) -> str:
    """金额 → 两位小数字符串（T13 实证格式，如 "123.00"）。"""
    return f"{Decimal(money):.2f}"
