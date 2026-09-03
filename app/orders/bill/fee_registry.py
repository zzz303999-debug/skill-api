"""费目自举注册表（T24）：标准费目码 → {price_id, tms_name, created_at}。

- 存储：{storage_dir}/fee_registry.json（单文件 JSON；进程内锁 + 临时文件原子替换，
  复用 master_data_store 的持久化模式——JSON + 进程锁 + 原子写，无 SQLite/DB 设施）；
- 语义：自举建档成功才登记；**registry 不记失败**（建档失败当批降级、下批重试）；
  登记后解析顺序命中（YAML 显式 id → registry → 自举 → 降级，YAML 优先——
  人工修正永远压过自动产物）；
- 幂等：registry 命中即复用，不重发建档。
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

# 注册记录键：price_id=建档回值 / tms_name=费目名 / created_at=登记时间（ISO 串）
_KEYS: tuple[str, ...] = ("price_id", "tms_name", "created_at")


class FeeRegistry:
    """单文件费目注册表（进程内锁 + 原子写；单进程部署下精确）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        # 启动即加载磁盘数据（跨批次持久化：新实例/进程重启登记不丢）
        self.reload()

    def _load(self) -> dict[str, dict[str, Any]]:
        """读取文件（损坏/缺文件 → 空字典，不阻断导入）。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        data: dict[str, dict[str, Any]] = {}
        for code, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            price_id = rec.get("price_id")
            try:
                price_id = int(price_id) if price_id is not None else None
            except (TypeError, ValueError):
                price_id = None
            data[str(code)] = {
                "price_id": price_id,
                "tms_name": str(rec.get("tms_name") or "") or None,
                "created_at": str(rec.get("created_at") or "") or None,
            }
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

    def lookup(self, code: str) -> dict[str, Any] | None:
        """标准费目码 → 注册记录（无记录/未取到 price_id → None）。"""
        with self._lock:
            rec = self._data.get(code)
            return dict(rec) if rec and rec.get("price_id") is not None else None

    def mark_exists_external(self, code: str, tms_name: str | None) -> dict[str, Any]:
        """T27b：登记「费目已存在于 TMS」（建档 204 已存在拒单，无 price_id 可取）。

        语义：费目码在 TMS 价格表已存在（存量费目，多半名字撞了）——登记
        status=exists_external 后不再尝试自举建档（避免每次都 204 重试）；
        price_id 保持 null（无查询接口取不到 id，费用继续走降级不录入仅对账）。
        """
        with self._lock:
            rec = self._data.setdefault(code, {})
            rec["status"] = "exists_external"
            rec["price_id"] = None
            rec["tms_name"] = str(tms_name or "") or None
            rec["created_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def exists_external(self, code: str) -> bool:
        """T27b：该码是否已标记「外部已存在」（不再重试自举）。"""
        with self._lock:
            rec = self._data.get(code) or {}
            return rec.get("status") == "exists_external"

    def register(
        self, code: str, price_id: int, tms_name: str | None
    ) -> dict[str, Any]:
        """登记自举建档结果（price_id + tms_name + created_at）；返回最新记录。"""
        with self._lock:
            rec = self._data.setdefault(code, {})
            rec["price_id"] = int(price_id)
            rec["tms_name"] = str(tms_name or "") or None
            rec["created_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """全量快照（测试/报告用）。"""
        with self._lock:
            return {code: dict(rec) for code, rec in self._data.items()}


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
