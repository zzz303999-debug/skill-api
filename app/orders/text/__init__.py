"""自由文本下单子域：extractor（解析）→ mapper（校验/组装）→ client（下单）。

对应路由 POST /orders；域门面 app/orders/__init__.py 统一 re-export 本子域符号。
"""

from .client import OrderUpstreamError, publish_create_order_async
from .extractor import extract_order_text, parse_source_fields
from .mapper import OrderNotReadyError, build_order_data
from .schema import (
    CreateOrderFromTextRequest,
    CreateOrderFromTextResponse,
    OrderApiResponse,
    OrderTextExtraction,
)

__all__ = [
    "CreateOrderFromTextRequest",
    "CreateOrderFromTextResponse",
    "OrderApiResponse",
    "OrderNotReadyError",
    "OrderTextExtraction",
    "OrderUpstreamError",
    "build_order_data",
    "extract_order_text",
    "parse_source_fields",
    "publish_create_order_async",
]
