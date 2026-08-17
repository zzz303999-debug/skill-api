"""竞品账单导入「成功单注册表」：提单号 → 首次创建回执（重复上传去重）。

方案一：同一提单号全局只允许创建成功一次——中断/并发下重复上传同一文件时，
已成功的单跳过（create_result 标记 skipped），失败单不登记、可修正后重导。

- 存储：{storage_dir}/imported_orders.json（单文件 JSON；进程内锁 + 临时文件原子替换，
  复用 master_data_store/fee_registry 的持久化模式——JSON + 进程锁 + 原子写，
  无 SQLite/DB 设施）；
- 语义：**只登记创建成功单**（submit 返回 success 才调用 register；失败/未尝试
  不登记，保证修正后重导失败单不被误拦）；
- 键规范化 normalize()：strip 首尾空白 + upper 统一大小写（假设下游 TMS 不区分
  提单号大小写；若下游敏感，改为仅 strip）；
- 并发：per-bl_no 锁 lock_for(bl_no) 供编排层包住「查重→提交→登记」临界区——
  同提单号跨请求串行化（check-then-act 原子化），不同提单号互不阻塞
  （历史账单按提单号串行导入，冲突率≈0）；
- 局限：单进程部署有效（同 master_data 计数存储）；文件只增不减，清理方式为
  删除文件全量重置（运维操作）；下游「已创建但响应超时」假失败兜不住
  （本地无记录），由 TMS 查重（方案二）补强。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

# 注册记录键：sn=首次创建 TMS 业务编号 / source_sha256=来源文件哈希（溯源）/
# created_at=登记时间（ISO 串）
_KEYS: tuple[str, ...] = ("sn", "source_sha256", "created_at")


def normalize(bl_no: str | None) -> str | None:
    """去重键规范化：strip 首尾空白 + upper 统一大小写；空值原样返回。"""
    if not bl_no:
        return bl_no
    return str(bl_no).strip().upper()


class ImportedOrderRegistry:
    """单文件成功单注册表（进程内锁 + 原子写；单进程部署下精确）。"""

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
    """注册表文件路径：{storage_dir}/imported_orders.json（测试可 monkeypatch）。"""
    return settings.storage_dir / "imported_orders.json"


# 进程内单例（惰性；测试用 reload_registry 重置路径与内存）
_REGISTRY: ImportedOrderRegistry | None = None


def get_imported_registry() -> ImportedOrderRegistry:
    """全局成功单注册表单例（惰性初始化）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ImportedOrderRegistry(store_path())
    return _REGISTRY


def reload_registry(path: Path | None = None) -> ImportedOrderRegistry:
    """重置注册表实例（测试用；path 注入时使用指定路径）。"""
    global _REGISTRY
    _REGISTRY = ImportedOrderRegistry(path or store_path())
    return _REGISTRY
