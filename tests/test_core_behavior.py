from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.core import registry
from app.errors import LLMError, ParseError
from app.llm.openclaw import chat_json
from app.main import app
from app.skills.tuoshu.chinese_schema import to_chinese
from app.skills.tuoshu.normalizer import normalize_llm_output
from app.skills.tuoshu.postprocessor import finalize_extraction
from app.skills.tuoshu.prompt import (
    build_few_shot_messages,
    build_system_prompt,
    detect_prompt_route,
    format_to_chat_text,
)
from app.skills.tuoshu.schema import TuoshuOutput
from app.skills.tuoshu.skill import TuoshuSkill, _clean_json_schema

client = TestClient(app)


def test_nullable_object_schema_keeps_object_shape():
    schema = _clean_json_schema(TuoshuOutput.model_json_schema())
    factory = schema["properties"]["factory"]

    assert set(factory["type"]) == {"object", "null"}
    assert "name" in factory["properties"]
    assert "address" in factory["properties"]


def test_business_dates_are_constrained_in_json_schema():
    schema = _clean_json_schema(TuoshuOutput.model_json_schema())

    assert schema["properties"]["etd"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
    assert "T\\d{2}:\\d{2}:\\d{2}" in schema["properties"]["loading_time"]["pattern"]


def test_prompt_routes_to_one_matching_example():
    route = detect_prompt_route("运输委托书\n我司编号：WXHYC22010107\n拆装箱日期：2022-01-01")
    messages = build_few_shot_messages(route)

    assert route.doc_type == "TRANSPORT_ORDER"
    assert route.template_hint == "wxhyc_transport_para"
    assert len(messages) == 2
    assert "WXHYC" in messages[0]["content"]


def test_incident_prompt_uses_fragmented_date_few_shot_with_positive_mapping():
    route = detect_prompt_route(
        "1SHA044022运输委托书2021-05-28.pdf\n订单编号：BSSE2105280058"
    )
    messages = build_few_shot_messages(route)
    system = build_system_prompt(route)

    assert route.template_hint == "bingsheng_transport"
    assert len(messages) == 2
    assert "日期：20" in messages[0]["content"]
    assert '"doc_date": "2021-05-28"' in messages[1]["content"]
    assert "| `TO`/`致`/非空`ATTN` | `recipient` |" in system
    assert "| `FROM`/`FM` 联系人 | `sender_contact` |" in system
    assert "carrier_by_vessel" in system


def test_prompt_route_treats_door_loading_notice_as_trucking():
    route = detect_prompt_route("门点装箱通知\n车队将于 2019-09-12 到以下门点装柜")

    assert route.doc_type == "TRUCKING_ORDER"


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


def test_system_prompt_does_not_duplicate_schema_or_display_reference():
    system = build_system_prompt(detect_prompt_route("做箱通知书 ESFF21030474"))

    assert "# 托书统一 JSON Schema" not in system
    assert "# 运输委托书展示格式约束" not in system
    assert "# 字段来源表（唯一目标）" in system


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


def test_high_confidence_mineru_image_is_cross_checked_against_original(monkeypatch):
    import app.skills.tuoshu.skill as skill_module
    from app.document_parsers.models import ParsedPage, ParseResult

    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    parsed_text = "备注：出口清关的装完箱后请及时进港 作业资水！"
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

    TuoshuSkill().run(file_bytes=image_bytes, filename="order.png")

    user_content = captured["messages"][-1]["content"]
    text_content = next(item["text"] for item in user_content if item["type"] == "text")
    images = [item for item in user_content if item["type"] == "image_url"]
    assert parsed_text in text_content
    assert "OCR 中存在但图片上看不到的词句必须剔除" in text_content
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert images[0]["image_url"]["detail"] == "low"


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
    calls: list[tuple[list[dict], dict]] = []

    def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs["response_format"]))
        if kwargs["response_format"]["type"] == "json_schema":
            raise LLMError(
                "unsupported response format",
                code="llm_response_format_unsupported",
            )
        return '{"ok": true}', {"model": "fake", "usage": None}

    monkeypatch.setattr("app.llm.openclaw.chat", fake_chat)
    data, _meta = chat_json([], json_schema={"type": "object"})

    assert data == {"ok": True}
    assert [response_format["type"] for _, response_format in calls] == [
        "json_schema",
        "json_object",
    ]
    assert calls[0][0] == []
    assert '"type":"object"' in calls[1][0][0]["content"]


def test_chat_json_rejects_non_object(monkeypatch):
    monkeypatch.setattr(
        "app.llm.openclaw.chat",
        lambda _messages, **_kwargs: ("[]", {"model": "fake", "usage": None}),
    )

    with pytest.raises(ParseError, match="JSON object"):
        chat_json([])


def test_business_number_and_customs_number_are_not_conflated():
    result = normalize_llm_output(
        {
            "我司业务编号": "WXHYC22010107",
            "报关单号": "223120220000123456",
        }
    )

    assert result["internal_ref"] == "WXHYC22010107"
    assert result["customs_declaration_no"] == "223120220000123456"


def test_structured_review_issue_aliases_are_normalized():
    result = normalize_llm_output(
        {
            "review_issues": [
                {
                    "问题代码": "known_issue",
                    "字段": "carrier",
                    "描述": "承运人需确认",
                    "候选值": "OOCL",
                    "是否阻断": False,
                }
            ]
        }
    )

    assert result["review_issues"] == [
        {
            "code": "known_issue",
            "field": "carrier",
            "message": "承运人需确认",
            "source_values": ["OOCL"],
            "blocking": False,
        }
    ]


@pytest.mark.parametrize(
    "review_issues",
    [
        ["carrier_by_vessel: 船名 OOCL 属 OOCL"],
        [{"问题": "箱号无法辨认"}],
        [{"code": "unstructured_review_issue", "field": "carrier", "message": "兜底"}],
        "missing_container_measurements: 件数无具体值",
    ],
)
def test_unstructured_review_issues_are_logged_and_rejected(review_issues, caplog):
    with caplog.at_level("ERROR"), pytest.raises(ParseError):
        normalize_llm_output({"review_issues": review_issues})

    assert "tuoshu_review_issue_parse_failed" in caplog.text


def test_finalize_cannot_silently_drop_unstructured_review_issue(caplog):
    with caplog.at_level("ERROR"), pytest.raises(ParseError):
        finalize_extraction(
            {"review_issues": ["missing_container_measurements: 件数无具体值"]},
            source_text=None,
        )

    assert "tuoshu_review_issue_parse_failed" in caplog.text


def test_chinese_combined_fields_are_normalized_to_schema():
    result = normalize_llm_output(
        {
            "船名航次": "RESURGENCE V.1728S",
            "开航时间": "2026-07-21",
            "开港时间": "2026-07-20",
            "拆装箱日期": "2026-07-19",
            "箱型箱量": "3*40HC",
            "件数": "2,150 CTNS",
            "毛重": "5,375.0 KGS",
            "体积": "64.518 CBM",
            "门点地址": "无锡市惠山区示例路1号",
            "工厂联系人": "范颖晔",
            "工厂电话": "13800000000",
            "发货人公司": "无锡某进出口有限公司",
            "备注": "原备注",
        }
    )

    assert result["vessel"] == "RESURGENCE"
    assert result["voyage"] == "1728S"
    assert result["etd"] == "2026-07-21"
    assert result["loading_time"] == "2026-07-19"
    assert result["remark"] == "原备注；开港时间：2026-07-20"
    assert result["containers"] == [
        {
            "type": "40HC",
            "qty": 3,
            "packages": 2150,
            "gross_weight_kg": 5375.0,
            "volume_cbm": 64.518,
            "packages_unit": "CTNS",
        }
    ]
    assert result["factory"] == {
        "address": "无锡市惠山区示例路1号",
        "contact": "范颖晔",
        "phone": "13800000000",
    }
    assert result["shipper_company"] == "无锡某进出口有限公司"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026/7/20", "2026-07-20"),
        ("2026.7.20", "2026-07-20"),
        ("2026年7月20日", "2026-07-20"),
        ("2026-07-20T08:30:00", "2026-07-20"),
    ],
)
def test_etd_common_date_spellings_are_normalized(value, expected):
    result = normalize_llm_output({"etd": value})

    assert result["etd"] == expected


def test_document_and_loading_dates_are_normalized_before_schema_validation():
    result = normalize_llm_output(
        {
            "doc_date": "2026年7月15日",
            "loading_time": "2026年7月16日 0:00",
        }
    )

    assert result["doc_date"] == "2026-07-15"
    assert result["loading_time"] == "2026-07-16T00:00:00"


def test_loading_time_without_space_after_chinese_day_is_normalized():
    result = normalize_llm_output({"loading_time": "2026年7月16日0:00"})

    assert result["loading_time"] == "2026-07-16T00:00:00"


def test_invalid_optional_date_becomes_blocking_review_issue():
    result = finalize_extraction(
        {
            "loading_time": "2026年2月30日",
            "shipper_company": "测试托运人有限公司",
            "factory": {"name": "测试工厂"},
            "containers": [
                {"type": "20GP", "qty": 1, "packages": 1, "volume_cbm": 1}
            ],
        },
        source_text=None,
    )

    assert result["loading_time"] is None
    assert result["ready_for_order"] is False
    assert {
        "code": "invalid_date_format",
        "field": "loading_time",
        "message": "日期值格式或日历日期无效，已清空并需人工确认",
        "source_values": ["2026年2月30日"],
        "blocking": True,
    } in result["review_issues"]
    result["source"] = {"file": "invalid-date.pdf", "doc_format": "pdf"}
    TuoshuOutput.model_validate(result)


def test_table_dates_restore_loading_time_without_copying_etd():
    result = finalize_extraction(
        {
            "etd": "2026-07-19",
            "loading_time": "2026-07-19",
            "shipper_company": "上海柚理供应链管理有限公司",
            "factory": {"name": "皇裕精密技术（苏州）股份有限公司"},
            "containers": [
                {"type": "20GP", "qty": 1, "packages": 42, "volume_cbm": 70}
            ],
        },
        source_text=(
            "托运人：上海柚理供应链管理有限公司\n"
            "| 装箱日期 | 船期 | 截AMS时间 |\n"
            "| --- | --- | --- |\n"
            "| 2026年7月15日 0:00 | 2026/7/19 | |"
        ),
    )

    assert result["loading_time"] == "2026-07-15T00:00:00"
    assert result["etd"] == "2026-07-19"


def test_invalid_calendar_date_is_not_silently_rewritten():
    result = normalize_llm_output({"etd": "2026/2/30"})

    assert result["etd"] == "2026/2/30"


def test_slash_vessel_voyage_is_split():
    result = normalize_llm_output({"船名航次": "BALLENITA/0PPT4E"})

    assert result["vessel"] == "BALLENITA"
    assert result["voyage"] == "0PPT4E"


def test_spaced_table_aliases_normalize_to_schema_fields():
    result = normalize_llm_output(
        {"船  公  司": "EMC CPS", "中转港（卸港）": "USLAX", "船 期": "2026-01-21"}
    )

    assert result["carrier"] == "EMC CPS"
    assert result["transit_port"] == "USLAX"
    assert result["etd"] == "2026-01-21"


def test_finalize_preserves_transit_lookup_and_builds_order_mapping():
    result = finalize_extraction(
        {
            "internal_ref": "WXHYC22010107",
            "transit_port": None,
            "shipper_company": "无锡某进出口有限公司",
            "factory": {"name": "宜兴门点"},
            "containers": [{"po_no": "D0483/0908"}],
            "remark": "下单前核对船期",
        },
        source_text=(
            "我司业务编号：WXHYC22010107\n"
            "托运人：无锡某进出口有限公司\n"
            "中转港：见设备交接单\nPO：D0483/0908"
        ),
    )

    assert result["transit_port"] == "见设备交接单"
    assert result["order_mapping"] == {
        "c_sn": "WXHYC22010107",
        "mbl_no": None,
        "hbl_no": None,
        "c_title": "无锡某进出口有限公司",
        "factory_name": "宜兴门点",
        "c_note": "PO号：D0483/0908",
    }
    issue = next(item for item in result["review_issues"] if item["code"] == "ungrounded_text")
    assert issue["source_values"] == ["下单前核对船期"]
    assert result["ready_for_order"] is False


def test_child_bill_aliases_never_fall_back_to_master_bill():
    normalized = normalize_llm_output({"子提单号": "HBL240001"})

    assert normalized["hbl_no"] == "HBL240001"
    assert normalized.get("mbl_no") is None

    result = finalize_extraction(
        {
            "mbl_no": "HBL240001",
            "hbl_no": None,
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text="子提单号：HBL240001",
    )

    assert result["mbl_no"] is None
    assert result["hbl_no"] == "HBL240001"
    assert result["order_mapping"]["mbl_no"] is None
    assert result["order_mapping"]["hbl_no"] == "HBL240001"
    assert any(issue["code"] == "hbl_misclassified_as_mbl" for issue in result["review_issues"])
    assert result["ready_for_order"] is False


def test_explicit_master_and_child_bills_are_both_preserved():
    result = finalize_extraction(
        {
            "mbl_no": "MBL240001",
            "hbl_no": "HBL240001",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text="主提单号：MBL240001\n子提单号：HBL240001",
    )

    assert result["mbl_no"] == "MBL240001"
    assert result["hbl_no"] == "HBL240001"
    assert result["order_mapping"]["mbl_no"] == "MBL240001"
    assert result["order_mapping"]["hbl_no"] == "HBL240001"
    assert not any(
        issue["code"] == "hbl_misclassified_as_mbl" for issue in result["review_issues"]
    )


def test_finalize_blocks_non_verbatim_person_and_missing_order_fields():
    result = finalize_extraction(
        {
            "sender_contact": "范颖晰",
            "factory": {"name": None, "contact": "范颖晰"},
            "containers": [],
        },
        source_text="发货联系人：范颖晔",
    )

    issue_keys = {(issue["code"], issue["field"]) for issue in result["review_issues"]}
    assert ("sender_contact_not_from_from_field", "sender_contact") in issue_keys
    assert ("person_name_not_verbatim", "factory.contact") in issue_keys
    assert result["sender_contact"] is None
    assert ("missing_shipper_company", "shipper_company") in issue_keys
    assert ("missing_factory_name", "factory.name") in issue_keys
    assert result["order_mapping"]["c_title"] is None
    assert result["order_mapping"]["factory_name"] is None
    assert result["ready_for_order"] is False
    assert result["raw_text_snippet"] == "发货联系人：范颖晔"


def test_incident_document_keeps_header_agent_out_of_c_title_and_restores_fields():
    source_text = """# 运输委托书
上海秉晟国际物流有限公司
| 订单编号： | BSSE2105280058 | 日期：20 | 21.5.28 |
| TO: | 上海运嘉货运代理有限公司 | 海运出口 | |
| 主单号 | 船名 | 航次 |
| 1SHA044022 | MAERSK LAVRAS | 122W |
| 件数 | 毛重 | 体积 |
| 0 | 0 | 0 |
| 地址类型 | 地址 | 时间 |
| 拆/装箱地址 | 杨会计 13921910878 江苏省扬州市宝应县望直港汽配工业园 | 2021-06-01 08:00:00 |
图章 OCR：28GSHEN S
"""
    result = finalize_extraction(
        {
            "shipper_company": "上海秉晟国际物流有限公司",
            "shipper_agent": None,
            "recipient": None,
            "doc_date": None,
            "carrier": "MSK",
            "vessel": "MAERSK LAVRAS",
            "loading_time": "2021-06-01",
            "factory": {"name": None},
            "containers": [
                {
                    "seal_no": "28GSHEN S",
                    "packages": 0,
                    "gross_weight_kg": 0,
                    "volume_cbm": 0,
                }
            ],
            "remark": "TO：上海运嘉货运代理有限公司；海运出口；封号：28GSHEN S",
        },
        source_text=source_text,
    )

    assert result["shipper_company"] is None
    assert result["shipper_agent"] == "上海秉晟国际物流有限公司"
    assert result["order_mapping"]["c_title"] is None
    assert result["recipient"] == "上海运嘉货运代理有限公司"
    assert result["doc_date"] == "2021-05-28"
    assert result["loading_time"] == "2021-06-01T08:00:00"
    assert result["containers"][0]["seal_no"] is None
    assert result["containers"][0]["packages"] is None
    assert result["containers"][0]["gross_weight_kg"] is None
    assert result["containers"][0]["volume_cbm"] is None
    assert result["remark"] == "海运出口"
    assert result["order_mapping"]["c_note"] == "海运出口"
    issue_keys = {(issue["code"], issue["field"]) for issue in result["review_issues"]}
    assert ("shipper_company_not_from_explicit_field", "shipper_company") in issue_keys
    assert ("missing_shipper_company", "shipper_company") in issue_keys
    assert ("invalid_seal_no", "containers[0].seal_no") in issue_keys
    assert ("missing_container_measurements", "containers[0]") in issue_keys
    assert ("missing_factory_name", "factory.name") in issue_keys
    assert ("carrier_by_vessel", "carrier") in issue_keys
    assert result["ready_for_order"] is False


@pytest.mark.parametrize("label", ["致", "ATTN"])
def test_recipient_aliases_route_to_recipient_and_leave_remark_clean(label):
    result = finalize_extraction(
        {
            "recipient": None,
            "remark": f"{label}：上海硕豪物流有限公司；请准时到达",
            "shipper_company": None,
            "factory": {"name": "江西杰盛医疗制品有限公司"},
        },
        source_text=f"{label}：上海硕豪物流有限公司\n请准时到达",
    )

    assert result["recipient"] == "上海硕豪物流有限公司"
    assert result["remark"] == "请准时到达"
    assert result["order_mapping"]["c_note"] == "请准时到达"


def test_explicit_body_shipper_is_the_only_c_title_source():
    result = finalize_extraction(
        {
            "shipper_company": "抬头货代有限公司",
            "factory": {"name": "某门点"},
        },
        source_text="抬头货代有限公司\n托运人：正文出口有限公司",
    )

    assert result["shipper_company"] == "正文出口有限公司"
    assert result["order_mapping"]["c_title"] == "正文出口有限公司"
    assert any(
        issue["code"] == "shipper_company_not_from_explicit_field"
        for issue in result["review_issues"]
    )
    assert result["ready_for_order"] is False


def test_container_number_format_and_verbatim_rules_are_blocking():
    result = finalize_extraction(
        {
            "factory": {"name": "某门点"},
            "containers": [
                {"container_no": "28GSHEN S"},
                {"container_no": "SEGU1234567", "seal_no": "SEAL-99"},
            ],
        },
        source_text="箱号：SEGU1234567",
    )

    assert result["containers"][0]["container_no"] is None
    assert result["containers"][1]["container_no"] == "SEGU1234567"
    assert result["containers"][1]["seal_no"] is None
    issue_keys = {(issue["code"], issue["field"]) for issue in result["review_issues"]}
    assert ("invalid_container_no", "containers[0].container_no") in issue_keys
    assert ("seal_no_not_verbatim", "containers[1].seal_no") in issue_keys


def test_carrier_prefix_wins_and_indexed_remark_stays_with_container():
    result = finalize_extraction(
        {
            "mbl_no": "HLCUSHA12345678",
            "carrier": "HMM",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [{"remark": None}],
            "remark": "柜1备注：博特装柜；下单前核对",
        },
        source_text="提单号：HLCUSHA12345678\n柜1：博特装柜",
    )

    assert result["carrier"] == "HLC"
    assert result["containers"][0]["remark"] == "博特装柜"
    assert any(issue["code"] == "carrier_prefix_mismatch" for issue in result["review_issues"])
    assert result["ready_for_order"] is False


def test_explicit_spaced_carrier_wins_over_mbl_prefix_and_restores_booking_fields():
    result = finalize_extraction(
        {
            "carrier": "EMC",
            "mbl_no": "EGLV12345678",
            "etd": None,
            "transit_port": None,
            "doc_date": None,
            "remark": None,
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=(
            "内装箱委托书\n"
            "| 船 公 司 | 船 期 | 中转港（卸港） | 要求进港时间 |\n"
            "| --- | --- | --- | --- |\n"
            "| EMC CPS | 1月21日 | USLAX | 装好就进港 |"
        ),
        reference_year=2026,
    )

    assert result["carrier"] == "EMC CPS"
    assert result["etd"] == "2026-01-21"
    assert result["transit_port"] == "USLAX"
    assert result["remark"] == "装好就进港"
    assert not any(
        issue["code"] in {"carrier_by_mbl", "carrier_prefix_mismatch"}
        for issue in result["review_issues"]
    )


@pytest.mark.parametrize("source_text", ["船期：1月21日", None])
def test_partial_etd_without_reference_year_is_nullable_with_review_issue(source_text):
    result = finalize_extraction(
        {
            "etd": "1月21日",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=source_text,
    )

    assert result["etd"] is None
    issue = next(issue for issue in result["review_issues"] if issue["code"] == "etd_year_missing")
    assert issue["source_values"] == ["1月21日"]


def test_visual_person_name_is_blocked_when_verbatim_check_is_unavailable():
    result = finalize_extraction(
        {
            "sender_contact": "范颖晰",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=None,
    )

    assert result["raw_text_snippet"] is None
    assert any(issue["code"] == "person_name_unverified" for issue in result["review_issues"])
    assert result["ready_for_order"] is False


def test_finalize_blocks_container_data_conflict_left_in_remark():
    conflict = "主值 2150 CTNS / 5375.0 KGS / 64.518 CBM；另有记录 2149 / 5372.5 / 64.487"
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [{"packages": 2150, "remark": conflict}],
        },
        source_text=conflict,
    )

    assert any(issue["code"] == "conflicting_container_data" for issue in result["review_issues"])
    assert result["ready_for_order"] is False


def test_review_issues_are_single_source_for_missing_measurements_and_renderer():
    result = finalize_extraction(
        {
            "mbl_no": "COSU6886686750",
            "carrier": "OOCL",
            "factory": {"name": "江西杰盛医疗制品有限公司"},
            "containers": [
                {
                    "type": "40HC",
                    "qty": 1,
                    "packages": None,
                    "volume_cbm": None,
                }
            ],
        },
        source_text="提单号：COSU6886686750 承运人：OOCL 件数： 体积：",
    )

    assert {
        issue["code"] for issue in result["review_issues"]
    } == {
        "missing_shipper_company",
        "missing_container_measurements",
    }
    assert result["carrier"] == "OOCL"
    assert len(result["review_issues"]) == 2

    rendered = format_to_chat_text(
        {
            **result,
            "source": {"file": "test.pdf", "doc_format": "pdf"},
        }
    )
    assert rendered.count("复核项:") == len(result["review_issues"])
    assert "集装箱数据不完整" not in rendered
    assert "集装箱件数和体积为空，需人工确认" in rendered


def test_review_issues_are_deduplicated_by_code_before_output():
    result = finalize_extraction(
        {
            "mbl_no": "COSU6886686750",
            "carrier": "COSCO",
            "factory": {"name": "江西杰盛医疗制品有限公司"},
            "containers": [{"type": "40HC", "packages": None, "volume_cbm": None}],
            "review_issues": [
                {
                    "code": "missing_container_measurements",
                    "field": "containers[0]",
                    "message": "模型生成的缺失提示",
                    "source_values": [],
                    "blocking": True,
                },
                {
                    "code": "missing_container_measurements",
                    "field": "containers[0]",
                    "message": "另一条重复提示",
                    "source_values": ["件数为空"],
                    "blocking": True,
                },
            ],
        },
        source_text="提单号：COSU6886686750 件数： CTNS 体积： CBM",
    )

    codes = [issue["code"] for issue in result["review_issues"]]
    assert len(codes) == len(set(codes))
    assert codes.count("missing_container_measurements") == 1
    assert codes.count("carrier_by_mbl") == 1
    assert all(code != "unstructured_review_issue" for code in codes)


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [("40HQ", "40HQ"), ("40'HQ", "40HQ"), ("40'HC", "40HC"), ("40HC", "40HC")],
)
def test_container_type_preserves_hq_or_hc_suffix(raw_type, expected):
    normalized = normalize_llm_output({"containers": [{"type": raw_type}]})

    assert normalized["containers"][0]["type"] == expected


def test_container_type_is_restored_from_source_instead_of_hq_to_hc_rewrite():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [{"type": "40HC", "packages": 10, "volume_cbm": 20}],
        },
        source_text="托运人：某托运人公司\n箱型箱量：1 X 40HQ\n件数：10\n体积：20CBM",
    )

    assert result["containers"][0]["type"] == "40HQ"
    issue = next(
        item
        for item in result["review_issues"]
        if item["code"] == "container_type_not_verbatim"
    )
    assert issue["source_values"] == ["40HC", "40HQ"]
    assert issue["blocking"] is False


def test_detail_container_type_wins_over_stale_total_container_value():
    result = finalize_extraction(
        {
            "shipper_company": "上海柚理供应链管理有限公司",
            "factory": {"name": "皇裕精密技术（苏州）股份有限公司"},
            "containers": [
                {"type": "40HQ", "qty": 1},
                {
                    "type": "20GP",
                    "qty": 1,
                    "packages": 42,
                    "gross_weight_kg": 35000,
                    "volume_cbm": 70,
                },
            ],
            "review_issues": [
                {
                    "code": "conflicting_container_data",
                    "field": "containers",
                    "message": "总箱量标注40HQ×1 20GP×1，但详情箱型栏仅显示20GP×1，存在冲突",
                    "source_values": ["40HQ×1 20GP×1", "20GP×1"],
                    "blocking": True,
                }
            ],
        },
        source_text=(
            "托运人：上海柚理供应链管理有限公司\n"
            "总箱量：40HQ×1 20GP×1\n"
            "货名：热镀锌钢带等 箱型：20GP×1\n"
            "件数：42 毛重：35000 体积：70"
        ),
    )

    assert result["containers"] == [
        {
            "type": "20GP",
            "qty": 1,
            "packages": 42,
            "gross_weight_kg": 35000,
            "volume_cbm": 70,
        }
    ]
    assert not any(
        issue["code"] == "conflicting_container_data"
        for issue in result["review_issues"]
    )


def test_detail_container_precedence_does_not_hide_measurement_conflicts():
    result = finalize_extraction(
        {
            "shipper_company": "上海柚理供应链管理有限公司",
            "factory": {"name": "皇裕精密技术（苏州）股份有限公司"},
            "containers": [
                {
                    "type": "20GP",
                    "qty": 1,
                    "packages": 42,
                    "gross_weight_kg": 35000,
                    "volume_cbm": 70,
                }
            ],
            "review_issues": [
                {
                    "code": "conflicting_container_data",
                    "field": "containers[0]",
                    "message": "同一柜存在两组件数、毛重或体积，需人工裁决",
                    "source_values": ["42/35000/70", "41/34990/69"],
                    "blocking": True,
                }
            ],
        },
        source_text=(
            "托运人：上海柚理供应链管理有限公司\n"
            "总箱量：40HQ×1 20GP×1\n"
            "货名：热镀锌钢带等 箱型：20GP×1\n"
            "件数：42 毛重：35000 体积：70"
        ),
    )

    assert any(
        issue["code"] == "conflicting_container_data"
        for issue in result["review_issues"]
    )


def test_ungrounded_remark_is_removed_before_building_c_note():
    hallucination = "出口清关的装完箱后请及时进港作业资水！"
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "江阴市信腾新颖地面材料有限公司"},
            "remark": f"数据准确，箱单填好过去；{hallucination}",
            "order_mapping": {"c_note": hallucination},
            "containers": [{"type": "40HQ", "packages": 10, "volume_cbm": 10}],
        },
        source_text=(
            "托运人：某托运人公司\n"
            "箱型箱量：1 X 40HQ\n"
            "江阴市信腾新颖地面材料有限公司\n"
            "数据准确，箱单填好过去"
        ),
    )

    assert result["remark"] == "数据准确，箱单填好过去"
    assert result["order_mapping"]["c_note"] == "数据准确，箱单填好过去"
    assert hallucination not in (result["remark"] or "")
    assert hallucination not in (result["order_mapping"]["c_note"] or "")
    issue = next(item for item in result["review_issues"] if item["code"] == "ungrounded_text")
    assert issue["field"] == "remark"
    assert issue["source_values"] == ["出口清关的装完箱后请及时进港"]
    assert issue["blocking"] is True
    artifact_issue = next(
        item for item in result["review_issues"] if item["code"] == "confirmed_ocr_artifact"
    )
    assert artifact_issue["source_values"] == ["作业资水"]


def test_confirmed_ocr_artifact_is_removed_even_when_mineru_text_contains_it():
    artifact = "作业资水"
    source_remark = f"货名：无纺布；作业要求：出口清关的装完箱后请及时进港 {artifact}"
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "太仓本杰明纺织科技有限公司"},
            "remark": source_remark,
            "containers": [{"type": "40HQ", "packages": 249, "volume_cbm": 68}],
        },
        source_text=f"托运人：某托运人公司\n备注：{source_remark}",
    )

    expected = "货名：无纺布；作业要求：出口清关的装完箱后请及时进港"
    assert result["remark"] == expected
    assert result["order_mapping"]["c_note"] == expected
    assert artifact not in result["remark"]
    issue = next(
        item for item in result["review_issues"] if item["code"] == "confirmed_ocr_artifact"
    )
    assert issue["field"] == "remark"
    assert issue["source_values"] == [artifact]
    assert issue["blocking"] is True


def test_grounded_conflict_wrapper_in_container_remark_is_retained():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [
                {
                    "type": "40HC",
                    "packages": 2150,
                    "volume_cbm": 64.518,
                    "remark": "另有记录 2149CTNS, 5372.5KGS, 64.487CBM，待人工确认",
                }
            ],
        },
        source_text=(
            "托运人：某托运人公司\n箱型：40HC\n"
            "2150CTNS, 5375KGS, 64.518CBM\n2149CTNS, 5372.5KGS, 64.487CBM"
        ),
    )

    assert "另有记录 2149CTNS" in result["containers"][0]["remark"]
    assert not any(item["code"] == "ungrounded_text" for item in result["review_issues"])


def test_remark_label_is_removed_while_grounded_value_is_kept():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "remark": "备注说明：1750",
        },
        source_text="托运人：某托运人公司\n备注说明\n1750",
    )

    assert result["remark"] == "1750"
    assert result["order_mapping"]["c_note"] == "1750"
    assert not any(item["code"] == "ungrounded_text" for item in result["review_issues"])


def test_footer_contact_cannot_be_used_as_sender_contact():
    result = finalize_extraction(
        {
            "sender_contact": "ADA",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text="FM: 上海威世国际货物运输代理有限公司\n联系人：ADA",
    )

    assert result["sender_contact"] is None
    issue = next(
        item
        for item in result["review_issues"]
        if item["code"] == "sender_contact_not_from_from_field"
    )
    assert issue["source_values"] == ["ADA"]
    assert issue["blocking"] is True


def test_unknown_container_type_is_preserved_with_non_blocking_review():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [{"type": "40NOR", "packages": 10, "volume_cbm": 20}],
        },
        source_text="托运人：某托运人公司\n箱型：40NOR\n件数：10\n体积：20",
    )

    assert result["containers"][0]["type"] == "40NOR"
    issue = next(issue for issue in result["review_issues"] if issue["code"] == "unknown_container_type")
    assert issue["blocking"] is False
    assert result["ready_for_order"] is True


def test_port_modifier_and_package_unit_are_restored_from_source():
    result = finalize_extraction(
        {
            "pod": "COLUMBUS",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [
                {
                    "type": "40HQ",
                    "packages": None,
                    "packages_unit": None,
                    "volume_cbm": None,
                }
            ],
        },
        source_text=(
            "托运人：某托运人公司\n"
            "箱型/箱量：40HQ X 1\n"
            "件数：\nCTNS\n"
            "目的港：\nCOLUMBUS(OH)\n"
            "体积：CBM"
        ),
    )

    assert result["pod"] == "COLUMBUS, OH"
    assert result["containers"][0]["type"] == "40HQ"
    assert result["containers"][0]["packages"] is None
    assert result["containers"][0]["packages_unit"] == "CTNS"


def test_known_conflict_cannot_disable_blocking():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "review_issues": [
                {
                    "code": "conflicting_container_data",
                    "field": "containers[2]",
                    "message": "柜3有两组数据",
                    "blocking": False,
                }
            ],
        },
        source_text=None,
    )

    assert result["review_issues"][0]["blocking"] is True
    assert result["ready_for_order"] is False


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


def test_extract_response_has_no_independent_summary(monkeypatch):
    import app.skills.tuoshu.skill as skill_module

    monkeypatch.setattr(settings, "api_key", "test-key")
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
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    body = response.json()
    data = body["data"]
    assert body["content"] == "提单号：HLCUSHA12345678\n承运人：HMM\n柜1备注：博特装柜"
    assert body["meta"] == {"model": "fake", "usage": None}
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
