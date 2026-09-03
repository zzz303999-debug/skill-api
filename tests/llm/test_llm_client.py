"""LLM 客户端错误处理规范测试：所有 LLMError 必须携带可排查的 details。"""

from __future__ import annotations

import httpx
import pytest
from openai import BadRequestError as OpenAIBadRequestError

from app.llm import client as llm_client


def _fake_gateway_error(status_code: int, message: str) -> OpenAIBadRequestError:
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "http://llm.test/v1/chat/completions"),
        json={"error": {"message": message, "type": "server_error"}},
    )
    return OpenAIBadRequestError(message, response=response, body=None)


def _stub_gateway(monkeypatch, error: Exception) -> None:
    """用抛出固定异常的假网关替换真实客户端，避免网络调用。"""

    class FakeCompletions:
        def create(self, **kwargs):
            raise error

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(llm_client, "get_client", lambda: FakeClient())
    # 关闭 thinking 注入，避免降级重试逻辑干扰断言
    monkeypatch.setattr("app.core.config.settings.llm_thinking_mode", "enabled")


def test_upstream_rejection_carries_status_and_message(monkeypatch):
    """llm_upstream 错误必须携带上游状态码与错误摘要，便于区分限流/5xx。"""
    _stub_gateway(monkeypatch, _fake_gateway_error(429, "rate limited by gateway"))

    with pytest.raises(llm_client.LLMError) as exc_info:
        llm_client.chat([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == "llm_upstream"
    assert exc_info.value.details["upstream_status"] == 429
    assert "rate limited" in exc_info.value.details["upstream_message"]
    assert exc_info.value.details["error_type"] == "BadRequestError"


def test_upstream_error_message_is_truncated(monkeypatch):
    """上游错误文本超长时必须截断，避免超大响应体刷响应。"""
    long_message = "x" * 2000
    _stub_gateway(monkeypatch, _fake_gateway_error(500, long_message))

    with pytest.raises(llm_client.LLMError) as exc_info:
        llm_client.chat([{"role": "user", "content": "hi"}])

    assert len(exc_info.value.details["upstream_message"]) <= llm_client._UPSTREAM_ERROR_MAX_CHARS


def test_response_format_error_keeps_specific_code(monkeypatch):
    """结构化输出不支持的网关错误仍归 llm_response_format_unsupported，并带 details。"""
    _stub_gateway(
        monkeypatch,
        _fake_gateway_error(400, "The model does not support json_schema response_format"),
    )

    with pytest.raises(llm_client.LLMError) as exc_info:
        llm_client.chat([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == "llm_response_format_unsupported"
    assert exc_info.value.details["upstream_status"] == 400


def test_chat_json_invalid_json_reports_content_preview(monkeypatch):
    """LLM 返回非 JSON 时，ParseError 应带内容长度与预览便于定位格式问题。"""

    class FakeChoices:
        message = type("Msg", (), {"content": "not json at all"})

    class FakeResponse:
        choices = [FakeChoices()]
        model = "fake"

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(llm_client, "get_client", lambda: FakeClient())
    monkeypatch.setattr("app.core.config.settings.llm_thinking_mode", "enabled")

    from app.core.errors import ParseError

    with pytest.raises(ParseError) as exc_info:
        llm_client.chat_json([{"role": "user", "content": "hi"}])

    assert exc_info.value.details["content_length"] == len("not json at all")
    assert exc_info.value.details["content_preview"] == "not json at all"
