"""prompt 路由与 JSON schema 行为测试。"""

from __future__ import annotations

import pytest

from app.core.errors import LLMError, ParseError
from app.llm import client as llm_client
from app.llm.client import chat_json
from app.skills.tuoshu.prompt import (
    build_few_shot_messages,
    build_system_prompt,
    detect_prompt_route,
)
from app.skills.tuoshu.schema import TuoshuOutput
from app.skills.tuoshu.skill import _clean_json_schema


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


def test_prompt_contains_compact_order_field_rules():
    system = build_system_prompt()

    assert "提单号必须至少 8 位" in system
    assert "箱长 `20`/`25`/`40`" in system
    assert "优先逐字取 `FM` 后的值" in system
    assert "详细街道地址" in system
    assert "毛重单位 `KGS`" in system
    assert "体积单位 `CBM`" in system


def test_prompt_route_treats_door_loading_notice_as_trucking():
    route = detect_prompt_route("门点装箱通知\n车队将于 2019-09-12 到以下门点装柜")

    assert route.doc_type == "TRUCKING_ORDER"


def test_system_prompt_does_not_duplicate_schema_or_display_reference():
    system = build_system_prompt(detect_prompt_route("做箱通知书 ESFF21030474"))

    assert "# 托书统一 JSON Schema" not in system
    assert "# 运输委托书展示格式约束" not in system
    assert "# 字段来源表（唯一目标）" in system


def test_json_schema_falls_back_to_json_object(monkeypatch):
    monkeypatch.setattr("app.llm.client._json_schema_supported", None)
    calls: list[tuple[list[dict], dict]] = []

    def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs["response_format"]))
        if kwargs["response_format"]["type"] == "json_schema":
            raise LLMError(
                "unsupported response format",
                code="llm_response_format_unsupported",
            )
        return '{"ok": true}', {"model": "fake", "usage": None}

    monkeypatch.setattr("app.llm.client.chat", fake_chat)
    data, _meta = chat_json([], json_schema={"type": "object"})

    assert data == {"ok": True}
    assert [response_format["type"] for _, response_format in calls] == [
        "json_schema",
        "json_object",
    ]
    assert calls[0][0] == []
    assert '"type":"object"' in calls[1][0][0]["content"]


def test_json_schema_fallback_is_cached_after_first_failure(monkeypatch):
    """首次失败后缓存网关能力，后续调用直接走 json_object，不再重复失败。"""
    monkeypatch.setattr("app.llm.client._json_schema_supported", None)
    calls: list[tuple[list[dict], dict]] = []

    def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs["response_format"]))
        if kwargs["response_format"]["type"] == "json_schema":
            raise LLMError(
                "unsupported response format",
                code="llm_response_format_unsupported",
            )
        return '{"ok": true}', {"model": "fake", "usage": None}

    monkeypatch.setattr("app.llm.client.chat", fake_chat)

    chat_json([], json_schema={"type": "object"})
    assert llm_client._json_schema_supported is False
    data, _meta = chat_json([], json_schema={"type": "object"})

    assert data == {"ok": True}
    assert [response_format["type"] for _, response_format in calls] == [
        "json_schema",
        "json_object",
        "json_object",
    ]


def test_chat_json_rejects_non_object(monkeypatch):
    monkeypatch.setattr(
        "app.llm.client.chat",
        lambda _messages, **_kwargs: ("[]", {"model": "fake", "usage": None}),
    )

    with pytest.raises(ParseError, match="JSON object"):
        chat_json([])
