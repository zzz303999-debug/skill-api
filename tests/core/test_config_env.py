"""Settings 环境变量映射测试（APP_ENV alias，2026-08-31 事故回归）。

背景：字段 ``env`` 此前无 alias，pydantic-settings 按字段名读环境变量
``ENV``，文档口径 ``APP_ENV`` 完全无效——生产实际永远跑 test 口径的
费目映射表。回归点：APP_ENV 必须是唯一生效的配置入口。

另含 2026-09 目录迁移事故回归：config.py 迁入 app/core/ 后
_PROJECT_ROOT 层级漏改（见 test_project_root_is_repo_root）。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import _PROJECT_ROOT, Settings

# 测试文件位于 <repo>/tests/ 下，以其位置为仓库根的独立基准
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# 统一跳过 .env 文件：测试只验证进程环境变量映射，不受开发者本地 .env 干扰
def _settings() -> Settings:
    return Settings(_env_file=None)


def test_app_env_alias_binds(monkeypatch):
    """APP_ENV 环境变量必须映射到 settings.env（而不是字段名 ENV）。"""
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("ENV", raising=False)
    assert _settings().env == "prod"


def test_app_env_default_is_test(monkeypatch):
    """未设置 APP_ENV 时默认 test（与 .env.example 注释口径一致）。"""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    assert _settings().env == "test"


def test_plain_env_name_no_longer_binds(monkeypatch):
    """裸 ENV 变量不再被读取（防止与系统级 ENV 变量意外串扰）。"""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("ENV", "prod")
    assert _settings().env == "test"


def test_project_root_is_repo_root():
    """_PROJECT_ROOT 必须解析到仓库根（2026-09 迁移事故回归）。

    背景：config.py 迁入 app/core/ 后目录深度 +1，_PROJECT_ROOT 仍用两级
    parent 解析到 app/——根目录 .env 静默失效、相对 storage_dir 落到
    app/storage（Docker 下绕开挂载卷，容器重建丢注册表）。回归点：以测试
    文件位置为独立基准锚定仓库根，未来再迁移目录时层级漏改直接红。
    """
    assert _PROJECT_ROOT == _REPO_ROOT
    assert (_PROJECT_ROOT / "pyproject.toml").is_file()


def test_default_storage_dir_resolves_under_repo_root(monkeypatch):
    """相对 storage_dir 默认值必须解析到仓库根 storage/（而非 app/storage）。"""
    monkeypatch.delenv("STORAGE_DIR", raising=False)
    assert _settings().storage_dir == _PROJECT_ROOT / "storage"
