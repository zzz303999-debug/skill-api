"""费目自举注册表测试：owner 维度隔离（2026-09-08 费目隔离拍板）——
双 owner 各自建档互不串扰、exists_external 不跨 owner、旧版全局条目加载
迁移 _LEGACY_OWNER 槽保留审计不再参与判定、status 重启保留（旧版丢键修复）、
损坏容错与快照过滤。"""

from __future__ import annotations

import json

import pytest

from app.orders.bill.fees.fee_registry import _LEGACY_OWNER, FeeRegistry
from app.orders.bill.submission.imported_registry import owner_key

# 测试用维度键（真实链路由 owner_key(sk) 计算，这里固定可读）
OWNER_A = owner_key("sk-a")
OWNER_B = owner_key("sk-b")


@pytest.fixture()
def registry(tmp_path) -> FeeRegistry:
    return FeeRegistry(tmp_path / "fee_registry.json")


class TestOwnerIsolation:
    def test_register_isolated_per_owner(self, registry):
        """A 建档 B 查不到（各自建档各自持有 price_id，不回退全局）。"""
        registry.register("waiting", 90001, "待时费", OWNER_A)
        assert registry.lookup("waiting", OWNER_A)["price_id"] == 90001
        assert registry.lookup("waiting", OWNER_B) is None

    def test_same_code_both_owners_registered(self, registry):
        """同码双 owner 各自建档（同名费目挂各账号档案，price_id 独立）。"""
        registry.register("waiting", 90001, "待时费", OWNER_A)
        registry.register("waiting", 91001, "待时费", OWNER_B)
        assert registry.lookup("waiting", OWNER_A)["price_id"] == 90001
        assert registry.lookup("waiting", OWNER_B)["price_id"] == 91001

    def test_exists_external_not_cross_owner(self, registry):
        """A 撞名（204 已存在）不影响 B 自举。"""
        registry.mark_exists_external("waiting", "待时费", OWNER_A)
        assert registry.exists_external("waiting", OWNER_A) is True
        assert registry.exists_external("waiting", OWNER_B) is False
        assert registry.lookup("waiting", OWNER_B) is None

    def test_exists_external_blocks_owner_lookup(self, registry):
        """已存在标记后该 owner 无 price_id：lookup 仍 None（费用降级语义）。"""
        registry.register("waiting", 90001, "待时费", OWNER_A)
        registry.mark_exists_external("waiting", "待时费", OWNER_A)
        assert registry.lookup("waiting", OWNER_A) is None

    def test_default_owner_slot_for_direct_calls(self, registry):
        """无 owner 直调（测试/异常路径）落 DEFAULT_OWNER 槽。"""
        registry.register("waiting", 90001, "待时费")
        assert registry.lookup("waiting")["price_id"] == 90001
        assert registry.lookup("waiting", OWNER_A) is None

    def test_lookup_returns_copy(self, registry):
        """lookup 返回副本：调用方改返回值不得污染内部状态。"""
        registry.register("waiting", 90001, "待时费", OWNER_A)
        rec = registry.lookup("waiting", OWNER_A)
        rec["price_id"] = 1
        assert registry.lookup("waiting", OWNER_A)["price_id"] == 90001


class TestLegacyMigration:
    def test_old_global_format_migrated_to_legacy(self, tmp_path):
        """旧版全局扁平 {code: rec} → 整体迁 _LEGACY_OWNER 槽。"""
        path = tmp_path / "fee_registry.json"
        path.write_text(
            json.dumps(
                {
                    "waiting": {
                        "price_id": 124844,
                        "tms_name": "待时费",
                        "created_at": "2026-08-14T08:16:40Z",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        registry = FeeRegistry(path)
        # legacy 槽保留审计
        assert registry.snapshot(owner=_LEGACY_OWNER)["waiting"]["price_id"] == 124844
        # 不参与任何 owner 判定（各 owner 槽自举重建）
        assert registry.lookup("waiting", OWNER_A) is None
        assert registry.exists_external("waiting", OWNER_A) is False

    def test_old_flat_format_status_migrated_to_legacy(self, tmp_path):
        """线上真实旧形态（扁平 {code: rec} 含 status=exists_external、price_id
        null）加载：整表迁 _legacy 槽，status 保留不复活重试，也不误拦新 owner。"""
        path = tmp_path / "fee_registry.json"
        path.write_text(
            json.dumps(
                {
                    "waiting": {
                        "price_id": None,
                        "tms_name": "待时费",
                        "created_at": "2026-09-01T03:00:00Z",
                        "status": "exists_external",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        registry = FeeRegistry(path)
        legacy = registry.snapshot(owner=_LEGACY_OWNER)["waiting"]
        # 旧终态留档审计（status 不再被丢键——旧版 _load 缺陷修复）
        assert legacy["status"] == "exists_external"
        assert legacy["price_id"] is None
        # 不参与任何 owner 判定：真实 owner 不复活重试、也不误拦新 owner 自举
        assert registry.exists_external("waiting", OWNER_A) is False
        assert registry.lookup("waiting", OWNER_A) is None

    def test_new_format_roundtrip(self, tmp_path):
        """新版 {code: {owner: rec}} 原样往返。"""
        path = tmp_path / "fee_registry.json"
        registry = FeeRegistry(path)
        registry.register("waiting", 90001, "待时费", OWNER_A)
        registry.mark_exists_external("other", "其它费", OWNER_B)
        reloaded = FeeRegistry(path)
        assert reloaded.lookup("waiting", OWNER_A)["price_id"] == 90001
        assert reloaded.exists_external("other", OWNER_B) is True

    def test_corrupted_file_tolerated(self, tmp_path):
        """损坏/非 dict → 空表不阻断。"""
        path = tmp_path / "fee_registry.json"
        path.write_text("not-json", encoding="utf-8")
        registry = FeeRegistry(path)
        assert registry.lookup("waiting", OWNER_A) is None
        path.write_text("[1, 2]", encoding="utf-8")
        assert FeeRegistry(path).lookup("waiting", OWNER_A) is None


class TestSnapshot:
    def test_snapshot_owner_filter(self, registry):
        registry.register("waiting", 90001, "待时费", OWNER_A)
        registry.register("other", 90002, "其它费", OWNER_B)
        only_a = registry.snapshot(owner=OWNER_A)
        assert set(only_a) == {"waiting"}
        full = registry.snapshot()
        assert set(full["waiting"]) == {OWNER_A}
        assert set(full["other"]) == {OWNER_B}
