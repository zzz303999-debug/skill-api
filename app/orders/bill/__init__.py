"""竞品账单导入（Excel 对账单 → 业务订单）子包。"""

from .aggregation.aggregator import AggregationOutput, group_orders
from .aggregation.canonical_aggregator import group_canonical, to_canonical
from .parsing.opener import parse_bill, parse_xls, parse_xlsx
from .parsing.parser import ParseOutput
from .schema import (
    CANONICAL_REQUIRED,
    HEADER_ALIASES,
    HEADER_COLUMN_MAP,
    IGNORED_HEADERS,
    MISSING_BOX,
    MISSING_C_TITLE,
    MISSING_ORDER_NUM1,
    REASON_INVALID_FORMAT,
    REASON_NOT_FOUND,
    REQUIRED_FIELDS,
    REQUIRED_HEADERS,
    BillImportResponse,
    BillOrder,
    BillParseResult,
    BillPeriod,
    BillRow,
    BoxGroup,
    CanonicalOrder,
    ContainerInfo,
    FeeItem,
    FeeReconcile,
)
from .service import build_result_async

__all__ = [
    "AggregationOutput",
    "ParseOutput",
    "build_result_async",
    "group_canonical",
    "group_orders",
    "parse_bill",
    "parse_xls",
    "parse_xlsx",
    "CANONICAL_REQUIRED",
    "HEADER_ALIASES",
    "HEADER_COLUMN_MAP",
    "IGNORED_HEADERS",
    "MISSING_BOX",
    "MISSING_C_TITLE",
    "MISSING_ORDER_NUM1",
    "REASON_INVALID_FORMAT",
    "REASON_NOT_FOUND",
    "REQUIRED_FIELDS",
    "REQUIRED_HEADERS",
    "BillOrder",
    "BillParseResult",
    "BillPeriod",
    "BillRow",
    "BoxGroup",
    "CanonicalOrder",
    "ContainerInfo",
    "FeeItem",
    "FeeReconcile",
    "BillImportResponse",
    "to_canonical",
]
