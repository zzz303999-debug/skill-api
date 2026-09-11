"""tuoshu 归一子包：tables 簇（P4-1 自 normalizer.py 拆分，行为零变更）。"""

from __future__ import annotations

import html
import re


def pipe_row_cells(line: str) -> list[str]:
    """Split a pipe table row into cells, undoing escaped pipes."""
    return [cell.strip().replace("\\|", "|") for cell in line.strip("|").split("|")]


def parse_html_table_rows(source: str) -> list[list[str]]:
    """Parse every HTML table row (tr/td/th) into stripped cell lists."""
    decoded = html.unescape(source)
    return [
        [
            re.sub(r"<[^>]+>", "", cell).strip()
            for cell in re.findall(
                r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>",
                raw_row,
                re.IGNORECASE | re.DOTALL,
            )
        ]
        for raw_row in re.findall(
            r"<tr\b[^>]*>(.*?)</tr>", decoded, re.IGNORECASE | re.DOTALL
        )
    ]


def is_separator_row(row: list[str]) -> bool:
    """A Markdown table separator such as ``| --- | :--: |``."""
    return not row or all(
        re.fullmatch(r":?-{3,}:?", part.replace(" ", "")) for part in row
    )
