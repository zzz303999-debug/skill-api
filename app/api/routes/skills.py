"""skill 动态路由：为每个已注册 skill 挂 extract / batch-extract 接口。"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import FastAPI, File, Request, UploadFile
from pydantic import BaseModel, Field, create_model

from app.api.response_shell import _use_unified_response
from app.api.uploads import _read_upload
from app.core import skill_registry
from app.core.config import settings
from app.core.errors import (
    ERROR_CODE_DESCRIPTIONS,
    BadRequestError,
    SkillAPIError,
)
from app.core.executor import _run_skill
from app.core.logging_conf import get_logger
from app.core.skill_base import SkillBase

log = get_logger(__name__)


def _typed_response_model(skill: SkillBase) -> type[BaseModel]:
    """为每个 skill 动态生成一个精确类型的响应模型：
    data 字段的类型 = skill.output_model，这样 OpenAPI 就能显示精确 schema。
    统一外壳路径（_UNIFIED_RESPONSE_PATHS 内的 skill 接口，当前仅
    /skills/tuoshu/extract）生成 {code, msg, data} 外壳模型，data 内为
    {skill, version, result, meta, content?}（result 即原 data 抽取结果）；
    其余 skill 保持原 {skill, version, data, meta, content?} 结构。
    """
    data_type: Any = skill.output_model if skill.output_model else dict
    model_name = f"{skill.name.title().replace('-', '')}Response"
    if _use_unified_response(f"/skills/{skill.name}/extract"):
        inner_fields: dict[str, Any] = {
            "skill": (str, skill.name),
            "version": (str, skill.version),
            "result": (data_type, ...),
            "meta": (dict, Field(default_factory=dict)),
        }
        if skill.include_content:
            inner_fields["content"] = (str, ...)
        inner = create_model(f"{model_name}Data", **inner_fields, __base__=BaseModel)
        return create_model(
            model_name,
            code=(str, ...),
            msg=(str, ...),
            data=(inner, ...),
            __base__=BaseModel,
        )
    fields: dict[str, Any] = {
        "skill": (str, skill.name),
        "version": (str, skill.version),
        "data": (data_type, ...),
        "meta": (dict, Field(default_factory=dict)),
    }
    if skill.include_content:
        fields["content"] = (str, ...)
    return create_model(model_name, **fields, __base__=BaseModel)


def _make_extract_route(skill: SkillBase):
    """为一个 skill 生成一个 handler。闭包捕获 skill 实例。"""

    async def handler(
        file: Annotated[UploadFile, File()],
        request: Request,
    ) -> dict[str, Any]:
        # 先设置文件名，读取失败时访问日志也能记录到 file
        request.state.file_name = file.filename or "unnamed"
        content = await _read_upload(file)
        request.state.file_size = len(content)
        out = await _run_skill(skill, content, file.filename or "unnamed")
        if _use_unified_response(request.url.path):
            # 统一外壳（当前仅 /skills/tuoshu/extract）：{code, msg, data}，
            # data 内 {skill, version, result, meta, content?}（result 即原
            # data 抽取结果），错误场景由全局异常处理器套同一外壳
            payload = {
                "skill": skill.name,
                "version": skill.version,
                "result": out["result"],
                "meta": out.get("meta", {}),
            }
            if skill.include_content:
                payload["content"] = out.get("content", "")
            return {"code": "200", "msg": "解析成功", "data": payload}
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

    async def handler(
        files: Annotated[list[UploadFile], File()],
        request: Request,
    ) -> dict[str, Any]:
        request.state.file_name = ", ".join(f.filename or "unnamed" for f in files)
        request.state.file_size = 0
        if not files:
            raise BadRequestError(
                "no files uploaded in the batch",
                code="empty_batch",
                details={"min_files": 1},
            )
        if len(files) > settings.api_batch_max_files:
            raise BadRequestError(
                "too many files in one batch",
                code="too_many_files",
                details={"max_files": settings.api_batch_max_files},
            )

        async def run_one(f: UploadFile) -> dict:
            try:
                content = await _read_upload(f)
                request.state.file_size += len(content)
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
                    "error": {
                        "code": e.code,
                        "message": e.message,
                        "description": e.description,
                        "details": e.details,
                    },
                }
            except Exception:
                log.exception("batch_skill_failed", extra={"file": f.filename or "unnamed"})
                return {
                    "file": f.filename or "unnamed",
                    "error": {
                        "code": "internal_error",
                        "message": "skill execution failed",
                        "description": ERROR_CODE_DESCRIPTIONS["internal_error"],
                    },
                }

        # 并行执行所有文件识别
        raw_results = await asyncio.gather(*[run_one(f) for f in files])
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


def register_skill_routes(app: FastAPI) -> None:
    """为全部已注册 skill 挂载 extract / batch-extract 路由（create_app 时调用）。"""
    skill_registry.discover()
    for skill in skill_registry.all_skills():
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
