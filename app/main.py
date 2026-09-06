"""FastAPI 入口（纯组装，2026-09 P1 去上帝化）。

应用组装见 app/api/app_factory.py（分层：middleware / routes / bridges /
response_shell），启动时 create_app() 完成：
1. 加载 logging
2. 注册中间件与异常处理器、挂载全部路由
3. discover() 自动扫描 app.skills 下所有子包并注册，动态挂
   `POST /skills/{name}/extract` 与 batch-extract 路由

本模块不再承载测试接缝与 re-export（P1 起归位各宿主模块）：
- 鉴权豁免 / 限流白名单：app/api/middleware/{auth,rate_limit}.py
- 路由编排入口（publish_order / parse_document_to_order / build_result_async）：
  路由模块自身命名空间，测试以 setattr(路由模块, 符号名, ...) 注入替身
"""

from __future__ import annotations

from app.api.app_factory import create_app
from app.core.config import settings
from app.core.logging_conf import setup_logging

setup_logging()
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )
