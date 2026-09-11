"""bill 解析 sheet 抽象：xls/xlsx 双引擎的统一视图（P7-1 自 parser.py 拆分）。"""

from __future__ import annotations

import xlrd
from openpyxl.worksheet.worksheet import Worksheet


class _SheetView:
    """统一工作表视图：1-based 行列访问，合并单元格按需填充左上角值。

    表头/抬头区用 merged_cell（合并值填充），数据区用 cell（原生值，
    合并区非左上角视为空——真实账单尾部「合计:」行为整行合并）。
    """

    def __init__(
        self,
        nrows: int,
        ncols: int,
        merged_map: dict[tuple[int, int], object],
        get_cell,
    ):
        self.nrows = nrows
        self.ncols = ncols
        self._merged = merged_map
        self._get_cell = get_cell

    def cell(self, row: int, col: int) -> object:
        """原生单元格值（不填充合并区）。"""
        return self._get_cell(row, col)

    def merged_cell(self, row: int, col: int) -> object:
        """单元格值：合并区域内的非左上角坐标返回左上角值。"""
        if (row, col) in self._merged:
            return self._merged[(row, col)]
        return self._get_cell(row, col)


def _openpyxl_merged_map(ws: Worksheet) -> dict[tuple[int, int], object]:
    """openpyxl 合并单元格：左上角值映射到区域内所有坐标（1-based）。"""
    values: dict[tuple[int, int], object] = {}
    for merged in ws.merged_cells.ranges:
        top_left = ws.cell(merged.min_row, merged.min_col).value
        for row in range(merged.min_row, merged.max_row + 1):
            for col in range(merged.min_col, merged.max_col + 1):
                values[(row, col)] = top_left
    return values


def _xls_merged_map(sheet: xlrd.sheet.Sheet) -> dict[tuple[int, int], object]:
    """xlrd 合并单元格：merged_cells 为 (rlo, rhi, clo, chi) 0-based 排他边界。"""
    values: dict[tuple[int, int], object] = {}
    for rlo, rhi, clo, chi in sheet.merged_cells:
        top_left = _xls_cell_value(sheet, rlo, clo)
        for row in range(rlo + 1, rhi + 1):
            for col in range(clo + 1, chi + 1):
                values[(row, col)] = top_left
    return values


def _xls_cell_value(sheet: xlrd.sheet.Sheet, row0: int, col0: int) -> object:
    """xlrd 单元格 → 与 openpyxl 等价的 python 对象：number→float（保留浮点尾巴）、
    date→datetime、text→str、其余（空/错误/布尔）→ None。"""
    ctype = sheet.cell_type(row0, col0)
    value = sheet.cell_value(row0, col0)
    if ctype == xlrd.XL_CELL_NUMBER:
        return float(value)
    if ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(value, sheet.book.datemode)
    if ctype == xlrd.XL_CELL_TEXT:
        return value
    return None
