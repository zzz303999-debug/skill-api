"""全局异常处理器：统一外壳路径输出 {code, msg, data}，其余路径保持默认结构。"""

from __future__ import annotations

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.response_shell import _unified_error_body, _use_unified_response
from app.errors import ERROR_CODE_DESCRIPTIONS, SkillAPIError
from app.logging_conf import get_logger

log = get_logger(__name__)


async def _skill_api_error_handler(request: Request, exc: SkillAPIError) -> JSONResponse:
    log.warning(
        "skill_api_error",
        extra={
            "code": exc.code,
            "error_message": exc.message,
            "description": exc.description,
            "details": exc.details,
        },
    )
    request.state.error_code = exc.code
    # 完整错误详情（含中文说明）透传访问日志，供审计导出错误信息
    request.state.error_detail = {
        "code": exc.code,
        "message": exc.message,
        "description": exc.description,
        "details": exc.details,
    }
    content = (
        _unified_error_body(exc.code, exc.description, exc.details)
        if _use_unified_response(request.url.path)
        else {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "description": exc.description,
                "details": exc.details,
            }
        }
    )
    return JSONResponse(status_code=exc.http_status, content=content)


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求参数校验失败（422）：统一外壳路径输出 {code, msg, data}（msg 直接
    可展示，data 携带完整字段错误明细）；其余路径保持 FastAPI 默认
    {"detail": [...]} 结构不变。errors 统一经 jsonable_encoder（Pydantic v2
    的 value_error 类 ctx 含异常实例，直接透传会 422 退化为 500）。"""
    if not _use_unified_response(request.url.path):
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
    return JSONResponse(
        status_code=422,
        content=_unified_error_body(
            "bad_request",
            ERROR_CODE_DESCRIPTIONS["bad_request"],
            {"errors": jsonable_encoder(exc.errors())},
        ),
    )


async def _generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """未包装异常兜底（审查修正 2026-08-27）：统一外壳路径（_UNIFIED_RESPONSE_PATHS
    内接口）返回 {code, msg, data}（msg 可直接展示），避免调用方在意外异常（httpx
    超时/解析器内部错误等）下拿到 FastAPI 默认 {"detail": ...} 破坏三字段契约；
    其余路径保持默认 {"detail": "Internal Server Error"} 行为不变。路由级
    404/405 由 Starlette 专用处理器处理不经过此处（统一路径 404/405 不可达，
    尾斜杠已 307 归一）。
    """
    if not _use_unified_response(request.url.path):
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    log.error("unhandled_error", extra={"exc": f"{exc.__class__.__name__}: {exc}"})
    request.state.error_code = "internal_error"
    request.state.error_detail = {
        "code": "internal_error",
        "message": str(exc),
        "description": ERROR_CODE_DESCRIPTIONS["internal_error"],
        "details": None,
    }
    return JSONResponse(
        status_code=500,
        content=_unified_error_body("internal_error", ERROR_CODE_DESCRIPTIONS["internal_error"]),
    )
