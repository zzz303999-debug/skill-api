"""费目自举注册表（T24）：费目码 → {owner: {price_id, tms_name, created_at}}。

- 存储：{storage_dir}/fee_registry.json（单文件 JSON；进程内锁 + 临时文件原子替换，
  复用 master_data_store 的持久化模式——JSON + 进程锁 + 原子写，无 SQLite/DB 设施）；
- owner 维度（用户拍板：费目建档全链按 sk 隔离，废除首个触发者建档
  全局共享的旧语义——档案归属必须跟当前上传者账号走，不串到别人名下）：
  owner = owner_key(sk)（sha256 前 16 hex，与 imported_registry/master_data_store
  同款口径），建档幂等判定与登记按 owner 槽隔离，无全局回退；
- 语义：自举建档成功才登记；**registry 不记失败**（建档失败当批降级、下批重试）；
  登记后解析顺序命中（per-owner 显式 id → registry → 自举 → 降级，显式优先——
  人工修正永远压过自动产物）；
- 幂等：owner 槽命中即复用，不重发建档；不同 owner 互不串扰；
- 无 sk 直调（测试/异常路径）落 DEFAULT_OWNER 槽；旧版全局条目（无 owner）加载时
  整体迁入 _LEGACY_OWNER 槽保留审计、不再参与判定（各 owner 槽自举重建）。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging_conf import get_logger

log = get_logger(__name__)

# 注册记录键：price_id=建档回值 / tms_name=费目名 / created_at=登记时间（ISO 串）/
# status=终态标记（exists_external：TMS 已存在拒单，不再重试）
_KEYS: tuple[str, ...] = ("price_id", "tms_name", "created_at", "status")

# 无 sk 上下文（测试/异常直调）的 owner 槽位：真实 owner 为 16 位 hex，不会与
# 字面量撞名；旧版全局条目迁移槽（保留审计，不再参与判定）——与
# master_data_store 同款口径（费目隔离拍板）
DEFAULT_OWNER = "default"
_LEGACY_OWNER = "_legacy"


class FeeRegistry:
    """单文件费目注册表（进程内锁 + 原子写；单进程部署下精确）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        # 启动即加载磁盘数据（跨批次持久化：新实例/进程重启登记不丢）
        self.reload()

    @staticmethod
    def _normalize_rec(rec: dict[str, Any]) -> dict[str, Any]:
        """记录归一：price_id 数字化（非法 → None）、字符串键清洗、status 保留。"""
        price_id = rec.get("price_id")
        try:
            price_id = int(price_id) if price_id is not None else None
        except (TypeError, ValueError):
            price_id = None
        return {
            "price_id": price_id,
            "tms_name": str(rec.get("tms_name") or "") or None,
            "created_at": str(rec.get("created_at") or "") or None,
            "status": rec.get("status"),
        }

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        """读取文件（损坏/缺文件 → 空字典，不阻断导入）。

        旧版全局格式 {code: rec} 与新版 {code: {owner: rec}} 兼容：顶层 value
        直接含记录键（price_id 等）判为旧版，整体迁入 _LEGACY_OWNER 槽（无 sk
        无法归属；保留审计，不再参与幂等判定——各 owner 槽自举重建，
        费目隔离拍板）。顺带修复：status 键旧版加载时被丢弃导致 exists_external
        进程重启后失效的重试循环。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        data: dict[str, dict[str, dict[str, Any]]] = {}
        for code, val in raw.items():
            if not isinstance(val, dict):
                continue
            owners: dict[str, dict[str, Any]] = {}
            if any(k in _KEYS for k in val):
                # 旧版全局扁平记录 → legacy 槽
                owners[_LEGACY_OWNER] = self._normalize_rec(
                    {k: val.get(k) for k in _KEYS}
                )
            else:
                for owner, rec in val.items():
                    if not isinstance(rec, dict):
                        continue
                    owners[str(owner)] = self._normalize_rec(rec)
            data[str(code)] = owners
        return data

    def _save(self) -> None:
        """原子写：临时文件 + replace（进程崩溃不损坏主文件）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def reload(self) -> None:
        """重新从磁盘加载（测试/多进程热加载用）。"""
        with self._lock:
            self._data = self._load()

    def lookup(
        self, code: str, owner: str = DEFAULT_OWNER
    ) -> dict[str, Any] | None:
        """费目码 → 当前 owner 槽的注册记录（无记录/未取到 price_id → None）。

        只查该 owner 槽，不回退全局（不同 sk 各自建档各自持有 price_id）。
        """
        with self._lock:
            rec = self._data.get(code, {}).get(owner)
            return dict(rec) if rec and rec.get("price_id") is not None else None

    def mark_exists_external(
        self, code: str, tms_name: str | None, owner: str = DEFAULT_OWNER
    ) -> dict[str, Any]:
        """T27b：登记「费目已存在于 TMS」（建档 204 已存在拒单，无 price_id 可取）。

        语义：该费目码在**当前 owner** 的 TMS 价格表已存在（多半存量费目，名字
        撞了）——登记 status=exists_external 后该 owner 不再尝试自举建档（避免
        每次都 204 重试）；price_id 保持 null（无查询接口取不到 id，费用继续走
        降级不录入仅对账）。按 owner 隔离：A 账号撞名不影响 B 账号自举。
        """
        with self._lock:
            rec = self._data.setdefault(code, {}).setdefault(owner, {})
            rec["status"] = "exists_external"
            rec["price_id"] = None
            rec["tms_name"] = str(tms_name or "") or None
            rec["created_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def exists_external(self, code: str, owner: str = DEFAULT_OWNER) -> bool:
        """T27b：该码在当前 owner 槽是否已标记「外部已存在」（不再重试自举）。"""
        with self._lock:
            rec = self._data.get(code, {}).get(owner) or {}
            return rec.get("status") == "exists_external"

    def register(
        self,
        code: str,
        price_id: int,
        tms_name: str | None,
        owner: str = DEFAULT_OWNER,
    ) -> dict[str, Any]:
        """登记自举建档结果到当前 owner 槽（price_id + tms_name + created_at）。"""
        with self._lock:
            rec = self._data.setdefault(code, {}).setdefault(owner, {})
            rec["price_id"] = int(price_id)
            rec["tms_name"] = str(tms_name or "") or None
            rec["created_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def snapshot(
        self, owner: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """全量快照（测试/报告用）；owner 给定时只取该槽（{code: rec}）。"""
        with self._lock:
            if owner is None:
                return {
                    code: {ow: dict(rec) for ow, rec in owners.items()}
                    for code, owners in self._data.items()
                }
            return {
                code: dict(owners[owner])
                for code, owners in self._data.items()
                if owner in owners
            }


def store_path() -> Path:
    """注册表文件路径：{storage_dir}/fee_registry.json（测试可 monkeypatch）。"""
    return settings.storage_dir / "fee_registry.json"


# 进程内单例（惰性；测试用 reload_registry 重置路径与内存）
_REGISTRY: FeeRegistry | None = None


def get_fee_registry() -> FeeRegistry:
    """全局费目注册表单例（惰性初始化）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = FeeRegistry(store_path())
    return _REGISTRY


def reload_registry(path: Path | None = None) -> FeeRegistry:
    """重置注册表实例（测试用；path 注入时使用指定路径）。"""
    global _REGISTRY
    _REGISTRY = FeeRegistry(path or store_path())
    return _REGISTRY
