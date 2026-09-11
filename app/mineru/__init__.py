"""外部适配器层：文档解析（MinerU）客户端（分层定位见 docs/架构说明.md §0）。"""
from .schema import PageQuality, ParsedPage, ParseIssue, ParseResult

__all__ = ["PageQuality", "ParsedPage", "ParseIssue", "ParseResult"]
