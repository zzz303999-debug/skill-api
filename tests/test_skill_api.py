"""skill 执行、HTTP API 与 chat 渲染行为测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.core import registry
from app.errors import ParseError
from app.main import app
from app.skills.tuoshu.chinese_schema import to_chinese
from app.skills.tuoshu.prompt import format_to_chat_text
from app.skills.tuoshu.skill import TuoshuSkill

client = TestClient(app)



def test_upload_limit(monkeypatch):
    monkeypatch.setattr(settings, "api_max_upload_bytes", 3)

    response = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("a.xlsx", b"four", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "file_too_large"


def test_empty_upload_is_rejected_before_conversion():
    response = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"


def test_batch_extract_calls_keyword_only_skill(monkeypatch):
    skill = registry.get("tuoshu")

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
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] == 2
    assert body["failed"] == 0
    assert [item["result"]["filename"] for item in body["results"]] == ["a.xlsx", "b.xlsx"]


def test_extract_response_has_no_independent_summary(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    monkeypatch.setattr(
        skill_module,
        "convert_to_markdown",
        lambda _file_bytes, _filename: (
            "提单号：HLCUSHA12345678\n承运人：HMM\n柜1备注：博特装柜"
        ),
    )
    monkeypatch.setattr(
        skill_module,
        "chat_json",
        lambda _messages, **_kwargs: (
            {
                "mbl_no": "HLCUSHA12345678",
                "carrier": "HMM",
                "shipper_company": "某托运人公司",
                "factory": {"name": "某门点"},
                "containers": [{"type": "40HC", "qty": 1, "remark": None}],
                "remark": "柜1备注：博特装柜",
                "raw_text_snippet": "模型生成的摘要不得采用",
                "source": {},
            },
            {
                "model": "fake",
                "usage": None,
                "chat_text": "承运人: HMM\n做箱工厂: 错误工厂",
                "summary": "模型二次摘要",
            },
        ),
    )

    response = client.post(
        "/skills/tuoshu/extract",
        files={"file": ("order.docx", b"fake-docx", "application/octet-stream")},
    )

    assert response.status_code == 200
    body = response.json()
    data = body["data"]
    assert body["content"] == "提单号：HLCUSHA12345678\n承运人：HMM\n柜1备注：博特装柜"
    assert body["meta"]["model"] == "fake"
    assert body["meta"]["usage"] is None
    assert body["meta"]["conversion_status"] == "converted"
    assert body["meta"]["source_bytes"] == len(b"fake-docx")
    assert body["meta"]["content_chars"] == len(body["content"])
    assert len(body["meta"]["source_sha256"]) == 64
    assert len(body["meta"]["content_sha256"]) == 64
    assert "carrier" in data and "承运人" not in data
    assert data["carrier"] == "HMM"
    assert data["factory"]["name"] == "某门点"
    assert data["containers"][0]["remark"] == "博特装柜"
    assert data["raw_text_snippet"].startswith("提单号：HLCUSHA12345678")

    rendered = format_to_chat_text(data)
    assert "船公司: HMM（接口原始值）" in rendered
    assert "船公司: HLC（接口原始值）" not in rendered
    assert "做箱工厂: 某门点" in rendered
    assert "错误工厂" not in rendered
    assert "箱型备注: 博特装柜" in rendered


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

    response = TuoshuSkill().run(file_bytes=b"fake-pdf", filename="scan.pdf")

    user_content = captured["messages"][-1]["content"]
    images = [item for item in user_content if item["type"] == "image_url"]
    assert len(images) == 2
    assert all(item["image_url"]["url"].startswith("data:image/png;base64,") for item in images)
    assert response["content"] == ""
    assert response["meta"]["conversion_status"] == "needs_review"
    assert response["meta"]["page_routes"] == [
        {
            "page": 1,
            "parser": "vision",
            "confidence": "low",
            "issues": ["vision_only_unverified"],
        },
        {
            "page": 2,
            "parser": "vision",
            "confidence": "low",
            "issues": ["vision_only_unverified"],
        },
    ]
    assert any(
        issue["code"] == "vision_only_unverified" and issue["blocking"]
        for issue in response["result"]["review_issues"]
    )
    assert response["result"]["ready_for_order"] is False


def test_incomplete_review_issue_is_repaired_once(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    calls: list[list[dict]] = []

    def fake_chat_json(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "review_issues": [
                    {
                        "code": "document_value_unclear",
                        "message": "单据字段无法辨认，需人工确认",
                    }
                ],
                "source": {},
            }, {"model": "fake", "usage": None}
        return {
            "review_issues": [
                {
                    "code": "document_value_unclear",
                    "field": "customer_ref",
                    "message": "单据字段无法辨认，需人工确认",
                    "source_values": [],
                    "blocking": True,
                }
            ],
            "source": {},
        }, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "chat_json", fake_chat_json)

    response = TuoshuSkill().run(
        file_bytes=b"\x89PNG\r\n\x1a\nimage",
        filename="order.png",
    )

    assert len(calls) == 2
    assert calls[1][-2]["role"] == "assistant"
    assert "index=0" in calls[1][-1]["content"]
    assert any(
        issue["code"] == "document_value_unclear"
        and issue["field"] == "customer_ref"
        for issue in response["result"]["review_issues"]
    )


def test_incomplete_review_issue_still_fails_after_one_repair(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    calls = 0

    def fake_chat_json(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "review_issues": [{"message": "箱号无法辨认"}],
            "source": {},
        }, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "chat_json", fake_chat_json)

    with pytest.raises(ParseError, match="required structure"):
        TuoshuSkill().run(
            file_bytes=b"\x89PNG\r\n\x1a\nimage",
            filename="order.png",
        )

    assert calls == 2


def test_parser_fallback_issue_reaches_final_output(monkeypatch):
    import app.skills.tuoshu.skill as skill_module
    from app.document_parsers.models import ParsedPage, ParseIssue, ParseResult

    parse_result = ParseResult(
        input_format="png",
        pages=[
            ParsedPage(
                page_number=1,
                parser="vision",
                confidence="low",
                vision_image=b"\x89PNG\r\n\x1a\nimage",
                issues=[
                    ParseIssue(
                        code="mineru_low_confidence",
                        message="MinerU 图片结果低置信，已转 vision，必须人工复核",
                        page=1,
                        source_values=("no_table_or_key_labels",),
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(skill_module, "convert_image_to_parse_result", lambda *_args: parse_result)
    monkeypatch.setattr(
        skill_module,
        "chat_json",
        lambda *_args, **_kwargs: (
            {
                "shipper_company": "某托运人公司",
                "factory": {"name": "某门点"},
                "source": {},
            },
            {"model": "fake", "usage": None},
        ),
    )

    response = TuoshuSkill().run(
        file_bytes=b"\x89PNG\r\n\x1a\nimage",
        filename="order.png",
    )

    issue = next(
        item
        for item in response["result"]["review_issues"]
        if item["code"] == "mineru_low_confidence"
    )
    assert issue["blocking"] is True
    assert response["result"]["ready_for_order"] is False
    assert response["meta"]["page_routes"][0]["parser"] == "vision"


def test_high_confidence_mineru_image_skips_vision_for_speed(monkeypatch):
    """MinerU 高置信时跳过 LLM vision 交叉核验，只用 OCR 文本走 LLM 以提速。"""
    import app.skills.tuoshu.skill as skill_module
    from app.document_parsers.models import ParsedPage, ParseResult

    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    parsed_text = "备注：出口清关的装完箱后请及时进港"
    captured: dict = {}
    parse_result = ParseResult(
        input_format="png",
        pages=[
            ParsedPage(
                page_number=1,
                parser="mineru",
                markdown=parsed_text,
                vision_image=image_bytes,
            )
        ],
    )
    monkeypatch.setattr(skill_module, "convert_image_to_parse_result", lambda *_args: parse_result)

    def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {"source": {}}, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "chat_json", fake_chat_json)

    result = TuoshuSkill().run(file_bytes=image_bytes, filename="order.png")

    user_content = captured["messages"][-1]["content"]
    # 高置信 MinerU 跳过 LLM vision，走纯文本通道
    assert isinstance(user_content, str)
    assert parsed_text in user_content
    assert "image_url" not in user_content
    # 标注跳过了 vision 交叉核验，提示人工抽检关键字段
    issues = result["result"].get("review_issues", [])
    assert any(i["code"] == "vision_cross_check_skipped" for i in issues)


def test_low_confidence_mineru_image_keeps_vision_cross_check(monkeypatch):
    """MinerU 低置信/fallback 时仍携原图走 LLM vision 交叉核验。"""
    import app.skills.tuoshu.skill as skill_module
    from app.document_parsers.models import ParsedPage, ParseResult

    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    parsed_text = "备注：出口清关的装完箱后请及时进港"
    captured: dict = {}
    parse_result = ParseResult(
        input_format="png",
        pages=[
            ParsedPage(
                page_number=1,
                parser="vision",
                markdown=parsed_text,
                confidence="low",
                vision_image=image_bytes,
                vision_mime="image/png",
            )
        ],
    )
    monkeypatch.setattr(skill_module, "convert_image_to_parse_result", lambda *_args: parse_result)

    def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return {"source": {}}, {"model": "fake", "usage": None}

    monkeypatch.setattr(skill_module, "chat_json", fake_chat_json)

    TuoshuSkill().run(file_bytes=image_bytes, filename="order.png")

    user_content = captured["messages"][-1]["content"]
    text_content = next(item["text"] for item in user_content if item["type"] == "text")
    images = [item for item in user_content if item["type"] == "image_url"]
    assert parsed_text in text_content
    assert "OCR 中存在但图片上看不到的词句必须剔除" in text_content
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert images[0]["image_url"]["detail"] == "high"


def test_skill_uses_deterministic_route_when_llm_misclassifies_doc_type(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    markdown = "门点装箱通知\n车队将于 2019-09-12 14:00 以前到门点装柜"
    monkeypatch.setattr(
        skill_module,
        "convert_to_markdown",
        lambda _file_bytes, _filename: markdown,
    )
    monkeypatch.setattr(
        skill_module,
        "chat_json",
        lambda _messages, **_kwargs: (
            {
                "doc_type": "PACKING_NOTICE",
                "shipper_company": "某托运人公司",
                "factory": {"name": "某门点"},
                "source": {},
            },
            {"model": "fake", "usage": None},
        ),
    )

    response = TuoshuSkill().run(file_bytes=b"fake-docx", filename="order.docx")

    assert response["result"]["doc_type"] == "TRUCKING_ORDER"


def test_chat_text_contains_every_container():
    text = format_to_chat_text(
        {
            "source": {"file": "test.docx", "doc_format": "docx"},
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


def test_chat_text_only_uses_present_json_content():
    text = format_to_chat_text(
        {
            "doc_type": "TRANSPORT_ORDER",
            "internal_ref": "WXHYC22010107",
            "vessel": "RESURGENCE",
            "voyage": None,
            "containers": [{"type": "40HC", "qty": 3}],
            "raw_text_snippet": "不应重新展示的原文",
            "source": {"file": "test.docx", "doc_format": "docx"},
        }
    )

    assert text == "我司业务编号: WXHYC22010107\n船名: RESURGENCE\n箱型: 40HC\n箱量: 3"


def test_chat_text_surfaces_blocking_review_issues():
    text = format_to_chat_text(
        {
            "source": {"file": "test.docx", "doc_format": "docx"},
            "review_issues": [
                {
                    "code": "conflicting_container_data",
                    "field": "containers[2]",
                    "message": "同一柜存在两组数据，需人工裁决",
                    "source_values": ["2150/5375.0/64.518", "2149/5372.5/64.487"],
                    "blocking": True,
                }
            ]
        }
    )

    assert "下单校验: 需人工复核" in text
    assert "2149/5372.5/64.487" in text


def test_display_renderers_reject_incomplete_json():
    with pytest.raises(ValidationError):
        format_to_chat_text({"carrier": "HLC"})
    with pytest.raises(ValidationError):
        to_chinese({"carrier": "HLC"})


def test_display_renderers_label_carrier_as_raw_shipping_company_value():
    data = {
        "carrier": "EMC CPS",
        "source": {"file": "test.docx", "doc_format": "docx"},
    }

    assert "船公司: EMC CPS（接口原始值）" in format_to_chat_text(data)
    assert to_chinese(data)["船公司"] == "EMC CPS（接口原始值）"
