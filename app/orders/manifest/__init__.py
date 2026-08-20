"""舱单导入（manifest-import）子包。

英文舱单 xlsx 解析 → TMS 舱单管理录入（addBill JSON 通道）。
契约见 docs/舱单/舱单导入需求文档.md 与 舱单导入接口文档.md（v1.0 冻结）。
"""

from .parser import ManifestParseOutput, parse_manifest
from .schema import (
    MANIFEST_REQUIRED,
    ManifestBoxGroup,
    ManifestContainer,
    ManifestOrder,
    ManifestParseResult,
)
from .service import build_manifest_result

__all__ = [
    "MANIFEST_REQUIRED",
    "ManifestBoxGroup",
    "ManifestContainer",
    "ManifestOrder",
    "ManifestParseOutput",
    "ManifestParseResult",
    "build_manifest_result",
    "parse_manifest",
]
