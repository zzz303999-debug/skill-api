"""统一错误类型。"""

from __future__ import annotations


class SkillAPIError(Exception):
    """所有服务内错误的基类，带 http_status 和 code。"""

    http_status: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.message = message
        self.details = details or {}


class BadRequestError(SkillAPIError):
    http_status = 400
    code = "bad_request"


class UnauthorizedError(SkillAPIError):
    http_status = 401
    code = "unauthorized"


class SkillNotFoundError(SkillAPIError):
    http_status = 404
    code = "skill_not_found"


class ConvertError(SkillAPIError):
    http_status = 422
    code = "convert_error"


class LLMError(SkillAPIError):
    http_status = 502
    code = "llm_error"


class ParseError(SkillAPIError):
    http_status = 502
    code = "parse_error"
