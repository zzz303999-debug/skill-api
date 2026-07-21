from __future__ import annotations

import pytest

from app.config import settings
from app.document_parsers.mineru import MinerUError, _extract_markdown
from app.errors import ConvertError
from app.skills.tuoshu import convert_service


def test_extract_markdown_from_nested_mineru_response():
    payload = {"results": {"order": {"md_content": "# 托书\n\n内容"}}}

    assert _extract_markdown(payload) == "# 托书\n\n内容"


def test_pdf_prefers_mineru(monkeypatch):
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(settings, "mineru_fallback_enabled", True)
    monkeypatch.setattr(
        convert_service.mineru,
        "parse_pdf",
        lambda _file_bytes, _filename: "# MinerU Markdown",
    )

    converted = convert_service.convert_to_markdown(b"pdf", "order.pdf")

    assert converted == "# MinerU Markdown"
    assert converted.parser == "mineru"
    assert converted.parser_fallback is False


def test_pdf_falls_back_to_original_converter(monkeypatch):
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(settings, "mineru_fallback_enabled", True)

    def fail_mineru(_file_bytes, _filename):
        raise MinerUError("unavailable")

    def fake_pdf_converter(_path):
        print("# pdfplumber Markdown")

    monkeypatch.setattr(convert_service.mineru, "parse_pdf", fail_mineru)
    monkeypatch.setitem(convert_service._DISPATCH, ".pdf", fake_pdf_converter)

    converted = convert_service.convert_to_markdown(b"pdf", "order.pdf")

    assert converted.strip() == "# pdfplumber Markdown"
    assert converted.parser == "pdfplumber"
    assert converted.parser_fallback is True


def test_pdf_can_disable_fallback(monkeypatch):
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(settings, "mineru_fallback_enabled", False)

    def fail_mineru(_file_bytes, _filename):
        raise MinerUError("unavailable")

    monkeypatch.setattr(convert_service.mineru, "parse_pdf", fail_mineru)

    with pytest.raises(ConvertError, match="MinerU convert failed"):
        convert_service.convert_to_markdown(b"pdf", "order.pdf")
