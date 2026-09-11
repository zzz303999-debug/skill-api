"""create_app()：FastAPI 应用组装（中间件、异常处理器、路由挂载）。

组装契约：
1. 中间件注册顺序即层级契约（后注册者为外层）：
   rate_limit → auth → access_log，执行序 access_log → auth → rate_limit——
   被限流/鉴权拒绝的请求仍经外层 access_log 审计留痕（各中间件 docstring 有说明）；
2. 路由挂载顺序保持既有定义顺序（OpenAPI paths 顺序不变）；
3. skill 动态路由最后注册（registry.discover() 扫描 app.skills 子包）。

仅 app.main 应调用本工厂（路由/中间件在调用时经 app.main 命名空间解析
测试接缝符号）；其它入口绕过 app.main 直接 create_app() 会导致接缝解析
时机异常，禁止。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api import error_handlers
from app.api.middleware.access_log import _access_log_middleware
from app.api.middleware.auth import _auth_middleware
from app.api.middleware.rate_limit import _rate_limit_middleware
from app.api.routes import bill_import, manifest_import, meta, orders
from app.api.routes.health import router as health_router
from app.api.routes.skills import register_skill_routes
from app.core.errors import SkillAPIError


def create_app() -> FastAPI:
    """组装 FastAPI 应用：中间件 → 异常处理器 → 路由 → skill 动态路由。"""
    app = FastAPI(
        title="skill-api",
        version="0.1.0",
        description="Multi-skill extraction API service backed by an OpenAI-compatible LLM.",
    )

    # 中间件：注册顺序 = 层级契约（后注册者为外层），见模块 docstring
    app.middleware("http")(_rate_limit_middleware)
    app.middleware("http")(_auth_middleware)
    app.middleware("http")(_access_log_middleware)

    # 全局异常处理器：统一外壳路径输出 {code, msg, data}，其余路径保持默认结构
    app.exception_handler(SkillAPIError)(error_handlers._skill_api_error_handler)
    app.exception_handler(RequestValidationError)(error_handlers._validation_error_handler)
    app.exception_handler(Exception)(error_handlers._generic_error_handler)

    # 路由：挂载顺序保持既有定义顺序（OpenAPI paths 顺序不变）
    app.include_router(health_router)
    app.include_router(meta.router)
    app.include_router(orders.router)
    app.include_router(bill_import.router)
    app.include_router(manifest_import.router)

    # skill 动态路由（extract / batch-extract）
    register_skill_routes(app)

    return app
