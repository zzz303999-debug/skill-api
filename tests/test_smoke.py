"""基本冒烟测试：不联网跑。"""

from __future__ import annotations

import os

os.environ.setdefault("OPENCLAW_API_KEY", "dummy")

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "tuoshu" in body["skills"]


def test_list_skills():
    r = client.get("/skills")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert "tuoshu" in names


def test_unauthorized_extract():
    # 无 X-API-Key，默认 API_KEY=change-me-in-prod（从 config.py 默认）
    r = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"fake", "application/octet-stream")},
    )
    assert r.status_code == 401


def test_unknown_skill():
    r = client.post(
        "/skills/does-not-exist/extract",
        files={"file": ("a.xlsx", b"fake", "application/octet-stream")},
        headers={"X-API-Key": "change-me"},
    )
    assert r.status_code == 404
