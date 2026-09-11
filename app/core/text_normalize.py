"""跨域共享的文本归一工具（P3 起，原 tuoshu/normalizer.py 抽取）。

单一实现源：订单（text）/文档（document）域与托书技能（tuoshu）共用；
tuoshu.normalizer 从本模块 re-export 保持其内部引用稳定。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

STANDARD_CONTAINER_LENGTHS = ("20", "25", "40")


STANDARD_CONTAINER_SUFFIXES = (
    "GP",
    "HC",
    "HQ",
    "RF",
    "OT",
    "TK",
    "FR",
    "PL",
    "OH",
    "RH",
    "UT",
    "VH",
)


_DATE_SEPARATOR_TRANSLATION = str.maketrans({"／": "/", "．": ".", "－": "-", "：": ":"})


_DATE_PATTERN = re.compile(
    r"(\d{4}|\d{2})(?!\d)\s*(?:年\s*|[./-]\s*)"
    r"(\d{1,2})\s*(?:月\s*|[./-]\s*)"
    r"(\d{1,2})(?!\d)(?:\s*日)?"
    r"(?:[T\s]+(\d{1,2})\s*(?::|时)\s*(\d{1,2})"
    r"(?:\s*(?::|分)\s*(\d{1,2})(?:\.\d+)?\s*秒?)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?",
    re.IGNORECASE,
)


def parse_date_or_none(value: str, *, allow_time: bool = False) -> str | None:
    """Extract the first unambiguous year-first date inside text, else None.

    Accepts ``2026-07-15``, Chinese ``2026年7月15日``, 2-digit years
    (``21.5.28``), and an optional time component. Invalid calendar dates
    and unrelated text return None so callers can fall back to review issues.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().translate(_DATE_SEPARATOR_TRANSLATION)
    # OCR commonly joins the time directly to the Chinese day suffix, for
    # example ``2026年7月15日0:00``.
    text = re.sub(r"日(?=\d{1,2}\s*(?::|时))", "日 ", text)
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    year_text = match.group(1)
    year = int(year_text)
    if len(year_text) == 2:
        # strptime ``%y`` semantics: 00-68 -> 2000-2068, 69-99 -> 1969-1999.
        year += 2000 if year < 69 else 1900
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    second = int(match.group(6) or 0)
    try:
        parsed = datetime(year, int(match.group(2)), int(match.group(3)), hour, minute, second)
    except ValueError:
        return None
    normalized_date = parsed.date().isoformat()
    if not allow_time or match.group(4) is None:
        return normalized_date
    return f"{normalized_date}T{hour:02d}:{minute:02d}:{second:02d}"


def normalize_date_value(value: Any, *, allow_time: bool) -> Any:
    """Normalize common year-first date spellings emitted by OCR/LLMs.

    Only unambiguous ``year-month-day`` values are changed. Invalid calendar
    dates and unrelated text are deliberately left untouched so schema
    validation can still reject them instead of silently inventing a value.
    与 ``parse_date_or_none`` 一致，支持 2 位年份（``21.5.28`` → 2021-05-28）。
    """
    if not isinstance(value, str):
        return value

    text = value.strip().translate(
        str.maketrans({"／": "/", "．": ".", "－": "-", "：": ":"})
    )
    # OCR commonly joins the time directly to the Chinese day suffix, for
    # example ``2026年7月15日0:00``.
    text = re.sub(r"日(?=\d{1,2}\s*(?::|时))", "日 ", text)
    match = re.fullmatch(
        r"(\d{2}|\d{4})(?!\d)\s*(?:年\s*|[./-]\s*)"
        r"(\d{1,2})\s*(?:月\s*|[./-]\s*)"
        r"(\d{1,2})(?!\d)\s*日?"
        r"(?:[T\s]+(\d{1,2})\s*(?::|时)\s*(\d{1,2})"
        r"(?:\s*(?::|分)\s*(\d{1,2})(?:\.\d+)?\s*秒?)?)?"
        r"(?:Z|[+-]\d{2}:?\d{2})?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return value

    year_text = match.group(1)
    year = int(year_text)
    if len(year_text) == 2:
        # strptime ``%y`` semantics: 00-68 -> 2000-2068, 69-99 -> 1969-1999.
        year += 2000 if year < 69 else 1900
    month, day = (int(match.group(index)) for index in range(2, 4))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    second = int(match.group(6) or 0)
    try:
        parsed = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return value

    normalized_date = parsed.date().isoformat()
    if not allow_time or match.group(4) is None:
        return normalized_date
    return f"{normalized_date}T{hour:02d}:{minute:02d}:{second:02d}"
