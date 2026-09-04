"""建档域常量与配置加载（config/master_data.{env}.yaml）。

从 orchestrator 拆出（2026-09）：KIND 常量/档案类展示名/候选 dataclass 被
下游封装（client.py）与编排（orchestrator.py）共同依赖，下沉本模块后
依赖方向单向：orchestrator → client → config ← store（config/store 互不依赖）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.config import settings
from app.core.logging_conf import get_logger

from ..schema import CanonicalOrder

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

# 档案类 → 中文展示名（报告/标注用；通用业务语义，非 TMS 内部值）
_KIND_LABELS: dict[str, str] = {
    KIND_CLIENT: "客户",
    KIND_FACTORY: "工厂",
    KIND_BAILOR: "委托人",
    KIND_TRUCK: "车辆",
    KIND_DRIVER: "司机",
    KIND_PRICE: "费目",
}


def kind_label(kind: str) -> str:
    """档案类 → 中文展示名（报告/标注）。"""
    return _KIND_LABELS.get(kind, kind)


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


_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "config"
# 按环境解析：config/master_data.{env}.yaml（APP_ENV 选择，与 fee_price_map 同模式）。
# 双份随镜像分发，环境切换零 Git 改动——本地联调永远 test，生产永远 prod。
_CONFIG_PATH = _CONFIG_DIR / f"master_data.{settings.env}.yaml"

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
