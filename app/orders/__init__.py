"""orders 域门面：自由文本下单（text）/ 附件文档抽取（document）/ 账单（bill）/
舱单（manifest）四个子域；下游 HTTP 共享底座为 core.http_client（P2 起上提横切层）。

分层定位见 docs/架构说明.md §0——跨域只经门面访问，子域私有符号不出域。
"""

from .document import ParseDocumentResponse, parse_document_to_order_async
from .text import (
    CreateOrderFromTextRequest,
    CreateOrderFromTextResponse,
    OrderNotReadyError,
    build_order_data,
    extract_order_text,
    parse_source_fields,
    publish_create_order_async,
)

__all__ = [
    "CreateOrderFromTextRequest",
    "CreateOrderFromTextResponse",
    "OrderNotReadyError",
    "ParseDocumentResponse",
    "build_order_data",
    "extract_order_text",
    "parse_document_to_order_async",
    "parse_source_fields",
    "publish_create_order_async",
]
