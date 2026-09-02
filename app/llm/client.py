"""OpenAI-compatible LLM client.

所有 skill 通过 `chat()` 统一调用，禁止绕过。

异步版本（achat / achat_json，2026-09 异步化改造新增）：与同步版逻辑逐项
对齐（同 thinking 注入/降级重试、同 json_schema 降级、同错误分类），
探测缓存（_json_schema_supported/_thinking_disabled_supported）与同步版
共享，参数（timeout/max_retries）一致；同步版本保留至异步链路全部切换后
统一清理（双轨过渡）。
"""

from __future__ import annotations

import base64
import json
import threading
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAI,
)

from app.config import settings
from app.errors import LLMError, ParseError
from app.logging_conf import get_logger

log = get_logger(__name__)

_client: OpenAI | None = None
# 网关是否支持 json_schema structured output；None=未知，True/False=已探测
_json_schema_supported: bool | None = None
# 网关/模型是否接受 thinking 参数；None=未知，False=已确认不支持（降级重试后缓存）
_thinking_disabled_supported: bool | None = None
# 上述全局状态的互斥锁（skill 在多个线程池线程中并发调用）
_state_lock = threading.Lock()

# 上游错误文本截断长度，避免超大响应体刷日志/响应
_UPSTREAM_ERROR_MAX_CHARS = 500


def _upstream_details(e: Exception, *, status: int | None = None) -> dict[str, Any]:
    """构造统一的上游错误 details：状态码 + 截断的错误摘要 + 异常类型。"""
    return {
        "error_type": e.__class__.__name__,
        "upstream_status": status,
        "upstream_message": str(e)[:_UPSTREAM_ERROR_MAX_CHARS],
    }


def get_client() -> OpenAI:
    global _client
    if _client is None:
        with _state_lock:
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


def _thinking_parameter_rejected(error_text: str) -> bool:
    """判断 API 错误是否由 thinking 参数不被支持引起。"""
    return "thinking" in error_text or "reasoning" in error_text


def _classify_api_error(e: APIError) -> None:
    """把 APIError 分类为响应格式不支持或其他上游错误。"""
    error_text = str(e).lower()
    response_format_error = (
        "response_format" in error_text
        or "json_schema" in error_text
        or "structured output" in error_text
    )
    code = "llm_response_format_unsupported" if response_format_error else "llm_upstream"
    details = _upstream_details(e, status=getattr(e, "status_code", None))
    log.warning("llm_upstream_error", extra={"code": code, "details": details}, exc_info=True)
    raise LLMError("LLM gateway rejected the request", code=code, details=details) from e


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
    当配置 llm_thinking_mode="disabled" 时默认注入 thinking: disabled 以降低
    reasoning token 与响应耗时；调用方显式传入 thinking/reasoning_effort 时
    以调用方为准。网关/模型不支持该参数时会自动去掉重试一次，并缓存结果。
    """
    model = model or settings.llm_model_default
    global _thinking_disabled_supported
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if extra_body:
        # 拷贝后使用，避免后续注入 thinking 等参数时污染调用方传入的 dict
        kwargs["extra_body"] = dict(extra_body)

    thinking_injected = False
    if (
        settings.llm_thinking_mode == "disabled"
        and _thinking_disabled_supported is not False
        and "thinking" not in (extra_body or {})
        and "reasoning_effort" not in (extra_body or {})
    ):
        kwargs.setdefault("extra_body", {})["thinking"] = {"type": "disabled"}
        thinking_injected = True

    try:
        resp = get_client().chat.completions.create(**kwargs)
    except (APITimeoutError, APIConnectionError) as e:
        log.warning("llm_network_error", exc_info=True)
        raise LLMError(
            "LLM gateway network error",
            code="llm_network",
            details=_upstream_details(e),
        ) from e
    except APIError as e:
        if thinking_injected and _thinking_parameter_rejected(str(e).lower()):
            # 网关/模型不接受 thinking 参数：去掉后重试一次，后续请求不再注入
            _thinking_disabled_supported = False
            log.warning("llm_thinking_disabled_fallback", extra={"model": model})
            kwargs["extra_body"] = {
                key: value
                for key, value in kwargs.get("extra_body", {}).items()
                if key != "thinking"
            }
            try:
                resp = get_client().chat.completions.create(**kwargs)
            except (APITimeoutError, APIConnectionError) as retry_err:
                log.warning("llm_network_error", exc_info=True)
                raise LLMError(
                    "LLM gateway network error",
                    code="llm_network",
                    details=_upstream_details(retry_err),
                ) from retry_err
            except APIError as retry_err:
                _classify_api_error(retry_err)
        else:
            _classify_api_error(e)
    except Exception as e:
        log.exception("llm_unexpected_error")
        raise LLMError(
            "LLM request failed",
            code="llm_error",
            details=_upstream_details(e),
        ) from e

    try:
        content = resp.choices[0].message.content or ""
    except (IndexError, AttributeError) as e:
        raise LLMError(
            f"LLM response has no content: {e}",
            code="llm_error",
            details=_upstream_details(e),
        ) from e

    meta = {
        "model": getattr(resp, "model", model),
        "usage": getattr(resp, "usage", None).model_dump()
        if getattr(resp, "usage", None)
        else None,
    }
    log.info("llm_call_ok", extra={"model": meta["model"], "usage": meta["usage"]})
    return content, meta


def _json_object_fallback_messages(
    messages: list[dict[str, Any]], json_schema: dict
) -> list[dict[str, Any]]:
    """构造 json_object 降级消息：插入 schema 指令，要求模型按 schema 输出。"""
    schema_instruction = {
        "role": "system",
        "content": (
            "网关不支持 structured output。仍须严格按以下 JSON Schema 输出对象：\n"
            + json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
        ),
    }
    insert_at = 1 if messages and messages[0].get("role") == "system" else 0
    return [
        *messages[:insert_at],
        schema_instruction,
        *messages[insert_at:],
    ]


def chat_json(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    json_schema: dict | None = None,
) -> tuple[dict, dict]:
    """要求模型输出 JSON 对象。返回 (parsed_dict, meta)。

    当提供 json_schema 时，优先使用 structured output 模式
    （json_schema）强制模型按指定字段名和类型输出；
    网关不支持时降级为 json_object + schema 指令。
    降级能力会被缓存，避免每次调用都先失败一次。
    """
    global _json_schema_supported
    if json_schema and _json_schema_supported is False:
        # 已确认网关不支持 json_schema，直接走降级路径
        fallback_messages = _json_object_fallback_messages(messages, json_schema)
        content, meta = chat(
            fallback_messages,
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
    else:
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
            if json_schema:
                _json_schema_supported = True
        except LLMError as e:
            if not json_schema or e.code != "llm_response_format_unsupported":
                raise
            _json_schema_supported = False
            log.warning("llm_json_schema_fallback", extra={"model": model})
            fallback_messages = _json_object_fallback_messages(messages, json_schema)
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
                raise ParseError(
                    "LLM did not return valid JSON",
                    details={
                        "content_length": len(content),
                        "content_preview": stripped[:200],
                    },
                )
        else:
            log.warning("llm_invalid_json", extra={"content_length": len(content)})
            raise ParseError(
                "LLM did not return valid JSON",
                details={
                    "content_length": len(content),
                    "content_preview": stripped[:200],
                },
            ) from None
    if not isinstance(data, dict):
        raise ParseError("LLM output must be a JSON object")
    return data, meta


# ---- 异步版本（与同步版逻辑逐项对齐；Phase 2 异步化改造新增）----

_async_client: AsyncOpenAI | None = None


def get_async_client() -> AsyncOpenAI:
    """AsyncOpenAI 懒加载单例（参数与同步版一致；探测缓存两版共享）。"""
    global _async_client
    if _async_client is None:
        with _state_lock:
            if _async_client is None:
                _async_client = AsyncOpenAI(
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    timeout=settings.llm_timeout_seconds,
                    max_retries=settings.llm_max_retries,
                )
    return _async_client


async def achat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    response_format: dict | None = None,
    extra_body: dict | None = None,
) -> tuple[str, dict]:
    """achat：chat 的异步版（逻辑/错误分类/降级重试与 chat 逐项对齐）。"""
    model = model or settings.llm_model_default
    global _thinking_disabled_supported
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if extra_body:
        # 拷贝后使用，避免后续注入 thinking 等参数时污染调用方传入的 dict
        kwargs["extra_body"] = dict(extra_body)

    thinking_injected = False
    if (
        settings.llm_thinking_mode == "disabled"
        and _thinking_disabled_supported is not False
        and "thinking" not in (extra_body or {})
        and "reasoning_effort" not in (extra_body or {})
    ):
        kwargs.setdefault("extra_body", {})["thinking"] = {"type": "disabled"}
        thinking_injected = True

    try:
        resp = await get_async_client().chat.completions.create(**kwargs)
    except (APITimeoutError, APIConnectionError) as e:
        log.warning("llm_network_error", exc_info=True)
        raise LLMError(
            "LLM gateway network error",
            code="llm_network",
            details=_upstream_details(e),
        ) from e
    except APIError as e:
        if thinking_injected and _thinking_parameter_rejected(str(e).lower()):
            # 网关/模型不接受 thinking 参数：去掉后重试一次，后续请求不再注入
            _thinking_disabled_supported = False
            log.warning("llm_thinking_disabled_fallback", extra={"model": model})
            kwargs["extra_body"] = {
                key: value
                for key, value in kwargs.get("extra_body", {}).items()
                if key != "thinking"
            }
            try:
                resp = await get_async_client().chat.completions.create(**kwargs)
            except (APITimeoutError, APIConnectionError) as retry_err:
                log.warning("llm_network_error", exc_info=True)
                raise LLMError(
                    "LLM gateway network error",
                    code="llm_network",
                    details=_upstream_details(retry_err),
                ) from retry_err
            except APIError as retry_err:
                _classify_api_error(retry_err)
        else:
            _classify_api_error(e)
    except Exception as e:
        log.exception("llm_unexpected_error")
        raise LLMError(
            "LLM request failed",
            code="llm_error",
            details=_upstream_details(e),
        ) from e

    try:
        content = resp.choices[0].message.content or ""
    except (IndexError, AttributeError) as e:
        raise LLMError(
            f"LLM response has no content: {e}",
            code="llm_error",
            details=_upstream_details(e),
        ) from e

    meta = {
        "model": getattr(resp, "model", model),
        "usage": getattr(resp, "usage", None).model_dump()
        if getattr(resp, "usage", None)
        else None,
    }
    log.info("llm_call_ok", extra={"model": meta["model"], "usage": meta["usage"]})
    return content, meta


async def achat_json(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    json_schema: dict | None = None,
) -> tuple[dict, dict]:
    """achat_json：chat_json 的异步版（json_schema 降级逻辑与缓存与同步版共享）。"""
    global _json_schema_supported
    if json_schema and _json_schema_supported is False:
        # 已确认网关不支持 json_schema，直接走降级路径
        fallback_messages = _json_object_fallback_messages(messages, json_schema)
        content, meta = await achat(
            fallback_messages,
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
    else:
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
            content, meta = await achat(
                messages,
                model=model,
                temperature=temperature,
                response_format=response_format,
            )
            if json_schema:
                _json_schema_supported = True
        except LLMError as e:
            if not json_schema or e.code != "llm_response_format_unsupported":
                raise
            _json_schema_supported = False
            log.warning("llm_json_schema_fallback", extra={"model": model})
            fallback_messages = _json_object_fallback_messages(messages, json_schema)
            content, meta = await achat(
                fallback_messages,
                model=model,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 兜底：从 ```json ... ``` 中抽（与 chat_json 同一处理，详见同步版注释）
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
                raise ParseError(
                    "LLM did not return valid JSON",
                    details={
                        "content_length": len(content),
                        "content_preview": stripped[:200],
                    },
                )
        else:
            log.warning("llm_invalid_json", extra={"content_length": len(content)})
            raise ParseError(
                "LLM did not return valid JSON",
                details={
                    "content_length": len(content),
                    "content_preview": stripped[:200],
                },
            ) from None
    if not isinstance(data, dict):
        raise ParseError("LLM output must be a JSON object")
    return data, meta
