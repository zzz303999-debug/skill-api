"""注册表测试：first-write-wins、键规范化、损坏容错、原子写。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.orders.manifest.imported_registry import (
    ManifestRegistry,
    get_manifest_registry,
    lock_for,
    normalize,
)


class TestNormalize:
    def test_strip_and_upper(self):
        assert normalize(" sitgbaqi005920 ") == "SITGBAQI005920"
        assert normalize(None) is None
        assert normalize("") == ""


class TestRegistrySemantics:
    def test_register_first_wins(self, tmp_path):
        reg = ManifestRegistry(tmp_path / "reg.json")
        reg.register("sITgbaqi005920", sn="11801")
        reg.register("SITGBAQI005920", sn="99999")  # 同键（规范化后）二次登记
        rec = reg.lookup("sitgbaqi005920")
        assert rec["sn"] == "11801"  # first-write-wins，sn 不回退

    def test_lookup_missing_returns_none(self, tmp_path):
        reg = ManifestRegistry(tmp_path / "reg.json")
        assert reg.lookup("UNKNOWN1") is None
        assert reg.lookup(None) is None

    def test_register_requires_key(self, tmp_path):
        reg = ManifestRegistry(tmp_path / "reg.json")
        with pytest.raises(ValueError):
            reg.register(None, sn="1")

    def test_corrupted_file_tolerated(self, tmp_path):
        path = tmp_path / "reg.json"
        path.write_text("{corrupted", encoding="utf-8")
        reg = ManifestRegistry(path)
        assert reg.snapshot() == {}
        reg.register("BL1", sn="1")
        assert reg.lookup("BL1")["sn"] == "1"

    def test_persist_across_reload(self, tmp_path):
        path = tmp_path / "reg.json"
        ManifestRegistry(path).register("BL1", sn="11801")
        reg2 = ManifestRegistry(path)
        assert reg2.lookup("BL1")["sn"] == "11801"

    def test_clear(self, tmp_path):
        reg = ManifestRegistry(tmp_path / "reg.json")
        reg.register("BL1", sn="1")
        reg.clear()
        assert reg.snapshot() == {}

    def test_lock_for_different_keys_independent(self):
        """per-bl_no 锁：异键互不阻塞（同键串行化由锁本身保证，勿嵌套同键获取——
        threading.Lock 不可重入）。"""
        with lock_for("BL1"):
            with lock_for("BL2"):  # 异键：可顺序获取
                assert True


class TestSingleton:
    def test_get_reload(self, tmp_path):
        reg = get_manifest_registry()
        assert isinstance(reg, ManifestRegistry)
        reloaded = get_manifest_registry()
        assert reloaded.path == reg.path  # 单例复用

    def test_reload_injects_path(self, tmp_path):
        injected = tmp_path / "injected.json"
        reloaded = get_manifest_registry()  # noqa: F841
        from app.orders.manifest import imported_registry

        new_reg = imported_registry.reload_registry(injected)
        assert new_reg.path == injected
        assert isinstance(new_reg.path, Path)
