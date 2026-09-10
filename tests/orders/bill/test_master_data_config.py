"""真实 master_data 配置文件结构断言（生产事故防回归）。

事故：defaults.client.su_id / defaults.truck.remind_id 曾死写 15478（章家俊），
多用户各自上传建档全部归属错账号。修复：归属键从 defaults 移除，建档请求省略
归属键、由 TMS 按 sk 会话自动分发。本测试直接读真实两份 yaml（prod/test），
防止将来有人把归属键写回 defaults（复发即建档归属错乱）。
"""

from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

# 账号级归属键：只允许经请求方会话（sk）自动分发，禁止落配置文件
_OWNER_KEYS = {"su_id", "remind_id", "su_name", "remind_user", "cu_id"}


def _master_data_defaults(env: str) -> dict:
    path = _CONFIG_DIR / f"master_data.{env}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (data.get("master_data") or {}).get("defaults") or {}


def test_prod_defaults_have_no_owner_keys():
    defaults = _master_data_defaults("prod")
    for kind in ("client", "truck"):
        assert set(defaults.get(kind) or {}) & _OWNER_KEYS == set(), (
            f"master_data.prod.yaml defaults.{kind} 不得含账号级归属键 "
            f"（{sorted(set(defaults.get(kind) or {}) & _OWNER_KEYS)}）——"
            f"归属由 TMS 按 sk 会话自动分发，写死会导致多用户建档归属错乱"
        )


def test_test_defaults_have_no_owner_keys():
    defaults = _master_data_defaults("test")
    for kind in ("client", "truck"):
        assert set(defaults.get(kind) or {}) & _OWNER_KEYS == set(), (
            f"master_data.test.yaml defaults.{kind} 不得含账号级归属键"
        )


def test_defaults_structure_intact():
    """归属键移除不波及其余必要默认值（分组/部门/类型等仍在）。"""
    for env in ("prod", "test"):
        defaults = _master_data_defaults(env)
        client = defaults.get("client") or {}
        truck = defaults.get("truck") or {}
        assert client.get("cg_id") == "4" and client.get("sys_type") == "1"
        assert truck.get("section_id") == "6987" and truck.get("sys_type") == "1"
        assert truck.get("type") == "bill"
