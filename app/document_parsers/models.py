"""Structured parser results shared by document-routing layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ParserName = Literal["local", "pdfplumber", "mineru", "vision"]
Confidence = Literal["high", "low"]


@dataclass(frozen=True)
class ParseIssue:
    """A parser decision that must remain visible to downstream reviewers."""

    code: str
    message: str
    page: int | None = None
    source_values: tuple[str, ...] = ()
    blocking: bool = True

    def as_review_issue(self) -> dict[str, Any]:
        field = "source"
        if self.page is not None:
            field = f"source.pages[{self.page - 1}]"
        return {
            "code": self.code,
            "field": field,
            "message": self.message,
            "source_values": list(self.source_values),
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class PageQuality:
    """Evidence used to select a parser for one PDF page."""

    char_count: int
    garbled_ratio: float
    table_count: int
    key_labels: tuple[str, ...] = ()
    bbox_overlap: bool = False
    text_layer_qualified: bool = True
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "char_count": self.char_count,
            "garbled_ratio": self.garbled_ratio,
            "table_count": self.table_count,
            "key_labels": list(self.key_labels),
            "bbox_overlap": self.bbox_overlap,
            "text_layer_qualified": self.text_layer_qualified,
            "reasons": list(self.reasons),
        }


@dataclass
class ParsedPage:
    page_number: int
    parser: ParserName
    markdown: str = ""
    quality: PageQuality | None = None
    confidence: Confidence = "high"
    vision_image: bytes | None = None
    issues: list[ParseIssue] = field(default_factory=list)

    def as_route_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "page": self.page_number,
            "parser": self.parser,
            "confidence": self.confidence,
        }
        if self.quality is not None:
            result["quality"] = self.quality.as_dict()
        if self.issues:
            result["issues"] = [issue.code for issue in self.issues]
        return result


@dataclass
class ParseResult:
    """Lossless routing result; Markdown is only one projection of it."""

    input_format: str
    pages: list[ParsedPage]
    issues: list[ParseIssue] = field(default_factory=list)

    @property
    def markdown(self) -> str:
        return "\n\n".join(page.markdown.strip() for page in self.pages if page.markdown.strip())

    @property
    def parsers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(page.parser for page in self.pages))

    @property
    def parser(self) -> str:
        parsers = self.parsers
        return parsers[0] if len(parsers) == 1 else "mixed"

    @property
    def parser_fallback(self) -> bool:
        return any(page.confidence == "low" or page.issues for page in self.pages) or bool(
            self.issues
        )

    @property
    def vision_images(self) -> list[bytes]:
        return [page.vision_image for page in self.pages if page.vision_image is not None]

    def all_issues(self) -> list[ParseIssue]:
        return [*self.issues, *(issue for page in self.pages for issue in page.issues)]

    def review_issues(self) -> list[dict[str, Any]]:
        return [issue.as_review_issue() for issue in self.all_issues()]

    def meta(self) -> dict[str, Any]:
        return {
            "parser": self.parser,
            "parser_fallback": self.parser_fallback,
            "input_format": self.input_format,
            "page_routes": [page.as_route_dict() for page in self.pages],
        }
