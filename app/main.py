"""FastAPI 入口。

应用组装见 app/api/app_factory.py（分层：middleware / routes / executor /
response_shell），启动时 create_app() 完成：
1. 加载 logging
2. 注册中间件与异常处理器、挂载全部路由
3. discover() 自动扫描 app.skills 下所有子包并注册，动态挂
   `POST /skills/{name}/extract` 与 batch-extract 路由

本模块同时保留拆分前的 app.main 命名空间：
- 测试接缝（必须经 app.main 解析）：_rate_limit_whitelist / _AUTH_FREE_PATHS
  为本模块定义；build_result / _publish_order / _parse_document_to_order
  为 re-export——中间件与路由在**调用时**从 app.main 命名空间读取这些符号，
  测试以 setattr(main_module, ...) 注入替身依然生效（见各调用点注释）；
- 兼容 re-export（同一对象）：app / _LIMITERS / _inflight_semaphore /
  _resolve_client_ip / _summarize_import_response / _probe_* / access_log。
"""

from __future__ import annotations

from app import access_log
from app.api.app_factory import create_app
from app.api.executor import _inflight_semaphore, _parse_document_to_order, _publish_order
from app.api.health import (
    _probe_dependencies,
    _probe_llm,
    _probe_mineru,
    _probe_order_config,
)
from app.api.middleware.access_log import _summarize_import_response
from app.api.middleware.rate_limit import _LIMITERS, _resolve_client_ip
from app.config import settings
from app.logging_conf import setup_logging
from app.orders.bill import build_result

__all__ = [
    "_AUTH_FREE_PATHS",
    "_LIMITERS",
    "_inflight_semaphore",
    "_parse_document_to_order",
    "_probe_dependencies",
    "_probe_llm",
    "_probe_mineru",
    "_probe_order_config",
    "_publish_order",
    "_rate_limit_whitelist",
    "_resolve_client_ip",
    "_summarize_import_response",
    "access_log",
    "app",
    "build_result",
    "create_app",
]

# ---- 测试接缝：鉴权豁免路径（auth 中间件调用时经 app.main 解析）----
# 鉴权豁免路径：健康检查、OpenAPI 文档与日志/账单上传页面本身（页面无数据）；
# /api/logs 日志数据接口含 PII，不在豁免内，必须鉴权才能查看。
# /orders/bill/import 为内网免 key 使用场景豁免（与页面配套，见 /bill-import），
# 仅限可信内网部署；对外开放部署时应移出豁免并恢复页面 Key 输入。
_AUTH_FREE_PATHS = frozenset(
    {
        "/healthz",
        "/skills",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
        "/logs",
        "/third-party-logs",
        "/bill-import",
        "/bill-import-help",
        "/orders/bill/import",
    }
)

# ---- 测试接缝：免限流 IP 白名单（rate_limit 中间件调用时经 app.main 解析）----
_rate_limit_whitelist = frozenset(
    ip.strip() for ip in settings.rate_limit_whitelist.split(",") if ip.strip()
)

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
