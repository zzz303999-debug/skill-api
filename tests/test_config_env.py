"""Settings 环境变量映射测试（APP_ENV alias，2026-08-31 事故回归）。

背景：字段 ``env`` 此前无 alias，pydantic-settings 按字段名读环境变量
``ENV``，文档口径 ``APP_ENV`` 完全无效——生产实际永远跑 test 口径的
费目映射表。回归点：APP_ENV 必须是唯一生效的配置入口。
"""

from __future__ import annotations

from app.config import Settings


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
