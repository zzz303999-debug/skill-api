"""结算区间与年份提示识别（P4-S1 自 parser.py 拆出）。

抬头区「结算」文本 → 两个 YYYY-MM-DD（extract_bill_period，识别不到返回空
BillPeriod 不报错）；year_source 配置 → 年份提示（resolve_year_hint，日期归一
date_flex 用）。纯 view 只读零副作用，与行解析无耦合。
"""

from __future__ import annotations

from ..schema import BillPeriod
from .normalizers import _SETTLEMENT_RE, parse_year_hint
from .sheet_view import _SheetView


def _format_period_date(parts: tuple[str, str, str]) -> str | None:
    """YYYY-M-D 三元组 → YYYY-MM-DD；月份/日期越界返回 None。"""
    year, month, day = (int(p) for p in parts)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def extract_bill_period(view: _SheetView, header_row: int) -> BillPeriod:
    """从表头行之前的抬头区识别结算区间；识别不到返回空 BillPeriod（不报错）。"""
    for row in range(1, header_row):
        for col in range(1, view.ncols + 1):
            raw = view.merged_cell(row, col)
            if raw is None:
                continue
            text = str(raw)
            if "结算" not in text:
                continue
            # 结算日期形如「2018-01-01-2018-12-31」；YYYY-MM-DD 正则单源
            # （normalizers._SETTLEMENT_RE，收敛重复定义）
            matches = _SETTLEMENT_RE.findall(text)
            if len(matches) < 2:
                continue
            start = _format_period_date(matches[0])
            end = _format_period_date(matches[1])
            if start and end:
                return BillPeriod(start=start, end=end)
    return BillPeriod()


def _settlement_text(view: _SheetView, header_row: int) -> str | None:
    """表头上方抬头区含「结算」的文本（year_hint 解析用）。"""
    parts: list[str] = []
    for row in range(1, header_row):
        for col in range(1, view.ncols + 1):
            raw = view.merged_cell(row, col)
            if raw is None:
                continue
            text = str(raw)
            if "结算" in text:
                parts.append(text)
    return "\n".join(parts) if parts else None


def resolve_year_hint(
    template: dict, filename: str, view: _SheetView, header_row: int
) -> tuple[int, int] | None:
    """按配置 year_source 顺序尝试年份提示：filename → 结算日期行。"""
    sources = template.get("year_source") or []
    settlement_text = _settlement_text(view, header_row)
    for source in sources:
        if source == "settlement_row":
            hint = parse_year_hint("", settlement_text)
        else:
            hint = parse_year_hint(filename or "", None)
        if hint is not None:
            return hint
    return None
