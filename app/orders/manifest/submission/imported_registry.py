"""舱单导入「成功单注册表」：提单号 → 首次创建回执（重复上传去重）。

⚠️ v1.9 起舱单放开重复导入（用户拍板）：service 层不再查重/登记，
本模块暂停生产使用（保留供回滚/恢复；历史数据 imported_manifests.json 不清除）。

与竞品账单 imported_registry 同模式（契约见需求文档 §6.8）：
- 存储：{storage_dir}/imported_manifests.json（独立文件，与账单注册表隔离）；
- 语义：只登记创建成功单（addBill 返回 success 才 register；失败单不登记，
  修正后重导不被误拦）；first-write-wins（sn 不回退，幂等）；
- 键规范化：strip + 大写统一（同账单 normalize 口径）；
- 并发：per-bl_no 锁 lock_for 包住「查重→提交→登记」临界区；
- 单进程单 worker 部署前提（同账单），禁止多 worker。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.logging_conf import get_logger

# P6 起注册表公共工具单一实现（re-export 保持消费方 import 路径不变）
from ...registry_common import normalize  # noqa: F401

log = get_logger(__name__)




class ManifestRegistry:
    """单文件成功单注册表（进程内锁 + 原子写；单进程部署下精确）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
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
        for bl_no, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            data[str(bl_no)] = {
                "sn": rec.get("sn"),
                "source_sha256": str(rec.get("source_sha256") or "") or None,
                "created_at": str(rec.get("created_at") or "") or None,
            }
        return data

    def _save(self) -> None:
        """原子写：临时文件 + replace（进程崩溃不损坏主文件）。"""
        from ...registry_common import atomic_write_json

        atomic_write_json(self.path, self._data)

    def reload(self) -> None:
        """重新从磁盘加载（测试/多进程热加载用）。"""
        with self._lock:
            self._data = self._load()

    def lookup(self, bl_no: str) -> dict[str, Any] | None:
        """提单号 → 首次创建回执（未登记/空键 → None）。键自动规范化。"""
        key = normalize(bl_no)
        if not key:
            return None
        with self._lock:
            rec = self._data.get(key)
            return dict(rec) if rec else None

    def register(
        self,
        bl_no: str,
        sn: str | None = None,
        source_sha256: str | None = None,
    ) -> dict[str, Any]:
        """登记创建成功单（首次 sn + 来源文件哈希 + 时间）；返回最新记录。

        只在提交成功后调用（本模块不判断成败）；同键重复登记以首次为准
        （sn 不回退，幂等）。
        """
        key = normalize(bl_no)
        if not key:
            raise ValueError("bl_no is required for register")
        with self._lock:
            rec = self._data.setdefault(key, {})
            if "sn" not in rec:
                rec["sn"] = sn
            rec["source_sha256"] = str(source_sha256 or "") or None
            rec["created_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """全量快照（测试断言/运维排查用）。"""
        with self._lock:
            return {bl_no: dict(rec) for bl_no, rec in self._data.items()}

    def clear(self) -> None:
        """全量清空（运维重置；TMS 手工删单后重导先执行）。"""
        with self._lock:
            self._data = {}
            self._save()


# ---- per-bl_no 并发锁：编排层包住「查重→提交→登记」临界区 ----

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def lock_for(bl_no: str) -> Iterator[None]:
    """按提单号取互斥锁（上下文管理器）：同键串行化，异键互不阻塞。"""
    key = normalize(bl_no)
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key or "", threading.Lock())
    with lock:
        yield


def store_path() -> Path:
    """注册表文件路径：{storage_dir}/imported_manifests.json（测试可 monkeypatch）。"""
    from ...registry_common import registry_storage_path

    return registry_storage_path("imported_manifests.json")


# 进程内单例（惰性；测试用 reload_registry 重置路径与内存）
_REGISTRY: ManifestRegistry | None = None


def get_manifest_registry() -> ManifestRegistry:
    """全局成功单注册表单例（惰性初始化）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ManifestRegistry(store_path())
    return _REGISTRY


def reload_registry(path: Path | None = None) -> ManifestRegistry:
    """重置注册表实例（测试用；path 注入时使用指定路径）。"""
    global _REGISTRY
    _REGISTRY = ManifestRegistry(path or store_path())
    return _REGISTRY
