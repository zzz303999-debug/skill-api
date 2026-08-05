"""全局 pytest 配置：隔离请求访问日志，避免测试污染真实 storage 日志。"""

from __future__ import annotations

from collections import deque

import pytest

import app.access_log as access_log
from app.config import settings


@pytest.fixture(autouse=True)
def _isolate_request_log(tmp_path, monkeypatch):
    """所有测试的请求日志重定向到临时目录并重置内存缓冲。"""
    monkeypatch.setattr(access_log, "_request_log_path", tmp_path / "requests.jsonl")
    monkeypatch.setattr(
        access_log, "_entries", deque(maxlen=access_log._MAX_MEMORY_ENTRIES)
    )
    monkeypatch.setattr(access_log, "_loaded", False)
    # 测试环境不受本地 .env 的 API_KEY 影响（鉴权默认关闭）
    monkeypatch.setattr(settings, "api_key", "")
