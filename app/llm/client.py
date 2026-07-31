"""OpenAI-compatible LLM client.

所有 skill 通过 `chat()` 统一调用，禁止绕过。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

from app.config import settings
from app.errors import LLMError, ParseError
from app.logging_conf import get_logger

log = get_logger(__name__)

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    return _client


def image_to_data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    response_format: dict | None = None,
    extra_body: dict | None = None,
) -> tuple[str, dict]:
    """调用一次 chat/completions。

    返回 (content_text, meta)。meta 包含 model / usage。
    """
    model = model or settings.llm_model_default
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if extra_body:
        kwargs["extra_body"] = extra_body

    try:
        resp = get_client().chat.completions.create(**kwargs)
    except (APITimeoutError, APIConnectionError) as e:
        log.warning("llm_network_error", exc_info=True)
        raise LLMError("LLM gateway network error", code="llm_network") from e
    except APIError as e:
        error_text = str(e).lower()
        response_format_error = (
            "response_format" in error_text
            or "json_schema" in error_text
            or "structured output" in error_text
        )
        code = "llm_response_format_unsupported" if response_format_error else "llm_upstream"
        log.warning("llm_upstream_error", extra={"code": code}, exc_info=True)
        raise LLMError("LLM gateway rejected the request", code=code) from e
    except Exception as e:
        log.exception("llm_unexpected_error")
        raise LLMError("LLM request failed") from e

    try:
        content = resp.choices[0].message.content or ""
    except (IndexError, AttributeError) as e:
        raise LLMError(f"LLM response has no content: {e}") from e

    meta = {
        "model": getattr(resp, "model", model),
        "usage": getattr(resp, "usage", None).model_dump()
        if getattr(resp, "usage", None)
        else None,
    }
    log.info("llm_call_ok", extra={"model": meta["model"], "usage": meta["usage"]})
    return content, meta


def chat_json(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    json_schema: dict | None = None,
) -> tuple[dict, dict]:
    """要求模型输出 JSON 对象。返回 (parsed_dict, meta)。

    当提供 json_schema 时，使用 structured output 模式
    （json_schema），强制模型按指定字段名和类型输出。
    网关不支持时自动降级为 json_object。
    """
    response_format: dict = {"type": "json_object"}
    if json_schema:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "strict": False,
                "schema": json_schema,
            },
        }
    try:
        content, meta = chat(
            messages,
            model=model,
            temperature=temperature,
            response_format=response_format,
        )
    except LLMError as e:
        if not json_schema or e.code != "llm_response_format_unsupported":
            raise
        log.warning("llm_json_schema_fallback", extra={"model": model})
        schema_instruction = {
            "role": "system",
            "content": (
                "网关不支持 structured output。仍须严格按以下 JSON Schema 输出对象：\n"
                + json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
            ),
        }
        insert_at = 1 if messages and messages[0].get("role") == "system" else 0
        fallback_messages = [
            *messages[:insert_at],
            schema_instruction,
            *messages[insert_at:],
        ]
        content, meta = chat(
            fallback_messages,
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 兜底：从 ```json ... ``` 中抽
        stripped = content.strip()
        if "```" in stripped:
            parts = stripped.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("{"):
                    try:
                        data = json.loads(p)
                        break
                    except json.JSONDecodeError:
                        continue
            else:
                log.warning("llm_invalid_json", extra={"content_length": len(content)})
                raise ParseError("LLM did not return valid JSON")
        else:
            log.warning("llm_invalid_json", extra={"content_length": len(content)})
            raise ParseError("LLM did not return valid JSON") from None
    if not isinstance(data, dict):
        raise ParseError("LLM output must be a JSON object")
    return data, meta
