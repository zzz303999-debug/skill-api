"""竞品账单导入「成功单注册表」：(提单号+箱号, sk) → 首次创建回执（重复上传去重）。

方案一（2026-08-31 起按 sk 维度）：同一提单号对同一上传人（sk）只允许创建
成功一次——同一 sk 重复上传时已成功单跳过（create_result 标记 skipped），
失败单不登记、可修正后重导；不同 sk（不同操作员）各自可导入同一账单
（生产误拦修正：此前全局按提单号拦截，A 创建后 B 无法导入）。

一行一票（2026-08-31 业务拍板）：同提单号多行各自成单，去重键改为
**提单号+箱号组合键**（`dedup_key`）；行无箱号时退化为纯提单号（历史行为）。
历史登记的纯提单号键保留：对无箱号行继续生效，对含箱号行自然失配。

- 存储：{storage_dir}/imported_orders.json（单文件 JSON；进程内锁 + 临时文件原子替换，
  复用 master_data_store/fee_registry 的持久化模式——JSON + 进程锁 + 原子写，
  无 SQLite/DB 设施）；结构 data[dedup_key] = {owner_key: {sn, source_sha256, created_at}}；
- owner_key：sha256(sk) 前 16 hex（不落盘 sk 原文）；旧版全局条目（顶层含 sn）
  加载时迁移至 "legacy" 槽位（真实 key 为 hex 不会撞名）——legacy 不匹配任何
  sk，存量记录保留可审计且不再拦截（生产误拦数据由此自然解封）；
- 语义：**只登记创建成功单**（submit 返回 success 才调用 register；失败/未尝试
  不登记，保证修正后重导失败单不被误拦）；
- 键规范化 normalize()：strip 首尾空白 + upper 统一大小写（假设下游 TMS 不区分
  提单号大小写；若下游敏感，改为仅 strip）；
- 并发：组合键锁 alock_for（asyncio.Lock）供 async 编排层包住
  「查重→提交→登记」临界区——同键跨请求串行化（check-then-act 原子化），
  不同键互不阻塞（临界区内 await 下游不阻塞事件循环）；
- 局限：单进程部署有效（同 master_data 计数存储）；文件只增不减，清理方式为
  删除文件全量重置（运维操作）；sk 为会话 token，同账号重新登录后 sk 变化
  → 去重按会话维度生效（换号/重登可重导，属本方案既定语义）；下游「已创建
  但响应超时」假失败兜不住（本地无记录），由 TMS 查重（方案二）补强。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging_conf import get_logger

log = get_logger(__name__)

# 注册记录键：sn=首次创建 TMS 业务编号 / source_sha256=来源文件哈希（溯源）/
# created_at=登记时间（ISO 串）
_KEYS: tuple[str, ...] = ("sn", "source_sha256", "created_at")

# 旧版全局条目迁移槽位：真实 owner_key 为 16 位 hex，不会与字面量撞名；
# legacy 记录不匹配任何 sk → 存量记录保留可审计且不再拦截
_LEGACY_OWNER = "legacy"


def owner_key(sk: str) -> str:
    """sk → 去重维度键：sha256 前 16 hex（注册表不落盘 token 原文）。"""
    return hashlib.sha256((sk or "").encode("utf-8")).hexdigest()[:16]


def normalize(bl_no: str | None) -> str | None:
    """去重键规范化：strip 首尾空白 + upper 统一大小写；空值原样返回。"""
    if not bl_no:
        return bl_no
    return str(bl_no).strip().upper()


# 组合键分隔符：真实提单号/箱号字符集（字母数字）不含该字符，解析无歧义；
# 行序号段加 "#" 前缀（纯数字箱号与行序号键不撞车）
_CONTAINER_SEP = "|"
_SEQ_PREFIX = "#"


def dedup_key(
    bl_no: str | None, container_no: str | None = None, fallback: str | None = None
) -> str | None:
    """去重组合键：提单号+箱号（一行一票，2026-08-31）；行序号兜底；末档纯提单号。

    - 有箱号 → ``提单号|箱号``；
    - 无箱号有行序号（fallback）→ ``提单号|#行序号``（2026-08-31 用户拍板：
      金科信等无箱号模板同号多行各自成键，保证每行都能录入且同文件重传可拦）；
    - 双缺 → 纯提单号（历史行为）。
    两段各自 normalize（strip + upper）；提单号缺失 → None（不构成键）。
    """
    bl = normalize(bl_no)
    if not bl:
        return None
    box = normalize(container_no)
    if box:
        return f"{bl}{_CONTAINER_SEP}{box}"
    seq = normalize(fallback)
    if seq:
        return f"{bl}{_CONTAINER_SEP}{_SEQ_PREFIX}{seq}"
    return bl


class ImportedOrderRegistry:
    """单文件成功单注册表（进程内锁 + 原子写；单进程部署下精确）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        # 启动即加载磁盘数据（跨批次持久化：新实例/进程重启登记不丢）
        self.reload()

    @staticmethod
    def _record(item: Any) -> dict[str, Any] | None:
        """磁盘条目 → 归一记录（非对象 → None 丢弃）。"""
        if not isinstance(item, dict):
            return None
        return {
            "sn": item.get("sn"),
            "source_sha256": str(item.get("source_sha256") or "") or None,
            "created_at": str(item.get("created_at") or "") or None,
        }

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        """读取文件（损坏/缺文件 → 空字典，不阻断导入）。

        旧版全局条目（顶层含 sn 键）迁移至 legacy 槽位：保留溯源数据，
        不匹配任何 sk → 存量记录不再拦截（生产误拦自然解封）。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        data: dict[str, dict[str, dict[str, Any]]] = {}
        for bl_no, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            if "sn" in rec:  # 旧版全局条目（单记录）→ legacy 槽位
                migrated = self._record(rec)
                if migrated is not None:
                    data[str(bl_no)] = {_LEGACY_OWNER: migrated}
                continue
            owners: dict[str, dict[str, Any]] = {}
            for owner, item in rec.items():
                migrated = self._record(item)
                if migrated is not None:
                    owners[str(owner)] = migrated
            data[str(bl_no)] = owners
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
        self,
        bl_no: str,
        owner: str,
        container_no: str | None = None,
        fallback: str | None = None,
    ) -> dict[str, Any] | None:
        """(组合键, owner) → 首次创建回执（未登记/空键 → None）。键自动规范化。"""
        key = dedup_key(bl_no, container_no, fallback)
        if not key or not owner:
            return None
        with self._lock:
            owners = self._data.get(key)
            rec = owners.get(owner) if owners else None
            return dict(rec) if rec else None

    def register(
        self,
        bl_no: str,
        owner: str,
        sn: str | None = None,
        source_sha256: str | None = None,
        container_no: str | None = None,
        fallback: str | None = None,
    ) -> dict[str, Any]:
        """登记创建成功单（首次 sn + 来源文件哈希 + 时间）；返回最新记录。

        只在提交成功后调用（本模块不判断成败）；同 (组合键, owner) 重复登记
        以首次为准（sn 不回退，幂等）；不同 owner 各自独立登记。
        """
        key = dedup_key(bl_no, container_no, fallback)
        if not key:
            raise ValueError("bl_no is required for register")
        if not owner:
            raise ValueError("owner is required for register")
        with self._lock:
            owners = self._data.setdefault(key, {})
            rec = owners.setdefault(owner, {})
            if "sn" not in rec:
                rec["sn"] = sn
            rec["source_sha256"] = str(source_sha256 or "") or None
            rec["created_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save()
            return dict(rec)

    def snapshot(self) -> dict[str, dict[str, dict[str, Any]]]:
        """全量快照（测试断言/运维排查用）：{bl_no: {owner: record}}。"""
        with self._lock:
            return {
                bl_no: {owner: dict(rec) for owner, rec in owners.items()}
                for bl_no, owners in self._data.items()
            }

    def clear(self) -> None:
        """全量清空（运维重置；TMS 手工删单后重导先执行）。"""
        with self._lock:
            self._data = {}
            self._save()


# ---- 组合键异步锁（async 编排层用，2026-09 异步化改造）----
# 临界区内含下游网络调用（查重→提交→登记），必须用 asyncio.Lock：
# await 让出事件循环时不阻塞异键协程；同键仍串行（原子性不变）。
# 字典 setdefault 在单线程事件循环内无竞态。
_ASYNC_LOCKS: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def alock_for(
    bl_no: str, container_no: str | None = None, fallback: str | None = None
) -> AsyncIterator[None]:
    """按去重组合键取互斥锁（async with）：同键串行化，异键互不阻塞。"""
    key = dedup_key(bl_no, container_no, fallback)
    lock = _ASYNC_LOCKS.setdefault(key or "", asyncio.Lock())
    async with lock:
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
