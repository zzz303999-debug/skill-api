"""元信息路由：skill 列表、请求访问日志与第三方接口日志的查询数据接口及页面。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from app import access_log, third_party_log
from app.core import registry
from app.core.skill_base import SkillMeta

router = APIRouter()

# app/static 静态页目录（原 main.py 以 parent/static 定位，拆分后按包层级回溯）
_STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


@router.get("/skills", response_model=list[SkillMeta], tags=["meta"])
def list_skills() -> list[SkillMeta]:
    return [s.meta() for s in registry.all_skills()]


@router.get("/api/logs", tags=["meta"])
def list_request_logs(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    path: str | None = Query(default=None),
    file: str | None = Query(default=None),
    status: int | None = Query(default=None, ge=100, le=599),
    request_id: str | None = Query(default=None),
    ip: str | None = Query(default=None),
    include_full: bool = Query(default=False),
) -> dict:
    """请求访问日志列表（时间倒序），支持分页与过滤。

    include_full=true 时返回摘要化条目的完整响应体 response_full
    （大字段，仅导出/取证场景使用；列表默认剥离以保持页面轻量）。
    """
    return access_log.query(
        limit=limit,
        offset=offset,
        path=path,
        file=file,
        status=status,
        request_id=request_id,
        ip=ip,
        include_full=include_full,
    )


@router.get("/logs", include_in_schema=False)
def request_logs_page() -> FileResponse:
    """内置的请求日志查看页面。"""
    return FileResponse(_STATIC_DIR / "logs.html")


@router.get("/api/third-party-logs", tags=["meta"])
def list_third_party_logs(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    message: str | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    status: int | None = Query(default=None, ge=100, le=599),
    status_min: int | None = Query(default=None, ge=100, le=599),
    status_max: int | None = Query(default=None, ge=100, le=599),
    level: str | None = Query(default=None),
) -> dict:
    """第三方接口调用日志列表（时间倒序），支持按事件/端点/状态码/级别过滤。

    数据源为 http_client 落盘的 third-party-YYYY-MM-DD.jsonl（内存缓冲查询），
    含请求/响应体摘要（body_preview，含业务 PII）——与 /api/logs 同级别鉴权。
    """
    return third_party_log.query(
        limit=limit,
        offset=offset,
        message=message,
        endpoint=endpoint,
        status=status,
        status_min=status_min,
        status_max=status_max,
        level=level,
    )


@router.get("/third-party-logs", include_in_schema=False)
def third_party_logs_page() -> FileResponse:
    """内置的第三方接口调用日志查看页面（页面无数据，鉴权豁免；数据接口需 Key）。"""
    return FileResponse(_STATIC_DIR / "third_party_logs.html")
