"""元信息路由：skill 列表、请求访问日志与第三方接口日志的查询数据接口。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.core import access_log_store, skill_registry, third_party_log_store
from app.core.skill_base import SkillMeta

router = APIRouter()


@router.get("/skills", response_model=list[SkillMeta], tags=["meta"])
def list_skills() -> list[SkillMeta]:
    return [s.meta() for s in skill_registry.all_skills()]


@router.get("/api/logs", tags=["meta"])
def list_request_logs(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    path: str | None = Query(default=None),
    file: str | None = Query(default=None),
    status: int | None = Query(default=None, ge=100, le=599),
    status_min: int | None = Query(default=None, ge=100, le=599),
    status_max: int | None = Query(default=None, ge=100, le=599),
    code: str | None = Query(default=None),
    method: str | None = Query(default=None),
    min_duration_ms: float | None = Query(default=None, ge=0),
    ts_from: Annotated[datetime | None, Query()] = None,
    ts_to: Annotated[datetime | None, Query()] = None,
    request_id: str | None = Query(default=None),
    ip: str | None = Query(default=None),
    include_full: bool = Query(default=False),
) -> dict:
    """请求访问日志列表（时间倒序），支持分页与多维过滤。

    ts_from/ts_to 为 ISO8601 时间范围（闭区间；不带时区按 UTC 解释）；
    code 为统一外壳业务码（如 204/409，取自响应体）；status_min/max 为
    HTTP 状态码范围。include_full=true 时返回摘要化条目的完整响应体
    response_full（大字段，仅导出/取证场景使用；列表默认剥离以保持页面轻量）。
    """
    return access_log_store.query(
        limit=limit,
        offset=offset,
        path=path,
        file=file,
        status=status,
        status_min=status_min,
        status_max=status_max,
        code=code,
        method=method,
        min_duration_ms=min_duration_ms,
        ts_from=ts_from,
        ts_to=ts_to,
        request_id=request_id,
        ip=ip,
        include_full=include_full,
    )


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
    request_id: str | None = Query(default=None),
    # datetime 注解用 Annotated 风格（理由同 /api/logs 的 ts_from）
    ts_from: Annotated[datetime | None, Query()] = None,
    ts_to: Annotated[datetime | None, Query()] = None,
) -> dict:
    """第三方接口调用日志列表（时间倒序），支持多维度过滤。

    request_id 与 /api/logs 审计条同值——一次请求全链路排查（审计条定位
    request_id 后查出该请求触发的全部下游调用）；ts_from/ts_to 为 ISO8601
    时间范围（闭区间；不带时区按 UTC 解释）。

    数据源为 http_client 落盘的 third-party-YYYY-MM-DD.jsonl（内存缓冲查询），
    含请求/响应体摘要（body_preview，含业务 PII）——与 /api/logs 同级别鉴权。
    """
    return third_party_log_store.query(
        limit=limit,
        offset=offset,
        message=message,
        endpoint=endpoint,
        status=status,
        status_min=status_min,
        status_max=status_max,
        level=level,
        request_id=request_id,
        ts_from=ts_from,
        ts_to=ts_to,
    )

