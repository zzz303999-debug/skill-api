"""错误码中文说明注册表测试：接口层每个错误码必须有可判断的中文 description。"""

from __future__ import annotations

from app.core.errors import ERROR_CODE_DESCRIPTIONS, SkillAPIError

# 接口层会产生/使用的错误码全集（代码显式 code= 处 + 默认 code）
_EXPECTED_CODES = (
    "internal_error",
    "bad_request",
    "file_too_large",
    "empty_file",
    "text_too_large",
    "empty_batch",
    "too_many_files",
    "unsupported_image_content",
    "file_format_mismatch",
    "skill_not_found",
    "convert_error",
    "pdf_page_limit_exceeded",
    "pdf_parse_failed",
    "empty_converted_content",
    "order_not_ready",
    "order_text_parse_failed",
    "order_upstream_error",
    "rate_limited",
    "parse_error",
    "llm_error",
    "llm_upstream",
    "llm_response_format_unsupported",
    "llm_network",
)


def test_every_known_code_has_chinese_description():
    """接口层所有错误码必须登记中文说明，否则调用方无法判断错误含义。"""
    for code in _EXPECTED_CODES:
        assert code in ERROR_CODE_DESCRIPTIONS, f"错误码 {code} 缺少中文说明"
        assert ERROR_CODE_DESCRIPTIONS[code].strip(), f"错误码 {code} 的中文说明为空"


def test_error_description_comes_from_registry():
    exc = SkillAPIError("LLM request failed", code="llm_error")
    assert exc.description == ERROR_CODE_DESCRIPTIONS["llm_error"]
    assert "文件类型不受支持" in exc.description


def test_explicit_description_overrides_registry():
    exc = SkillAPIError("boom", code="llm_error", description="自定义说明")
    assert exc.description == "自定义说明"


def test_unknown_code_falls_back_to_message():
    exc = SkillAPIError("boom", code="brand_new_code")
    assert exc.description == "boom"
