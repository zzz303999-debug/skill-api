"""tuoshu 归一子包：issues 簇（P4-1 自 normalizer.py 拆分，行为零变更）。"""

from __future__ import annotations

from typing import Any

from app.core.errors import ParseError
from app.core.logging_conf import get_logger

from .aliases import _REVIEW_ISSUE_ALIASES
from .common import _normalize_dict

log = get_logger(__name__)


def _raise_invalid_review_issue(issue: Any, index: int, reason: str) -> None:
    keys = sorted(str(key) for key in issue) if isinstance(issue, dict) else []
    log.error(
        "tuoshu_review_issue_parse_failed",
        extra={
            "review_issue_index": index,
            "review_issue_type": type(issue).__name__,
            "review_issue_keys": keys,
            "reason": reason,
        },
    )
    raise ParseError(
        "LLM review_issues item does not match the required structure",
        details={"index": index, "reason": reason},
    )


def _normalize_review_issue(issue: Any, index: int) -> dict[str, Any]:
    """Normalize one structured issue; malformed model output is a parse error."""
    if not isinstance(issue, dict):
        _raise_invalid_review_issue(issue, index, "item must be an object")

    normalized = _normalize_dict(issue, _REVIEW_ISSUE_ALIASES)
    if normalized.get("code") == "unstructured_review_issue":
        _raise_invalid_review_issue(
            issue,
            index,
            "unstructured_review_issue is not an allowed review issue code",
        )
    required = ("code", "field", "message")
    malformed = any(
        not isinstance(normalized.get(key), str) or not normalized[key].strip()
        for key in required
    )

    source_values = normalized.get("source_values")
    if isinstance(source_values, list):
        source_values = [str(value) for value in source_values]
    elif source_values is None:
        source_values = []
    else:
        source_values = [str(source_values)]

    if malformed:
        _raise_invalid_review_issue(
            issue,
            index,
            "code, field, and message must be non-empty strings",
        )

    blocking = normalized.get("blocking", True)
    if not isinstance(blocking, bool):
        if isinstance(blocking, str) and blocking.strip().lower() in {"false", "0", "no"}:
            blocking = False
        elif blocking in (0, 1):
            blocking = bool(blocking)
        else:
            blocking = True

    return {
        "code": normalized["code"].strip(),
        "field": normalized["field"].strip(),
        "message": normalized["message"].strip(),
        "source_values": source_values,
        "blocking": blocking,
    }


def normalize_review_issues(review_issues: Any) -> list[dict[str, Any]]:
    """Strict, shared entry point for every review issue entering the output queue."""
    if review_issues is None:
        return []
    if not isinstance(review_issues, list):
        _raise_invalid_review_issue(review_issues, 0, "review_issues must be an array")
    return [
        _normalize_review_issue(issue, index)
        for index, issue in enumerate(review_issues)
    ]
