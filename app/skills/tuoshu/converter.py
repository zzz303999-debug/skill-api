#!/usr/bin/env python3
"""
tuoshu-extractor: 任意格式 → Markdown（无损转换）

职责单一：xlsx/xls/docx/doc/pdf → stdout markdown。
不做任何语义抽取、不做归一化、不做 OCR。
图片和扫描 PDF 输出 SCAN_OR_IMAGE_HINT: <path>，由上层转为 vision 输入。

用法:
    python3 scripts/to_text.py <file>
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------- 通用工具 ----------

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".webp"}


def emit_scan_hint(path: str, reason: str = "") -> None:
    """输出 OCR 提示，供上层 skill 调用 OCR。"""
    suffix = f"  # {reason}" if reason else ""
    print(f"SCAN_OR_IMAGE_HINT: {path}{suffix}")


def _cell_col_letter(idx_1based: int) -> str:
    """1 → A, 27 → AA。"""
    s = ""
    n = idx_1based
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _clean_cell(value) -> str:
    """把任意单元格值转成字符串，保留换行为 <br>。"""
    if value is None:
        return ""
    s = str(value)
    # 统一换行 → <br>，保结构
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\n", "<br>")
    # 去尾部空白，不去内部空白
    return s.rstrip()


# ---------- 分支：xlsx ----------

def convert_xlsx(path: str) -> None:
    import io

    import openpyxl

    # 用 BytesIO 绕过 openpyxl 对扩展名的检查（有些 .xls 实际是 xlsx）
    with open(path, "rb") as fh:
        data = fh.read()
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    print(f"# {Path(path).name}")
    print("_format: xlsx_")
    print()

    for ws in wb.worksheets:
        print(f"## Sheet: {ws.title}")
        print()

        # 合并单元格值下渗：把每个 merged range 的左上值写到范围内所有格子
        merged_map: dict[tuple[int, int], object] = {}
        for rng in ws.merged_cells.ranges:
            min_col, min_row, max_col, max_row = rng.min_col, rng.min_row, rng.max_col, rng.max_row
            anchor_val = ws.cell(row=min_row, column=min_col).value
            for r in range(min_row, max_row + 1):
                for c in range(min_col, max_col + 1):
                    merged_map[(r, c)] = anchor_val

        max_row = ws.max_row or 0
        max_col = ws.max_column or 0

        if max_row == 0 or max_col == 0:
            print("_empty sheet_")
            print()
            continue

        # 修剪尾部纯空的行/列（结构中间的空行空列保留，只去外围的填充空白）
        def _cell_val(r: int, c: int, merged=merged_map, worksheet=ws):
            return merged.get((r, c), worksheet.cell(row=r, column=c).value)

        while max_row > 0 and all(
            _cell_val(max_row, c) in (None, "") for c in range(1, max_col + 1)
        ):
            max_row -= 1
        while max_col > 0 and all(
            _cell_val(r, max_col) in (None, "") for r in range(1, max_row + 1)
        ):
            max_col -= 1

        if max_row == 0 or max_col == 0:
            print("_empty sheet_")
            print()
            continue

        # 输出 markdown 表格（保留结构中的空行/空列）
        header = "| " + " | ".join(["_row/col_"] + [_cell_col_letter(c) for c in range(1, max_col + 1)]) + " |"
        sep = "| " + " | ".join(["---"] * (max_col + 1)) + " |"
        print(header)
        print(sep)

        for r in range(1, max_row + 1):
            row_cells: list[str] = [str(r)]
            for c in range(1, max_col + 1):
                if (r, c) in merged_map:
                    v = merged_map[(r, c)]
                else:
                    v = ws.cell(row=r, column=c).value
                row_cells.append(_clean_cell(v).replace("|", "\\|"))
            print("| " + " | ".join(row_cells) + " |")
        print()


# ---------- 分支：xls ----------

def convert_xls(path: str) -> None:
    import xlrd
    from xlrd import xldate

    # 有些 .xls 文件实际是 xlsx（ZIP 头 PK\x03\x04），转发处理
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        magic = b""
    if magic == b"PK\x03\x04":
        convert_xlsx(path)
        return

    book = xlrd.open_workbook(path, formatting_info=False)
    datemode = book.datemode
    print(f"# {Path(path).name}")
    print("_format: xls_")
    print()

    for sheet in book.sheets():
        print(f"## Sheet: {sheet.name}")
        print()

        nrows, ncols = sheet.nrows, sheet.ncols
        if nrows == 0 or ncols == 0:
            print("_empty sheet_")
            print()
            continue

        # xlrd 的 merged_cells 是 [(rlo, rhi, clo, chi), ...]，含前不含后
        merged_map: dict[tuple[int, int], object] = {}
        for rlo, rhi, clo, chi in sheet.merged_cells:
            anchor = sheet.cell_value(rlo, clo)
            for r in range(rlo, rhi):
                for c in range(clo, chi):
                    merged_map[(r, c)] = anchor

        def _xls_val(r: int, c: int, merged=merged_map, current_sheet=sheet):
            if (r, c) in merged:
                return merged[(r, c)]
            ctype = current_sheet.cell_type(r, c)
            v = current_sheet.cell_value(r, c)
            # XL_CELL_DATE = 3：还原为 ISO datetime 字符串
            if ctype == xlrd.XL_CELL_DATE:
                try:
                    dt = xldate.xldate_as_datetime(v, datemode)
                    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                        return dt.strftime("%Y-%m-%d")
                    return dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    return v
            return v

        # 修剪尾部纯空的行/列
        while nrows > 0 and all(_xls_val(nrows - 1, c) in (None, "") for c in range(ncols)):
            nrows -= 1
        while ncols > 0 and all(_xls_val(r, ncols - 1) in (None, "") for r in range(nrows)):
            ncols -= 1

        if nrows == 0 or ncols == 0:
            print("_empty sheet_")
            print()
            continue

        header = "| " + " | ".join(["_row/col_"] + [_cell_col_letter(c + 1) for c in range(ncols)]) + " |"
        sep = "| " + " | ".join(["---"] * (ncols + 1)) + " |"
        print(header)
        print(sep)

        for r in range(nrows):
            row_cells = [str(r + 1)]
            for c in range(ncols):
                v = _xls_val(r, c)
                # xlrd 的 float 需要处理成整数字符串（若是整数值）
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
                row_cells.append(_clean_cell(v).replace("|", "\\|"))
            print("| " + " | ".join(row_cells) + " |")
        print()


# ---------- 分支：docx ----------

def convert_docx(path: str) -> None:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(path)
    print(f"# {Path(path).name}")
    print("_format: docx_")
    print()

    # 按 body 顺序遍历段落和表格
    body = doc.element.body
    p_iter = iter(doc.paragraphs)
    t_iter = iter(doc.tables)

    p_idx = 0
    t_idx = 0
    for child in body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            para = next(p_iter, None)
            if para is None:
                continue
            text = para.text or ""
            text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip()
            p_idx += 1
            if text.strip() == "":
                # 保留空行为一个明确的占位，便于人工/LLM 定位段落断点
                print(f"_p{p_idx}: (empty)_")
            elif "\n" in text:
                # 段内软换行（shift-enter）：每个子行独立成 markdown 行，便于 LLM 逐字段抽取
                lines = [ln for ln in text.split("\n")]
                for sub_i, sub in enumerate(lines, start=1):
                    if sub.strip() == "":
                        print(f"_p{p_idx}.{sub_i}: (empty)_")
                    else:
                        print(f"_p{p_idx}.{sub_i}_ {sub.rstrip()}")
            else:
                print(f"_p{p_idx}_ {text}")
            print()
        elif tag == qn("w:tbl"):
            tbl = next(t_iter, None)
            if tbl is None:
                continue
            t_idx += 1
            print(f"### Table {t_idx}")
            print()
            rows = tbl.rows
            if not rows:
                print("_empty table_")
                print()
                continue
            ncols = max(len(r.cells) for r in rows)
            header = "| " + " | ".join(["_row/col_"] + [_cell_col_letter(c + 1) for c in range(ncols)]) + " |"
            sep = "| " + " | ".join(["---"] * (ncols + 1)) + " |"
            print(header)
            print(sep)
            for r_idx, row in enumerate(rows, start=1):
                row_cells = [str(r_idx)]
                cells = row.cells
                for c_idx in range(ncols):
                    if c_idx < len(cells):
                        # python-docx 对合并单元格会重复返回同一 cell.text，保留
                        v = cells[c_idx].text or ""
                    else:
                        v = ""
                    row_cells.append(_clean_cell(v).replace("|", "\\|"))
                print("| " + " | ".join(row_cells) + " |")
            print()


# ---------- 分支：doc（需 libreoffice） ----------

def _find_soffice() -> str | None:
    """跨平台定位 LibreOffice 可执行文件。"""
    # PATH 命中优先（Linux/Docker 常见）
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    # 常见平台安装位置兜底
    candidates = [
        # macOS
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        # Linux 常见路径
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
        "/usr/lib/libreoffice/program/soffice",
        "/opt/libreoffice/program/soffice",
        "/snap/bin/libreoffice",
        # Windows
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def convert_doc(path: str) -> None:
    soffice = _find_soffice()
    if not soffice:
        emit_scan_hint(
            path,
            reason="doc 需 libreoffice 转换（soffice 未找到）；请安装 LibreOffice 或改用 OCR",
        )
        return

    with tempfile.TemporaryDirectory() as tmpd:
        try:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "docx", "--outdir", tmpd, path],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            emit_scan_hint(path, reason=f"libreoffice 转换失败: {e}")
            return

        stem = Path(path).stem
        docx_path = Path(tmpd) / f"{stem}.docx"
        if not docx_path.exists():
            # libreoffice 有时会改名，回退用目录里唯一的 docx
            candidates = list(Path(tmpd).glob("*.docx"))
            if not candidates:
                emit_scan_hint(path, reason="libreoffice 未产出 docx")
                return
            docx_path = candidates[0]

        convert_docx(str(docx_path))


# ---------- 分支：pdf ----------

def convert_pdf(path: str) -> None:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        total_text_len = 0
        for page in pdf.pages:
            t = page.extract_text() or ""
            total_text_len += len(t.strip())

        # 全文极短 → 扫描件，交 OCR
        if total_text_len < 40:
            emit_scan_hint(path, reason=f"pdf 文本层过短 ({total_text_len} 字符)，判定为扫描件")
            return

        print(f"# {Path(path).name}")
        print(f"_format: pdf_ pages: {len(pdf.pages)}")
        print()

        for page_idx, page in enumerate(pdf.pages, start=1):
            print(f"## Page {page_idx}")
            print()

            # 文本层
            text = page.extract_text() or ""
            text = text.strip()
            if text:
                print("### Text")
                print()
                for line_i, line in enumerate(text.split("\n"), start=1):
                    print(f"_l{line_i}_ {line.rstrip()}")
                print()
            else:
                print("_no text layer on this page_")
                print()

            # 表格层（若有）
            tables = []
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []

            for t_idx, table in enumerate(tables, start=1):
                print(f"### Table {page_idx}.{t_idx}")
                print()
                if not table:
                    print("_empty table_")
                    print()
                    continue
                ncols = max(len(r) for r in table)
                header = "| " + " | ".join(["_row/col_"] + [_cell_col_letter(c + 1) for c in range(ncols)]) + " |"
                sep = "| " + " | ".join(["---"] * (ncols + 1)) + " |"
                print(header)
                print(sep)
                for r_idx, row in enumerate(table, start=1):
                    cells = [str(r_idx)]
                    for c_idx in range(ncols):
                        v = row[c_idx] if c_idx < len(row) else ""
                        cells.append(_clean_cell(v).replace("|", "\\|"))
                    print("| " + " | ".join(cells) + " |")
                print()


# ---------- 分支：图片 ----------

def convert_image(path: str) -> None:
    emit_scan_hint(path, reason="image → OCR")


# ---------- 入口 ----------

DISPATCH = {
    ".xlsx": convert_xlsx,
    ".xlsm": convert_xlsx,
    ".xls": convert_xls,
    ".docx": convert_docx,
    ".doc": convert_doc,
    ".pdf": convert_pdf,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: to_text.py <file>", file=sys.stderr)
        return 2

    path = argv[1]
    if not os.path.exists(path):
        print(f"file not found: {path}", file=sys.stderr)
        return 2

    ext = Path(path).suffix.lower()

    if ext in IMAGE_EXTS:
        convert_image(path)
        return 0

    handler = DISPATCH.get(ext)
    if handler is None:
        print(f"unsupported extension: {ext}", file=sys.stderr)
        return 2

    try:
        handler(path)
    except Exception as e:
        print(f"convert error: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
