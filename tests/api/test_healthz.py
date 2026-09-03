"""/healthz 依赖检查：配置级检查 + 可达性探测（不调 chat、不下单）。"""

from __future__ import annotations

import asyncio
import http.server
import threading

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import (
    _probe_dependencies,
    _probe_llm,
    _probe_mineru,
    _probe_order_config,
    app,
)


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    """本地假网关：任意 GET 返回 200。"""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:  # noqa: A002
        pass


@pytest.fixture()
def probe_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=2)


def test_healthz_disabled_probe_shape():
    """探测关闭（conftest 默认）：200、status=ok、dependencies 全 skipped。"""
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "tuoshu" in body["skills"]
    assert body["dependencies"] == {
        "llm": "skipped",
        "mineru": "skipped",
        "order_api": "skipped",
    }


def test_healthz_with_probe_enabled(monkeypatch, probe_server):
    """开启探测且依赖全部可达：dependencies 全 ok。"""
    monkeypatch.setattr(settings, "health_probe_enabled", True)
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", probe_server)
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(settings, "mineru_base_url", probe_server)

    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["dependencies"] == {
        "llm": "ok",
        "mineru": "ok",
        "order_api": "ok",
    }


def test_llm_not_configured_without_key(monkeypatch):
    """缺 LLM key：标记 not_configured，不发起网络请求。"""
    monkeypatch.setattr(settings, "llm_api_key", "")
    assert asyncio.run(_probe_llm()) == "not_configured"


def test_llm_reachable(monkeypatch, probe_server):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", probe_server)
    assert asyncio.run(_probe_llm()) == "ok"


def test_llm_unreachable(monkeypatch):
    """指向未监听端口：连接失败视为 unreachable。"""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1")
    assert asyncio.run(_probe_llm()) == "unreachable"


def test_mineru_disabled_when_not_enabled(monkeypatch):
    monkeypatch.setattr(settings, "mineru_enabled", False)
    assert asyncio.run(_probe_mineru()) == "disabled"


def test_mineru_reachable(monkeypatch, probe_server):
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(settings, "mineru_base_url", probe_server)
    assert asyncio.run(_probe_mineru()) == "ok"


def test_order_config_ok(monkeypatch):
    monkeypatch.setattr(settings, "order_api_url", "https://orders.example/create")
    assert _probe_order_config() == "ok"


def test_order_config_missing_url(monkeypatch):
    monkeypatch.setattr(settings, "order_api_url", "")
    assert _probe_order_config() == "not_configured"


def test_probe_dependencies_disabled(monkeypatch):
    """探测关闭时 _probe_dependencies 直接返回 skipped（零网络）。"""
    monkeypatch.setattr(settings, "health_probe_enabled", False)
    assert asyncio.run(_probe_dependencies()) == {
        "llm": "skipped",
        "mineru": "skipped",
        "order_api": "skipped",
    }
