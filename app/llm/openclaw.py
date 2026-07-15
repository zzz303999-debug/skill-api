"""OpenClaw 网关 LLM 客户端（OpenAI 兼容协议）。

所有 skill 通过 `chat()` 统一调用，禁止绕过。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from openai import OpenAI
from openai import APIError, APITimeoutError, APIConnectionError

from app.config import settings
from app.errors import LLMError
from app.logging_conf import get_logger

log = get_logger(__name__)

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.openclaw_base_url,
            api_key=settings.openclaw_api_key,
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
        raise LLMError(f"LLM gateway network error: {e}", code="llm_network") from e
    except APIError as e:
        raise LLMError(f"LLM gateway error: {e}", code="llm_upstream") from e
    except Exception as e:
        raise LLMError(f"LLM unexpected error: {e}") from e

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
) -> tuple[dict, dict]:
    """要求模型输出 JSON 对象。返回 (parsed_dict, meta)。"""
    content, meta = chat(
        messages,
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
                raise LLMError("LLM did not return valid JSON", details={"raw": content[:500]})
        else:
            raise LLMError("LLM did not return valid JSON", details={"raw": content[:500]})
    return data, meta
