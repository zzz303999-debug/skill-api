from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import registry
from app.errors import LLMError, ParseError
from app.llm.openclaw import chat_json
from app.main import app
from app.skills.tuoshu.prompt import format_to_chat_text
from app.skills.tuoshu.schema import TuoshuOutput
from app.skills.tuoshu.skill import TuoshuSkill, _clean_json_schema

client = TestClient(app)


def test_nullable_object_schema_keeps_object_shape():
    schema = _clean_json_schema(TuoshuOutput.model_json_schema())
    factory = schema["properties"]["factory"]

    assert set(factory["type"]) == {"object", "null"}
    assert "name" in factory["properties"]
    assert "address" in factory["properties"]


def test_chat_text_contains_every_container():
    text = format_to_chat_text(
        {
            "containers": [
                {"type": "40HC", "container_no": "CONT001"},
                {"type": "20GP", "container_no": "CONT002"},
            ]
        }
    )

    assert "柜 1:" in text
    assert "柜 2:" in text
    assert "CONT001" in text
    assert "CONT002" in text


def test_batch_extract_calls_keyword_only_skill(monkeypatch):
    skill = registry.get("tuoshu")
    monkeypatch.setattr(settings, "api_key", "test-key")

    def fake_run(*, file_bytes: bytes, filename: str, options=None):
        return {
            "result": {"filename": filename, "size": len(file_bytes)},
            "meta": {"fake": True},
        }

    monkeypatch.setattr(skill, "run", fake_run)
    response = client.post(
        "/skills/tuoshu/batch-extract",
        files=[
            ("files", ("a.xlsx", b"a", "application/octet-stream")),
            ("files", ("b.xlsx", b"bb", "application/octet-stream")),
        ],
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] == 2
    assert body["failed"] == 0
    assert [item["result"]["filename"] for item in body["results"]] == ["a.xlsx", "b.xlsx"]


def test_scanned_pdf_is_sent_as_vision_pages(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    captured: dict = {}
    monkeypatch.setattr(
        skill_module,
        "convert_to_markdown",
        lambda _file_bytes, _filename: "SCAN_OR_IMAGE_HINT: scan.pdf",
    )
    monkeypatch.setattr(
        skill_module,
        "render_pdf_pages",
        lambda _file_bytes, *, max_pages, scale: [b"page-1", b"page-2"],
    )

    def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {"source": {}}, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "chat_json", fake_chat_json)

    TuoshuSkill().run(file_bytes=b"fake-pdf", filename="scan.pdf")

    user_content = captured["messages"][-1]["content"]
    images = [item for item in user_content if item["type"] == "image_url"]
    assert len(images) == 2
    assert all(item["image_url"]["url"].startswith("data:image/png;base64,") for item in images)


def test_upload_limit(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-key")
    monkeypatch.setattr(settings, "api_max_upload_bytes", 3)

    response = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"four", "application/octet-stream")},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "file_too_large"


def test_json_schema_falls_back_to_json_object(monkeypatch):
    calls: list[dict] = []

    def fake_chat(_messages, **kwargs):
        calls.append(kwargs["response_format"])
        if kwargs["response_format"]["type"] == "json_schema":
            raise LLMError(
                "unsupported response format",
                code="llm_response_format_unsupported",
            )
        return '{"ok": true}', {"model": "fake", "usage": None}

    monkeypatch.setattr("app.llm.openclaw.chat", fake_chat)
    data, _meta = chat_json([], json_schema={"type": "object"})

    assert data == {"ok": True}
    assert [call["type"] for call in calls] == ["json_schema", "json_object"]


def test_chat_json_rejects_non_object(monkeypatch):
    monkeypatch.setattr(
        "app.llm.openclaw.chat",
        lambda _messages, **_kwargs: ("[]", {"model": "fake", "usage": None}),
    )

    with pytest.raises(ParseError, match="JSON object"):
        chat_json([])
