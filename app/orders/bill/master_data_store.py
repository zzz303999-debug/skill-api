"""基础资料计数存储（T17）：跨批次持久化 + 原子自增 + 并发/重入安全。

- 存储：{storage_dir}/master_data.json（单文件 JSON；进程内锁 + 临时文件原子替换，
  沿用 repo 既有持久化约定——无 SQLite/DB 设施，与 template_store/access_log 同源）；
- 结构：{kind: {key: {owner: {count, archive_id, archived_at, last_seen}}}}，键 =
  (kind, 归一键, owner)；owner = 去重/建档维度（sha256(sk) 前 16 hex，与
  imported_registry 同款口径，2026-09 统一按 sk 判断）；
- 语义：计数与建档登记按 owner 隔离（不同 TMS 用户各自累计/建档，互不串扰）；
  无 sk 直调（测试/异常路径）落 DEFAULT_OWNER 槽；archive_id 非空 = 已建档；
  旧版全局条目（无 owner）加载时整体迁入 _LEGACY_OWNER 槽保留审计、不再参与判定。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

# 档案记录键：count=累计出现次数 / archive_id=建档主键回值 / archived_at=建档时间 /
# last_seen=最近出现时间（ISO 串）/ exists_external=外部已存在标记（T27b：204 已存在
# 登记后不再重试建档，archive_id 保持 null）/ skip_archive=本侧不可建档终态标记
# （如司机无车牌——TMS AddCarDriver 必填 num，永久性约束，登记后不再重试）
_KEYS: tuple[str, ...] = (
    "count", "archive_id", "archived_at", "last_seen", "exists_external", "skip_archive"
)

# 无 sk 上下文（测试/异常直调）的 owner 槽位：真实 owner 为 16 位 hex，
# 不会与字面量撞名；旧版全局条目迁移槽（保留审计，不再参与判定）
DEFAULT_OWNER = "default"
_LEGACY_OWNER = "_legacy"


class MasterDataStore:
    """单文件计数存储（进程内锁 + 原子写；单进程部署下精确）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        # 启动即加载磁盘数据（跨批次持久化：新实例/进程重启计数不丢）
        self.reload()

    def _load(self) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
        """读取文件（损坏/缺文件 → 空字典，不阻断导入）。

        旧版全局格式 {kind: {key: rec}} 与新版 {kind: {key: {owner: rec}}}
        兼容：顶层 value 直接含记录键（count/archive_id 等）判为旧版，整体迁入
        _LEGACY_OWNER 槽（无 sk 无法归属；保留审计，不再参与建档判定）。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        data: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        for kind, entries in raw.items():
            if not isinstance(entries, dict):
                continue
            for key, val in entries.items():
                if not isinstance(val, dict):
                    continue
                owners: dict[str, dict[str, Any]] = {}
                if any(k in _KEYS for k in val):
                    # 旧版全局扁平记录 → legacy 槽
                    owners[_LEGACY_OWNER] = {k: val.get(k) for k in _KEYS}
                else:
                    for ow, rec in val.items():
                        if not isinstance(rec, dict):
                            continue
                        owners[ow] = {k: rec.get(k) for k in _KEYS}
                data.setdefault(kind, {})[key] = owners
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

    def record(self, kind: str, key: str, owner: str = DEFAULT_OWNER) -> dict[str, Any]:
        """原子自增一次并刷新 last_seen；返回该键最新记录。

        语义：按单计（一单一次，与「出现次数」口径一致，见 T19 同批去重）；
        按 owner 隔离累计（不同 sk 各自计数）。
        """
        with self._lock:
            rec = self._data.setdefault(kind, {}).setdefault(key, {}).setdefault(owner, {})
            rec["count"] = int(rec.get("count") or 0) + 1
            rec["last_seen"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def get(
        self, kind: str, key: str, owner: str = DEFAULT_OWNER
    ) -> dict[str, Any] | None:
        """只读当前记录（无记录 → None）。"""
        with self._lock:
            rec = self._data.get(kind, {}).get(key, {}).get(owner)
            return dict(rec) if rec else None

    def _owner_rec(self, kind: str, key: str, owner: str) -> dict[str, Any]:
        """取（kind, key, owner）记录，缺省建空（锁内调用）。"""
        return self._data.setdefault(kind, {}).setdefault(key, {}).setdefault(owner, {})

    def set_archive(
        self, kind: str, key: str, archive_id: str | int, owner: str = DEFAULT_OWNER
    ) -> dict[str, Any]:
        """登记建档结果（archive_id + archived_at）；返回该键最新记录。"""
        with self._lock:
            rec = self._owner_rec(kind, key, owner)
            rec["archive_id"] = str(archive_id)
            rec["archived_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["last_seen"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def mark_exists_external(
        self, kind: str, key: str, owner: str = DEFAULT_OWNER
    ) -> dict[str, Any]:
        """T27b：登记「外部已存在」标记（TMS 204 已存在拒单，无 id 可取）。

        语义：档案在 TMS 已存在（存量档案），本服务无查询接口无法取 id——
        登记 exists_external=true 后后续批次不再尝试建档（避免每次都 204 重试）；
        订单继续文本提交（现状行为，不阻塞）。按 owner 隔离：不同 sk 各自
        TMS 空间独立，A 用户已存在不代表 B 用户已存在。
        """
        with self._lock:
            rec = self._owner_rec(kind, key, owner)
            rec["exists_external"] = True
            rec["last_seen"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def mark_skip_archive(
        self, kind: str, key: str, owner: str = DEFAULT_OWNER
    ) -> dict[str, Any]:
        """登记「本侧不可建档」终态标记（如司机无车牌——TMS AddCarDriver 必填 num）。

        语义：数据缺失导致永久无法建档（非暂时性失败），登记 skip_archive=true 后
        后续批次不再尝试（避免每批都发注定被拒的请求）；订单标注保留（未建档提示
        仍可见），一旦数据补全（新键）即恢复正常建档。
        """
        with self._lock:
            rec = self._owner_rec(kind, key, owner)
            rec["skip_archive"] = True
            rec["last_seen"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def snapshot(self) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
        """全量快照（报告 TOP 清单/测试断言用）；结构 kind → key → owner → rec。"""
        with self._lock:
            return {
                kind: {
                    key: {ow: dict(rec) for ow, rec in owners.items()}
                    for key, owners in entries.items()
                }
                for kind, entries in self._data.items()
            }


def store_path() -> Path:
    """计数存储文件路径：{storage_dir}/master_data.json（测试可 monkeypatch）。"""
    return settings.storage_dir / "master_data.json"


# 进程内单例（惰性；测试用 reload_store 重置路径与内存）
_STORE: MasterDataStore | None = None


def get_store() -> MasterDataStore:
    """全局计数存储单例（惰性初始化）。"""
    global _STORE
    if _STORE is None:
        _STORE = MasterDataStore(store_path())
    return _STORE


def reload_store(path: Path | None = None) -> MasterDataStore:
    """重置存储实例（测试用；path 注入时使用指定路径）。"""
    global _STORE
    _STORE = MasterDataStore(path or store_path())
    return _STORE
