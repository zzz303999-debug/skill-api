"""FastAPI 入口。

启动时：
1. 加载 logging
2. discover() 自动扫描 app.skills 下所有子包并注册
3. 为每个已注册的 skill 动态挂一条 `POST /skills/{name}/extract` 路由，
   并把 skill 自己的 `output_model` 作为 response schema，OpenAPI 自动生成精确文档。
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, create_model

from app.config import settings
from app.core import registry
from app.core.skill_base import SkillBase, SkillMeta, SkillResponse
from app.deps import require_api_key
from app.errors import SkillAPIError
from app.logging_conf import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)

app = FastAPI(
    title="skill-api",
    version="0.1.0",
    description="Multi-skill extraction API service backed by OpenClaw gateway.",
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
    return create_model(
        f"{skill.name.title().replace('-', '')}Response",
        skill=(str, skill.name),
        version=(str, skill.version),
        data=(data_type, ...),
        meta=(dict, Field(default_factory=dict)),
        __base__=BaseModel,
    )


def _make_extract_route(skill: SkillBase):
    """为一个 skill 生成一个 handler。闭包捕获 skill 实例。"""

    async def handler(
        file: UploadFile = File(...),
        _: None = Depends(require_api_key),
    ) -> dict[str, Any]:
        content = await file.read()
        out = skill.run(file_bytes=content, filename=file.filename or "unnamed", options=None)
        return {
            "skill": skill.name,
            "version": skill.version,
            "data": out["result"],
            "meta": out.get("meta", {}),
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


_register_skill_routes()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
