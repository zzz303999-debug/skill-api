"""FastAPI 入口。

启动时：
1. 加载 logging
2. discover() 自动扫描 app.skills 下所有子包并注册
3. 为每个已注册的 skill 动态挂一条 `POST /skills/{name}/extract` 路由，
   并把 skill 自己的 `output_model` 作为 response schema，OpenAPI 自动生成精确文档。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Annotated, Any

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, create_model

from app.config import settings
from app.core import registry
from app.core.skill_base import SkillBase, SkillMeta
from app.errors import BadRequestError, SkillAPIError
from app.logging_conf import get_logger, setup_logging
from app.orders import (
    CreateOrderFromTextRequest,
    CreateOrderFromTextResponse,
    build_order_data,
    extract_order_text,
    parse_source_fields,
    publish_create_order,
    validate_order_api_config,
)

setup_logging()
log = get_logger(__name__)

app = FastAPI(
    title="skill-api",
    version="0.1.0",
    description="Multi-skill extraction API service backed by an OpenAI-compatible LLM.",
)

# Skill.run 是同步契约，统一放到有界线程池，避免文件转换和 LLM 请求阻塞事件循环，
# 同时限制对 LLM 服务的并发压力。
_skill_executor = ThreadPoolExecutor(
    max_workers=settings.skill_max_concurrency,
    thread_name_prefix="skill-runner",
)


@app.exception_handler(SkillAPIError)
async def _skill_api_error_handler(_: Request, exc: SkillAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    return {"status": "ok", "skills": [s.name for s in registry.all_skills()]}


@app.get("/skills", response_model=list[SkillMeta], tags=["meta"])
def list_skills() -> list[SkillMeta]:
    return [s.meta() for s in registry.all_skills()]


def _typed_response_model(skill: SkillBase) -> type[BaseModel]:
    """为每个 skill 动态生成一个精确类型的响应模型：
    data 字段的类型 = skill.output_model，这样 OpenAPI 就能显示精确 schema。
    """
    data_type: Any = skill.output_model if skill.output_model else dict
    fields: dict[str, Any] = {
        "skill": (str, skill.name),
        "version": (str, skill.version),
        "data": (data_type, ...),
        "meta": (dict, Field(default_factory=dict)),
    }
    if skill.include_content:
        fields["content"] = (str, ...)
    return create_model(
        f"{skill.name.title().replace('-', '')}Response",
        **fields,
        __base__=BaseModel,
    )


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read(settings.api_max_upload_bytes + 1)
    if len(content) > settings.api_max_upload_bytes:
        raise BadRequestError(
            "uploaded file is too large",
            code="file_too_large",
            details={"max_bytes": settings.api_max_upload_bytes},
        )
    if not content:
        raise BadRequestError(
            "uploaded file is empty",
            code="empty_file",
            details={"file": file.filename or "unnamed"},
        )
    return content


async def _run_skill(skill: SkillBase, content: bytes, filename: str) -> dict:
    loop = asyncio.get_running_loop()
    call = partial(
        skill.run,
        file_bytes=content,
        filename=filename,
        options=None,
    )
    return await loop.run_in_executor(_skill_executor, call)


async def _publish_order(order_data: dict[str, Any], *, room_id: str) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _skill_executor,
        partial(publish_create_order, order_data, room_id=room_id),
    )


async def _extract_order_text(text: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _skill_executor,
        partial(extract_order_text, text),
    )


@app.post(
    "/orders",
    response_model=CreateOrderFromTextResponse,
    tags=["orders"],
    summary="Extract and create an order from free text",
)
async def create_order_from_text(body: CreateOrderFromTextRequest) -> dict[str, Any]:
    validate_order_api_config()
    text = body.content.strip()
    encoded = text.encode("utf-8")
    if len(encoded) > settings.api_max_upload_bytes:
        raise BadRequestError(
            "text is too large",
            code="text_too_large",
            details={"max_bytes": settings.api_max_upload_bytes},
        )

    extracted, meta = await _extract_order_text(text)
    order_data = build_order_data(
        extracted,
        customer_id=settings.order_api_jxt_open_id,
    )
    upstream = await _publish_order(order_data, room_id=body.roomId)
    return {
        "roomId": body.roomId,
        "source_fields": parse_source_fields(text),
        "extracted": extracted.model_dump(),
        "order_data": order_data,
        "upstream": upstream,
        "meta": meta,
    }


def _make_extract_route(skill: SkillBase):
    """为一个 skill 生成一个 handler。闭包捕获 skill 实例。"""

    async def handler(
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        content = await _read_upload(file)
        out = await _run_skill(skill, content, file.filename or "unnamed")
        response = {
            "skill": skill.name,
            "version": skill.version,
            "data": out["result"],
            "meta": out.get("meta", {}),
        }
        if skill.include_content:
            response["content"] = out.get("content", "")
        return response

    return handler


def _make_batch_extract_route(skill: SkillBase):
    """为一个 skill 生成批量处理 handler。接收多个文件，并行识别后汇总。"""

    async def _run_one(f: UploadFile) -> dict:
        try:
            content = await _read_upload(f)
            out = await _run_skill(skill, content, f.filename or "unnamed")
            result = {
                "file": f.filename or "unnamed",
                "result": out["result"],
                "meta": out.get("meta", {}),
            }
            if skill.include_content:
                result["content"] = out.get("content", "")
            return result
        except SkillAPIError as e:
            return {
                "file": f.filename or "unnamed",
                "error": {"code": e.code, "message": e.message, "details": e.details},
            }
        except Exception:
            log.exception("batch_skill_failed", extra={"file": f.filename or "unnamed"})
            return {
                "file": f.filename or "unnamed",
                "error": {"code": "internal_error", "message": "skill execution failed"},
            }

    async def handler(
        files: Annotated[list[UploadFile], File()],
    ) -> dict[str, Any]:
        if len(files) > settings.api_batch_max_files:
            raise BadRequestError(
                "too many files in one batch",
                code="too_many_files",
                details={"max_files": settings.api_batch_max_files},
            )
        # 并行执行所有文件识别
        raw_results = await asyncio.gather(*[_run_one(f) for f in files])
        results = [r for r in raw_results if "error" not in r]
        errors = [r for r in raw_results if "error" in r]
        return {
            "skill": skill.name,
            "version": skill.version,
            "total": len(files),
            "success": len(results),
            "failed": len(errors),
            "results": results,
            "errors": errors,
        }

    return handler


def _register_skill_routes() -> None:
    registry.discover()
    for skill in registry.all_skills():
        handler = _make_extract_route(skill)
        response_model = _typed_response_model(skill)
        app.add_api_route(
            f"/skills/{skill.name}/extract",
            handler,
            methods=["POST"],
            tags=[f"skill:{skill.name}"],
            summary=f"Run skill: {skill.name}",
            description=skill.description,
            response_model=response_model,
        )
        log.info("route_mounted", extra={"path": f"/skills/{skill.name}/extract"})

        # 批量接口
        batch_handler = _make_batch_extract_route(skill)
        app.add_api_route(
            f"/skills/{skill.name}/batch-extract",
            batch_handler,
            methods=["POST"],
            tags=[f"skill:{skill.name}"],
            summary=f"Batch run skill: {skill.name}",
            description=f"批量处理多个文件，逐个调用 {skill.name} skill 识别后汇总。",
        )
        log.info("route_mounted", extra={"path": f"/skills/{skill.name}/batch-extract"})


_register_skill_routes()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
