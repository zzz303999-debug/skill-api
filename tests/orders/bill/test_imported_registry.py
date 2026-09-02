"""成功单注册表测试：register/lookup/snapshot/clear、损坏容错、键规范化
（strip + upper）、组合键去重（提单号+箱号，行序号兜底）、per-key 锁同键
互斥异键并行（重复上传去重方案一，2026-08-31 起按 (组合键, sk) 维度：
同 owner 幂等、异 owner 放行、旧版全局条目迁移 legacy 槽位不再拦截）。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.orders.bill.imported_registry import (
    ImportedOrderRegistry,
    alock_for,
    dedup_key,
    normalize,
    owner_key,
)

# 测试用去重维度键（真实链路由 owner_key(sk) 计算，这里固定可读）
pytestmark = pytest.mark.asyncio

# 测试用去重维度键（真实链路由 owner_key(sk) 计算，这里固定可读）
OWNER_A = owner_key("sk-a")
OWNER_B = owner_key("sk-b")


@pytest.fixture()
def registry(tmp_path) -> ImportedOrderRegistry:
    return ImportedOrderRegistry(tmp_path / "imported_orders.json")


class TestNormalize:
    def test_strip_and_upper(self):
        assert normalize("  oolu12345  ") == "OOLU12345"
        assert normalize("oolu12345") == "OOLU12345"

    def test_empty_unchanged(self):
        assert normalize(None) is None
        assert normalize("") == ""


class TestOwnerKey:
    def test_deterministic_and_distinct(self):
        """同 sk 稳定、异 sk 不同（去重维度键的前提）。"""
        assert owner_key("sk-a") == OWNER_A
        assert owner_key("sk-a") != OWNER_B
        assert len(OWNER_A) == 16

    def test_empty_sk_still_hashed(self):
        """空 sk 也产生稳定键（防御：create 模式 sk 缺失已被上层 400 拦截）。"""
        assert owner_key("") == owner_key("")
        assert owner_key("") != OWNER_A


class TestRegistry:
    def test_register_and_lookup(self, registry):
        rec = registry.register("OOLU12345", OWNER_A, sn="EX1", source_sha256="abc")
        assert rec["sn"] == "EX1"
        assert rec["source_sha256"] == "abc"
        assert rec["created_at"]
        # 查询键大小写/首尾空白不敏感（与登记键规范化一致）
        found = registry.lookup("  oolu12345 ", OWNER_A)
        assert found["sn"] == "EX1"

    def test_lookup_miss(self, registry):
        assert registry.lookup("NOPE", OWNER_A) is None
        assert registry.lookup("", OWNER_A) is None
        assert registry.lookup("OOLU1", "") is None

    def test_lookup_returns_copy(self, registry):
        """lookup 返回副本：调用方修改返回值不得污染注册表内部状态。"""
        registry.register("OOLU1", OWNER_A, sn="EX1")
        rec = registry.lookup("OOLU1", OWNER_A)
        rec["sn"] = "HACKED"
        assert registry.lookup("OOLU1", OWNER_A)["sn"] == "EX1"

    def test_register_idempotent_sn_kept(self, registry):
        """同 (bl_no, owner) 重复登记以首次为准（sn 不回退，并发下后到者不覆盖）。"""
        registry.register("OOLU1", OWNER_A, sn="EX1")
        registry.register("OOLU1", OWNER_A, sn="EX2")
        assert registry.lookup("OOLU1", OWNER_A)["sn"] == "EX1"

    def test_different_owner_same_bl_allowed(self, registry):
        """同提单号异 owner 各自独立登记/查询（2026-08-31 起：不同操作员可各导一次）。"""
        registry.register("OOLU1", OWNER_A, sn="EX1")
        assert registry.lookup("OOLU1", OWNER_B) is None  # 异 owner 不命中
        rec_b = registry.register("OOLU1", OWNER_B, sn="EX2")
        assert rec_b["sn"] == "EX2"  # 异 owner 首登不被 first-write-wins 拦截
        assert registry.lookup("OOLU1", OWNER_A)["sn"] == "EX1"  # A 的记录不回退
        snapshot = registry.snapshot()
        assert set(snapshot["OOLU1"]) == {OWNER_A, OWNER_B}

    def test_register_requires_key(self, registry):
        with pytest.raises(ValueError):
            registry.register("", OWNER_A, sn="EX1")
        with pytest.raises(ValueError):
            registry.register("OOLU1", "", sn="EX1")

    def test_snapshot_and_clear(self, registry):
        registry.register("A1", OWNER_A, sn="EX1")
        registry.register("B2", OWNER_B, sn="EX2")
        assert set(registry.snapshot()) == {"A1", "B2"}
        registry.clear()
        assert registry.snapshot() == {}

    def test_corrupt_file_tolerated(self, tmp_path):
        """文件损坏/非对象 → 空字典加载，不阻断登记（对齐 fee_registry 容错）。"""
        path = tmp_path / "imported_orders.json"
        path.write_text("{corrupt json", encoding="utf-8")
        reg = ImportedOrderRegistry(path)
        assert reg.snapshot() == {}
        reg.register("OOLU1", OWNER_A, sn="EX1")  # 损坏文件上可继续登记（原子写修复）
        assert reg.lookup("OOLU1", OWNER_A)["sn"] == "EX1"

    def test_reload_from_disk(self, tmp_path):
        """新实例（进程重启语义）加载磁盘登记，跨批次持久化。"""
        path = tmp_path / "imported_orders.json"
        ImportedOrderRegistry(path).register("OOLU1", OWNER_A, sn="EX1")
        reg2 = ImportedOrderRegistry(path)
        assert reg2.lookup("OOLU1", OWNER_A)["sn"] == "EX1"

    def test_legacy_global_entry_not_blocking(self, tmp_path):
        """旧版全局条目（顶层含 sn）加载 → 迁移 legacy 槽位，不匹配任何 sk。

        生产误拦解封路径：存量记录保留可审计，但任何 owner 查询都不再命中。
        """
        path = tmp_path / "imported_orders.json"
        path.write_text(
            json.dumps(
                {"OOLU1": {"sn": "EX9", "source_sha256": "abc", "created_at": "2026-08-30T00:00:00Z"}}
            ),
            encoding="utf-8",
        )
        reg = ImportedOrderRegistry(path)
        assert reg.lookup("OOLU1", OWNER_A) is None
        assert reg.lookup("OOLU1", OWNER_B) is None
        # legacy 记录保留在快照中（可审计），且同 owner 首登照常登记
        owners = reg.snapshot()["OOLU1"]
        assert owners["legacy"]["sn"] == "EX9"
        reg.register("OOLU1", OWNER_A, sn="EX1")
        assert reg.lookup("OOLU1", OWNER_A)["sn"] == "EX1"

    def test_malformed_owner_entry_dropped(self, tmp_path):
        """新版结构里非对象条目 → 丢弃（对齐旧容错口径）。"""
        path = tmp_path / "imported_orders.json"
        path.write_text(json.dumps({"OOLU1": {OWNER_A: "not-a-dict"}}), encoding="utf-8")
        reg = ImportedOrderRegistry(path)
        assert reg.snapshot() == {"OOLU1": {}}
        assert reg.lookup("OOLU1", OWNER_A) is None


class TestDedupKey:
    """一行一票组合键（2026-08-31）：提单号+箱号；行序号兜底；末档纯提单号。"""

    def test_container_segment(self):
        """有箱号 → 提单号|箱号（两段各自规范化）。"""
        assert dedup_key(" oolu123 ", " tclu1 ") == "OOLU123|TCLU1"

    def test_row_seq_fallback(self):
        """无箱号有行序号 → 提单号|#行序号（# 前缀与纯数字箱号键不撞车）。

        序号段 normalize 仅 strip+upper；浮点尾巴由归集层 row_seq 清洗后传入。
        """
        assert dedup_key("OOLU123", None, "1.0") == "OOLU123|#1.0"
        assert dedup_key("oolu123", None, " 2 ") == "OOLU123|#2"
        assert dedup_key("OOLU123", None, "2") != dedup_key("OOLU123", None, "1")
        # 纯数字箱号键（无 #）与行序号键（有 #）互异
        assert dedup_key("OOLU123", "2") != dedup_key("OOLU123", None, "2")

    def test_bare_bl_no_when_both_missing(self):
        """箱号与行序号双缺 → 纯提单号（历史行为）。"""
        assert dedup_key("OOLU123") == "OOLU123"
        assert dedup_key("OOLU123", None, None) == "OOLU123"
        assert dedup_key("OOLU123", "") == "OOLU123"

    def test_missing_bl_no_no_key(self):
        assert dedup_key(None, "TCLU1") is None
        assert dedup_key("", None, "1") is None

    def test_same_bl_different_containers_isolated(self, registry):
        """同提单号不同箱号各自成键：一行一票多柜互不拦截（登记/查询隔离）。"""
        registry.register("OOLU1", OWNER_A, sn="EX1", container_no="TCLU1")
        registry.register("OOLU1", OWNER_A, sn="EX2", container_no="TCLU2")
        assert registry.lookup("OOLU1", OWNER_A, container_no="TCLU1")["sn"] == "EX1"
        assert registry.lookup("OOLU1", OWNER_A, container_no="TCLU2")["sn"] == "EX2"
        # 无键段查询（纯提单号）不命中含箱号键
        assert registry.lookup("OOLU1", OWNER_A) is None

    def test_same_bl_seq_fallback_isolated(self, registry):
        """无箱号同号多行：行序号兜底成键，各行独立登记/命中。"""
        registry.register("OOLU1", OWNER_A, sn="EX1", fallback="1")
        registry.register("OOLU1", OWNER_A, sn="EX2", fallback="2")
        assert registry.lookup("OOLU1", OWNER_A, fallback="1")["sn"] == "EX1"
        assert registry.lookup("OOLU1", OWNER_A, fallback="2")["sn"] == "EX2"
        assert registry.lookup("OOLU1", OWNER_A, fallback="3") is None

    def test_reupload_same_key_hit(self, registry):
        """同组合键重复登记幂等（first-write-wins）→ 重传同键命中被拦。"""
        registry.register("OOLU1", OWNER_A, sn="EX1", container_no="TCLU1")
        registry.register("OOLU1", OWNER_A, sn="EX9", container_no="TCLU1")
        assert registry.lookup("OOLU1", OWNER_A, container_no="TCLU1")["sn"] == "EX1"


class TestLockFor:
    """per-键异步锁（alock_for，async 编排链路用）：协程并发模型（生产单事件循环）。"""

    async def test_same_key_serialized(self):
        """同提单号：后到协程必须等待持有者释放（check-then-act 原子化的前提）。"""
        events: list[str] = []
        a_in = asyncio.Event()
        a_done = asyncio.Event()

        async def worker_a():
            async with alock_for("OOLU1"):
                events.append("A-in")
                a_in.set()
                await asyncio.wait_for(a_done.wait(), timeout=5)  # A 持锁等待 B 尝试进入
                events.append("A-out")

        async def worker_b():
            async with alock_for("OOLU1"):
                events.append("B-in")
                events.append("B-out")

        ta = asyncio.create_task(worker_a())
        await asyncio.wait_for(a_in.wait(), timeout=2)
        tb = asyncio.create_task(worker_b())
        await asyncio.sleep(0.1)  # 若锁失效，B 会趁机进入临界区
        assert events == ["A-in"]  # B 被锁挡在临界区外
        a_done.set()
        await asyncio.wait_for(ta, timeout=5)
        await asyncio.wait_for(tb, timeout=5)
        assert events == ["A-in", "A-out", "B-in", "B-out"]

    async def test_different_keys_parallel(self):
        """不同提单号互不阻塞（串行提单导入场景冲突率≈0）。"""
        stamp: dict[str, float] = {}

        async def worker(key: str):
            async with alock_for(key):
                stamp[key] = time.monotonic()
                await asyncio.sleep(0.15)  # 拉长临界区：若串行，后到者进入必然延后

        t1 = asyncio.create_task(worker("A"))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(worker("B"))
        await asyncio.wait_for(t1, timeout=5)
        await asyncio.wait_for(t2, timeout=5)
        # B 在 A 的临界区期间进入（并行），而非等待 A 释放后进入（串行）
        assert stamp["B"] - stamp["A"] < 0.1
