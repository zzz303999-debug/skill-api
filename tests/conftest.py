"""全局 pytest 配置：隔离请求访问日志，避免测试污染真实 storage 日志。"""

from __future__ import annotations

from collections import deque

import pytest

import app.core.access_log_store as access_log
import app.core.third_party_log_store as third_party_log
from app.core.config import settings


@pytest.fixture(autouse=True)
def _isolate_third_party_log(tmp_path, monkeypatch):
    """所有测试的第三方调用日志重定向到临时目录并重置内存缓冲。"""
    monkeypatch.setattr(third_party_log, "_log_dir_override", tmp_path / "logs")
    monkeypatch.setattr(
        third_party_log,
        "_entries",
        deque(maxlen=third_party_log._MAX_MEMORY_ENTRIES),
    )
    monkeypatch.setattr(third_party_log, "_loaded", False)


@pytest.fixture(autouse=True)
def _isolate_request_log(tmp_path, monkeypatch):
    """所有测试的请求日志重定向到临时目录并重置内存缓冲。"""
    monkeypatch.setattr(access_log, "_request_log_dir", tmp_path / "logs")
    monkeypatch.setattr(
        access_log, "_entries", deque(maxlen=access_log._MAX_MEMORY_ENTRIES)
    )
    monkeypatch.setattr(access_log, "_loaded", False)
    # 测试环境不受本地 .env 的 API_KEY 影响（鉴权默认关闭）
    monkeypatch.setattr(settings, "api_key", "")
    # 健康检查探测默认关闭，避免测试向真实 LLM/MinerU 网关发起网络请求
    monkeypatch.setattr(settings, "health_probe_enabled", False)


@pytest.fixture(autouse=True)
def _isolate_tms_write_slots():
    """全局 TMS 写通道隔离（跨域共用：账单下单/建档、文本下单、舱单 addBill）。

    信号量首次竞争等待时绑定运行事件循环（pytest-asyncio 每用例新 loop），
    重建避免遗留 waiter 跨用例触发 "bound to a different event loop"。
    """
    import asyncio

    import app.core.tms_gate as tms_gate_mod
    import app.orders.bill.master_data.client as md_client_mod
    import app.orders.bill.submission.client as bill_client_mod
    import app.orders.manifest.submission.client as manifest_client_mod
    import app.orders.text.client as text_client_mod

    def _reset():
        # 宽度恒 1 全串行（TMS 不支持并发写）；各消费模块引用同步重建为同一实例
        slot = asyncio.Semaphore(1)
        tms_gate_mod.tms_write_slots = slot
        md_client_mod.tms_write_slots = slot
        bill_client_mod._create_downstream_slots = slot
        manifest_client_mod.tms_write_slots = slot
        text_client_mod.tms_write_slots = slot

    _reset()
    yield
    _reset()
