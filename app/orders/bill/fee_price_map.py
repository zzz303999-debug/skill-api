"""费目 price_id 映射表（T12）：标准费目码 → {tms_name, price_id}，配置先行。

- 启动/首次使用加载 `config/fee_price_map.{settings.env}.yaml`；缺文件/YAML 错误
  → 启动 fail fast（RuntimeError，不允许裸跑）；环境切换只换文件，代码零环境名。
- 解析顺序：FeeItem.code 查表 → 回填 tms_name/price_id；`price_id is None` →
  该条降级（excluded，不录入、进对账报告），不阻塞整单；
- `other.price_id is None` 且存在 to_other 项 → 全部降级 skip_report + 显著 warning。
- price_id 全局唯一、跨通道通用（EX26081252/EX26080031 双重实证），单表服务三通道。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "config"

_CACHE: dict[str, dict] | None = None


def price_map_path() -> Path:
    """按环境解析映射表路径：config/fee_price_map.{settings.env}.yaml。"""
    return _CONFIG_DIR / f"fee_price_map.{settings.env}.yaml"


def load_price_map() -> dict[str, dict]:
    """加载映射表（模块缓存；测试可用 reload_price_map 重置）。

    缺文件/YAML 错误 → RuntimeError（fail fast，不允许裸跑）——映射表是
    阶段二唯一外部依赖，缺表跑出的单费目无 price_id 无意义。
    """
    global _CACHE
    if _CACHE is not None:
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
    _CACHE = {str(code): _normalize_entry(code, entry) for code, entry in data.items()}
    return _CACHE


def _normalize_entry(code: str, entry) -> dict:
    """条目归一：{tms_name, price_id, import}；price_id 数字原样、null/缺失 → None。"""
    if not isinstance(entry, dict):
        raise RuntimeError(f"fee price map entry invalid: {code}")
    price_id = entry.get("price_id")
    try:
        price_id = int(price_id) if price_id is not None else None
    except (TypeError, ValueError):
        raise RuntimeError(f"fee price map price_id invalid: {code}={price_id!r}") from None
    return {
        "tms_name": str(entry.get("tms_name") or "") or None,
        "price_id": price_id,
        "import": bool(entry.get("import", True)),
    }


def reload_price_map() -> dict[str, dict]:
    """重置映射表缓存（测试用）。"""
    global _CACHE
    _CACHE = None
    return load_price_map()


def lookup(code: str) -> dict | None:
    """标准费目码 → {tms_name, price_id, import}；未登记 → None。"""
    return load_price_map().get(code)


def apply_price_map(fees: list) -> tuple[list, list[dict]]:
    """FeeItem 列表回填 tms_name/price_id；返回 (原列表, 降级清单)。

    降级规则（映射表文档 §4）：
    - 某费目 price_id null → 该条不录入（excluded=True），进对账报告，不阻塞整单；
    - other.price_id null 且存在 to_other 项 → 全部降级 + 显著 warning（先补其它费 id）。
    返回的降级清单条目：{channel, code, money, reason}（report 用，含原名 note）。
    """
    price_map = load_price_map()
    has_other_items = any(fee.code == "other" for fee in fees)
    other_price_id = (price_map.get("other") or {}).get("price_id")
    dropped: list[dict] = []
    for fee in fees:
        entry = price_map.get(fee.code)
        if entry:
            fee.tms_name = entry.get("tms_name") or fee.tms_name
            fee.price_id = entry.get("price_id")
        if fee.excluded:
            continue  # import:false 项已不录入（税金等），无 price_id 也无需降级
        if fee.price_id is None:
            # to_other 归并目标（其它费）无 price_id → 长尾全部无法落位，显著告警
            if fee.code == "other" and other_price_id is None and has_other_items:
                log.warning(
                    "fee_price_map_other_missing",
                    extra={"reason": "other.price_id 未补，未匹配费目全部降级 skip_report"},
                )
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
    return fees, dropped


def money_str(money: Decimal) -> str:
    """金额 → 两位小数字符串（T13 实证格式，如 "123.00"）。"""
    return f"{Decimal(money):.2f}"
