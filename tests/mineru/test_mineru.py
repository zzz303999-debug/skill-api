from __future__ import annotations

import asyncio

import pytest

import app.mineru.client as mineru_module
from app.core.config import settings
from app.core.errors import ConvertError
from app.mineru.client import (
    MINERU_REQUEST_PROFILE,
    MinerUContractError,
    MinerUError,
    MinerUParseResult,
    _content_list_to_markdown,
    _extract_markdown,
    _quality_result,
    _strip_markdown_images,
    parse_document,
    parse_pdf,
)
from app.skills.tuoshu import convert_service
from app.skills.tuoshu.convert_service import ConversionText, detect_image_mime


def test_extract_markdown_from_nested_mineru_response():
    payload = {"results": {"order": {"md_content": "# 托书\n\n内容"}}}

    assert _extract_markdown(payload) == "# 托书\n\n内容"


def test_image_format_is_detected_from_content():
    assert detect_image_mime(b"\x89PNG\r\n\x1a\ncontent", "order.png") == (
        "image/png",
        "png",
    )


def test_unknown_image_content_is_rejected():
    from app.core.errors import BadRequestError

    with pytest.raises(BadRequestError, match="not a supported raster format"):
        detect_image_mime(b"not-an-image", "order.png")


def test_image_extension_mismatch_is_rejected():
    from app.core.errors import BadRequestError

    with pytest.raises(BadRequestError, match="does not match"):
        detect_image_mime(b"\x89PNG\r\n\x1a\ncontent", "order.jpg")


def test_mineru_low_confidence_is_explicit_when_structure_and_labels_are_absent():
    result = _quality_result("一段没有业务标签且没有表格结构的普通文本内容", [])

    assert result.low_confidence is True
    assert result.low_confidence_reasons == ("no_table_or_key_labels",)


def test_original_image_is_uploaded_to_mineru(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        headers = {"content-type": "application/json", "x-mineru-version": "2.5.4"}
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"markdown": "| 提单号 | TEST000011 |"}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    image_bytes = b"\x89PNG\r\n\x1a\noriginal"
    monkeypatch.setattr(settings, "mineru_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_expected_version", "2.5.4")
    monkeypatch.setattr(mineru_module.httpx, "Client", FakeClient)

    result = parse_document(image_bytes, "order.png", mime_type="image/png")

    assert result.markdown == "| 提单号 | TEST000011 |"
    assert captured["files"] == {"files": ("order.png", image_bytes, "image/png")}


def test_image_requests_disable_formula_recognition(monkeypatch):
    """图片单据无公式：请求参数关闭 formula_enable 以降低 CPU 耗时；PDF 保留。"""
    captured: dict = {}

    class FakeResponse:
        headers = {"content-type": "application/json", "x-mineru-version": "2.5.4"}
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"markdown": "| 提单号 | TEST000011 |"}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **kwargs):
            captured[kwargs["files"]["files"][0]] = kwargs["data"]
            return FakeResponse()

    monkeypatch.setattr(settings, "mineru_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_expected_version", "2.5.4")
    monkeypatch.setattr(mineru_module.httpx, "Client", FakeClient)

    parse_document(b"image-bytes", "order.jpg", mime_type="image/jpeg")
    parse_document(b"%PDF-bytes", "order.pdf", mime_type="application/pdf")

    assert captured["order.jpg"]["formula_enable"] == "false"
    assert captured["order.pdf"]["formula_enable"] == "true"


def test_mixed_pdf_routes_only_bad_page_to_mineru(monkeypatch):
    import pdfplumber

    class FakePage:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

        def extract_tables(self):
            return []

        def extract_words(self):
            return []

    class FakePdf:
        pages = [FakePage("提单号 船名 件数 " + "有效文本" * 20), FakePage("")]

        def close(self):
            return None

    mineru_calls: list[str] = []

    def fake_mineru(_bytes, filename, **_kwargs):
        mineru_calls.append(filename)
        return MinerUParseResult(markdown="| 提单号 | OCR000001 |", table_count=1)

    monkeypatch.setattr(pdfplumber, "open", lambda _stream: FakePdf())
    monkeypatch.setattr(convert_service, "_render_pdf_page", lambda *_args, **_kwargs: b"png")
    monkeypatch.setattr(convert_service.mineru, "parse_document", fake_mineru)

    result = convert_service._convert_pdf_with_page_routing(b"pdf", "mixed.pdf")

    assert [page.parser for page in result.pages] == ["pdfplumber", "mineru"]
    assert mineru_calls == ["mixed-page-2.png"]


def test_pdf_page_routing_rejects_too_many_vision_pages(monkeypatch):
    import pdfplumber

    class FakePage:
        def extract_text(self):
            return ""

        def extract_tables(self):
            return []

        def extract_words(self):
            return []

    class FakePdf:
        pages = [FakePage(), FakePage(), FakePage()]

        def close(self):
            return None

    monkeypatch.setattr(settings, "vision_max_pdf_pages", 2)
    monkeypatch.setattr(pdfplumber, "open", lambda _stream: FakePdf())
    monkeypatch.setattr(convert_service, "_render_pdf_page", lambda *_args, **_kwargs: b"png")
    monkeypatch.setattr(
        convert_service.mineru,
        "parse_document",
        lambda *_args, **_kwargs: MinerUParseResult(
            markdown="无法辨认",
            low_confidence_reasons=("insufficient_text_blocks",),
        ),
    )

    with pytest.raises(ConvertError) as exc_info:
        convert_service._convert_pdf_with_page_routing(b"pdf", "scan.pdf")

    assert exc_info.value.code == "pdf_page_limit_exceeded"
    assert exc_info.value.details == {"vision_page_count": 3, "max_pages": 2}


def test_low_confidence_image_routes_to_vision_with_blocking_issue(monkeypatch):
    image_bytes = b"\x89PNG\r\n\x1a\nlow-resolution"
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(
        convert_service.mineru,
        "parse_document",
        lambda *_args, **_kwargs: MinerUParseResult(
            markdown="无法辨认",
            low_confidence_reasons=("insufficient_text_blocks", "no_table_or_key_labels"),
        ),
    )

    result = convert_service.convert_image_to_parse_result(image_bytes, "low.png")

    assert result.pages[0].parser == "vision"
    assert result.pages[0].vision_image == image_bytes
    assert result.review_issues() == [
        {
            "code": "mineru_low_confidence",
            "field": "source.pages[0]",
            "message": "MinerU 图片结果低置信，已转 vision，必须人工复核",
            "source_values": ["insufficient_text_blocks", "no_table_or_key_labels"],
            "blocking": True,
        }
    ]


def test_high_confidence_image_keeps_original_as_visual_evidence(monkeypatch):
    image_bytes = b"\x89PNG\r\n\x1a\noriginal"
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(
        convert_service.mineru,
        "parse_document",
        lambda *_args, **_kwargs: MinerUParseResult(
            markdown="备注：出口清关的装完箱后请及时进港 作业资水！",
            table_count=1,
        ),
    )

    result = convert_service.convert_image_to_parse_result(image_bytes, "order.png")

    assert result.pages[0].parser == "mineru"
    assert result.pages[0].confidence == "high"
    assert result.pages[0].markdown.endswith("作业资水！")
    assert result.pages[0].vision_image == image_bytes
    assert result.vision_images == [image_bytes]
    assert result.parser_fallback is False


def test_content_list_discards_image_stamp_and_logo_ocr():
    content_list = [
        {"type": "text", "text": "TO：上海运嘉货运代理有限公司"},
        {"type": "image", "text": "上海秉晟国际物流有限公司 28GSHEN S"},
        {"type": "stamp", "content": "上海秉晟国际物流有限公司"},
        {"type": "table", "table_body": "| 主单号 | 1SHA044022 |"},
    ]

    markdown = _content_list_to_markdown(content_list)

    assert markdown == "TO：上海运嘉货运代理有限公司\n\n| 主单号 | 1SHA044022 |"
    assert "28GSHEN S" not in markdown
    assert "秉晟" not in markdown


def test_content_list_preserves_markdown_heading_levels():
    content_list = [
        {"type": "text", "text_level": 1, "text": "运输委托书"},
        {"type": "title", "level": 2, "text": "箱信息"},
        {"type": "text", "text_level": 0, "text": "40HQ X 1"},
    ]

    assert _content_list_to_markdown(content_list) == (
        "# 运输委托书\n\n## 箱信息\n\n40HQ X 1"
    )


def test_markdown_fallback_removes_image_alt_text():
    markdown = "正文\n\n![上海秉晟国际物流有限公司 28GSHEN S](images/stamp.jpg)\n\n结尾"

    assert _strip_markdown_images(markdown) == "正文\n\n结尾"


def test_mineru_request_profile_and_version_are_pinned(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        headers = {"content-type": "application/json", "x-mineru-version": "2.5.4"}
        content = b"{}"
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"markdown": "# 固定解析结果"}

    class FakeClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(settings, "mineru_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_endpoint", "/file_parse")
    monkeypatch.setattr(settings, "mineru_expected_version", "2.5.4")
    monkeypatch.setattr(mineru_module.httpx, "Client", FakeClient)

    result = parse_pdf(b"pdf", "order.pdf")

    assert result == "# 固定解析结果"
    assert captured["data"] == MINERU_REQUEST_PROFILE
    assert captured["files"] == {"files": ("order.pdf", b"pdf", "application/pdf")}


def test_mineru_missing_version_header_is_accepted(monkeypatch, caplog):
    class FakeResponse:
        headers = {"content-type": "application/json"}
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"markdown": "# MinerU without version header"}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(settings, "mineru_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_expected_version", "2.5.4")
    monkeypatch.setattr(mineru_module.httpx, "Client", FakeClient)

    assert parse_pdf(b"pdf", "order.pdf") == "# MinerU without version header"
    assert "mineru_version_header_missing" in caplog.text


def test_mineru_version_drift_is_a_contract_error(monkeypatch):
    class FakeResponse:
        headers = {"content-type": "application/json", "x-mineru-version": "2.6.0"}
        content = b"{}"

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(settings, "mineru_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_expected_version", "2.5.4")
    monkeypatch.setattr(mineru_module.httpx, "Client", FakeClient)

    with pytest.raises(MinerUContractError, match="expected 2.5.4, got 2.6.0"):
        parse_pdf(b"pdf", "order.pdf")


def test_mineru_version_lock_cannot_be_empty(monkeypatch):
    monkeypatch.setattr(settings, "mineru_base_url", "http://mineru.test")
    monkeypatch.setattr(settings, "mineru_expected_version", "")

    with pytest.raises(MinerUContractError, match="must be pinned"):
        parse_pdf(b"pdf", "order.pdf")


def test_pdf_prefers_mineru(monkeypatch):
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(settings, "mineru_fallback_enabled", True)
    monkeypatch.setattr(
        convert_service.mineru,
        "parse_pdf",
        lambda _file_bytes, _filename: "# MinerU Markdown",
    )

    converted = convert_service.convert_to_markdown(b"%PDF-1.7\n", "order.pdf")

    assert converted == "# MinerU Markdown"
    assert converted.parser == "mineru"
    assert converted.parser_fallback is False


def test_pdf_fallback_raises_actionable_error_when_mineru_fails(monkeypatch):
    # pdfplumber 打不开（触发 ValueError）且 MinerU 也失败时，不再落回本地
    # 解析器（必然再次失败），而是给出可行动的错误提示
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(settings, "mineru_fallback_enabled", True)

    def fail_mineru(_file_bytes, _filename):
        raise MinerUError("unavailable")

    monkeypatch.setattr(convert_service.mineru, "parse_pdf", fail_mineru)

    with pytest.raises(ConvertError) as exc_info:
        convert_service.convert_to_markdown(b"%PDF-1.7\n", "order.pdf")

    assert exc_info.value.code == "pdf_parse_failed"
    assert "convert the PDF to images" in exc_info.value.message
    assert exc_info.value.details["mineru_error"].startswith("MinerUError")


def test_pdf_can_disable_fallback(monkeypatch):
    monkeypatch.setattr(settings, "mineru_enabled", True)
    monkeypatch.setattr(settings, "mineru_fallback_enabled", False)

    def fail_mineru(_file_bytes, _filename):
        raise MinerUError("unavailable")

    monkeypatch.setattr(convert_service.mineru, "parse_pdf", fail_mineru)

    with pytest.raises(ConvertError, match="MinerU convert failed"):
        convert_service.convert_to_markdown(b"%PDF-1.7\n", "order.pdf")


def test_image_without_mineru_is_marked_for_review(monkeypatch):
    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    monkeypatch.setattr(settings, "mineru_enabled", False)

    result = convert_service.convert_image_to_parse_result(image_bytes, "order.png")

    assert result.pages[0].confidence == "low"
    assert result.parser_fallback is True
    assert result.review_issues()[0]["code"] == "vision_only_unverified"
    assert result.review_issues()[0]["blocking"] is True


def test_mineru_markdown_is_sent_to_llm_in_full(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    markdown = "# 做箱通知书\n" + ("完整正文字段\n" * 2000) + "文档末尾唯一字段"
    captured: dict = {}
    async def _fake_convert_to_markdown(_file_bytes, _filename):
        return ConversionText(markdown, parser="mineru")

    monkeypatch.setattr(
        skill_module,
        "convert_to_markdown_async",
        _fake_convert_to_markdown,
    )

    async def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {"source": {}}, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "achat_json", fake_chat_json)

    asyncio.run(skill_module.TuoshuSkill().run(file_bytes=b"pdf", filename="order.pdf"))

    user_message = captured["messages"][-1]["content"]
    assert markdown in user_message
    assert "文档末尾唯一字段" in user_message


# ---------- async 页级路由（收尾计划改造项 D/4.1：gather + Semaphore） ----------


def _make_fake_pdf(pages_text):
    """构造 pdfplumber 替身（FakePage/FakePdf），供页级路由测试复用。"""
    import pdfplumber

    class FakePage:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

        def extract_tables(self):
            return []

        def extract_words(self):
            return []

    class FakePdf:
        pages = [FakePage(t) for t in pages_text]

        def close(self):
            return None

    return pdfplumber, FakePdf


@pytest.mark.asyncio
async def test_mixed_pdf_async_routes_only_bad_page_to_mineru(monkeypatch):
    """async 版与同步版同语义：合格页 pdfplumber 直出，仅不合格页送 OCR。"""
    import app.skills.tuoshu.convert_service as convert_service

    pdfplumber, FakePdf = _make_fake_pdf(
        ["提单号 船名 件数 " + "有效文本" * 20, ""]
    )
    mineru_calls: list[str] = []

    async def fake_mineru(_bytes, filename, **_kwargs):
        mineru_calls.append(filename)
        return MinerUParseResult(markdown="| 提单号 | OCR000001 |", table_count=1)

    monkeypatch.setattr(pdfplumber, "open", lambda _stream: FakePdf())
    monkeypatch.setattr(
        convert_service, "_render_pdf_page", lambda *_args, **_kwargs: b"png"
    )
    monkeypatch.setattr(convert_service.mineru, "parse_document_async", fake_mineru)

    result = await convert_service._convert_pdf_with_page_routing_async(
        b"pdf", "mixed.pdf"
    )

    assert [page.parser for page in result.pages] == ["pdfplumber", "mineru"]
    assert mineru_calls == ["mixed-page-2.png"]


@pytest.mark.asyncio
async def test_pdf_page_routing_async_bounds_ocr_concurrency(monkeypatch):
    """多页 OCR：gather 并发受 mineru_ocr_concurrency 有界（峰值 = 上限而非任务数），
    结果按文档序合并（页序不因完成顺序漂移）。"""
    import app.skills.tuoshu.convert_service as convert_service

    pdfplumber, FakePdf = _make_fake_pdf(["", "", ""])  # 3 页全不合格 → 全 OCR

    state = {"inflight": 0, "peak": 0}
    done_order: list[int] = []

    async def fake_mineru(_bytes, filename, **_kwargs):
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        await asyncio.sleep(0.001)
        state["inflight"] -= 1
        done_order.append(filename)
        return MinerUParseResult(markdown=f"ocr-{filename}")

    monkeypatch.setattr(settings, "mineru_ocr_concurrency", 2)
    monkeypatch.setattr(pdfplumber, "open", lambda _stream: FakePdf())
    monkeypatch.setattr(
        convert_service, "_render_pdf_page", lambda *_args, **_kwargs: b"png"
    )
    monkeypatch.setattr(convert_service.mineru, "parse_document_async", fake_mineru)

    result = await convert_service._convert_pdf_with_page_routing_async(
        b"pdf", "multi.pdf"
    )

    assert state["peak"] == 2  # 有界：3 任务共享 2 槽
    assert [page.parser for page in result.pages] == ["mineru"] * 3
    # 页序与文档序一致（合并不看完成顺序）
    assert [page.markdown for page in result.pages] == [
        "ocr-multi-page-1.png",
        "ocr-multi-page-2.png",
        "ocr-multi-page-3.png",
    ]
    assert len(done_order) == 3
