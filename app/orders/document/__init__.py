"""附件文档抽取子域：rules（纯规则族）→ service（转换编排 + LLM 抽取）。

对应路由 POST /orders/parse-document；域门面 app/orders/__init__.py 统一
re-export 本子域符号（规则族细节经 document.service 的 re-export 层可见）。
"""

from .schema import ParseDocumentResponse
from .service import parse_document_to_order_async

__all__ = [
    "ParseDocumentResponse",
    "parse_document_to_order_async",
]
