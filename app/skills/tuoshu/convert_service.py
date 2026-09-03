"""托书格式转换：bytes → markdown 文本。

复用 `converter.py`（原 tuoshu-extractor/scripts/to_text.py）的转换函数，
每个 handler 直接返回 (markdown, report)，无 stdout 重定向，可安全并发。
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import BadRequestError, ConvertError
from app.core.logging_conf import get_logger
from app.mineru import client as mineru
from app.mineru.schema import PageQuality, ParsedPage, ParseIssue, ParseResult

from . import converter as _conv

log = get_logger(__name__)

# 复用底层字典
_DISPATCH = _conv.DISPATCH
_IMAGE_EXTS = _conv.IMAGE_EXTS

SUPPORTED_EXTS = list(_DISPATCH.keys()) + list(_IMAGE_EXTS)


class ConversionText(str):
    """携带解析器信息、但仍与普通字符串完全兼容的 Markdown。"""

    parser: str
    parser_fallback: bool
    parse_result: ParseResult | None

    def __new__(
        cls,
        value: str,
        *,
        parser: str,
        parser_fallback: bool = False,
        parse_result: ParseResult | None = None,
    ):
        instance = super().__new__(cls, value)
        instance.parser = parser
        instance.parser_fallback = parser_fallback
        instance.parse_result = parse_result
        return instance


def is_image(ext: str) -> bool:
    return ext.lower() in _IMAGE_EXTS


_IMAGE_SIGNATURES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("image/jpeg", "jpeg", (".jpg", ".jpeg")),
    ("image/png", "png", (".png",)),
    ("image/gif", "gif", (".gif",)),
    ("image/bmp", "bmp", (".bmp",)),
    ("image/tiff", "tiff", (".tif", ".tiff")),
    ("image/webp", "webp", (".webp",)),
)


def _detect_image_signature(
    file_bytes: bytes,
) -> tuple[str, str, tuple[str, ...]] | None:
    """按 magic bytes 识别栅格图片格式，返回 (mime, format, accepted_exts)。"""
    if file_bytes.startswith(b"\xff\xd8\xff"):
        return _IMAGE_SIGNATURES[0]
    if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return _IMAGE_SIGNATURES[1]
    if file_bytes.startswith((b"GIF87a", b"GIF89a")):
        return _IMAGE_SIGNATURES[2]
    if file_bytes.startswith(b"BM"):
        return _IMAGE_SIGNATURES[3]
    if file_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return _IMAGE_SIGNATURES[4]
    if len(file_bytes) >= 12 and file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        return _IMAGE_SIGNATURES[5]
    return None


def detect_image_mime(file_bytes: bytes, filename: str) -> tuple[str, str]:
    """Validate image content by magic bytes instead of trusting the suffix."""
    detected = _detect_image_signature(file_bytes)
    if detected is None:
        raise BadRequestError(
            "uploaded image content is not a supported raster format",
            code="unsupported_image_content",
            details={"file": Path(filename).name},
        )

    mime, image_format, accepted_exts = detected
    suffix = Path(filename).suffix.lower()
    if suffix not in accepted_exts:
        raise BadRequestError(
            "image extension does not match file content",
            code="file_format_mismatch",
            # 显式 description（2026-08-27 审查修正）：errors.py 全局码文案已改
            # 为竞品账单专属（「请上传竞品应收对账单格式」），托书域必须覆盖
            description="图片格式与文件扩展名不匹配，请上传正确格式的图片",
            details={
                "file": Path(filename).name,
                "extension": suffix,
                "detected_format": image_format,
            },
        )
    return mime, image_format


def detect_document_format(file_bytes: bytes) -> str:
    """Detect the container format without trusting the uploaded filename."""
    if not file_bytes:
        return "empty"
    detected_image = _detect_image_signature(file_bytes)
    if detected_image is not None:
        return detected_image[1]

    prefix = file_bytes[:4096].lstrip()
    if b"%PDF-" in file_bytes[:1024]:
        return "pdf"
    if file_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if prefix.startswith(b"{\\rtf"):
        return "rtf"
    prefix_lower = prefix.lower()
    if prefix_lower.startswith((b"<!doctype html", b"<html")):
        return "html"
    if prefix.startswith((b"<?xml", b"<pkg:package", b"<w:wordDocument")) and (
        b"schemas.microsoft.com/office/word/2003/wordml" in prefix
        or b"schemas.microsoft.com/office/2006/xmlPackage" in prefix
    ):
        return "word_xml"

    if file_bytes.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return "zip"
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "zip"
    return "unknown"


def validate_document_content(file_bytes: bytes, filename: str) -> str:
    """Reject empty uploads and clear extension/content mismatches."""
    ext = Path(filename).suffix.lower()
    if is_image(ext):
        _mime, image_format = detect_image_mime(file_bytes, filename)
        return image_format

    detected = detect_document_format(file_bytes)
    if detected == "empty":
        raise BadRequestError(
            "uploaded file is empty",
            code="empty_file",
            details={"file": Path(filename).name},
        )

    accepted_formats = {
        ".pdf": {"pdf"},
        ".docx": {"docx"},
        ".xlsx": {"xlsx"},
        ".xlsm": {"xlsx"},
        # Legacy Office formats share the OLE container. Some real-world .xls
        # files are OOXML workbooks with the old suffix.
        ".xls": {"ole", "xlsx", "unknown"},
        ".doc": {"ole", "word_xml", "rtf", "html", "unknown"},
    }
    accepted = accepted_formats.get(ext)
    if accepted is not None and detected not in accepted:
        raise BadRequestError(
            "file extension does not match file content",
            code="file_format_mismatch",
            # 显式 description（2026-08-27 审查修正）：覆盖全局码的账单专属文案
            description="附件格式不匹配，请上传正确的文档格式（PDF/Word/图片）",
            details={
                "file": Path(filename).name,
                "extension": ext,
                "detected_format": detected,
            },
        )
    return detected


_QUALITY_LABELS = ("船名", "提单号", "主单号", "件数")


def _bbox_intersection_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    width = max(0.0, min(float(left["x1"]), float(right["x1"])) - max(float(left["x0"]), float(right["x0"])))
    height = max(0.0, min(float(left["bottom"]), float(right["bottom"])) - max(float(left["top"]), float(right["top"])))
    intersection = width * height
    left_area = max(1.0, (float(left["x1"]) - float(left["x0"])) * (float(left["bottom"]) - float(left["top"])))
    right_area = max(1.0, (float(right["x1"]) - float(right["x0"])) * (float(right["bottom"]) - float(right["top"])))
    return intersection / min(left_area, right_area)


def probe_pdf_page(page: Any) -> PageQuality:
    """Measure one page before selecting pdfplumber or OCR."""
    text = page.extract_text() or ""
    compact = re.sub(r"\s+", "", text)
    char_count = len(compact)
    garbled_count = compact.count("\ufffd") + len(re.findall(r"\(cid:\d+\)", compact))
    garbled_ratio = garbled_count / max(char_count, 1)
    try:
        tables = page.extract_tables() or []
    except Exception:
        tables = []
    labels = tuple(label for label in _QUALITY_LABELS if label in compact)

    try:
        words = page.extract_words() or []
    except Exception:
        words = []
    label_words = [
        word
        for word in words
        if any(label in re.sub(r"\s+", "", str(word.get("text", ""))) for label in labels)
    ]
    bbox_overlap = any(
        _bbox_intersection_ratio(left, right) > 0.5
        for index, left in enumerate(label_words)
        for right in label_words[index + 1 :]
    )

    reasons: list[str] = []
    if char_count < settings.parser_text_min_chars:
        reasons.append("text_below_minimum")
    if garbled_ratio > settings.parser_garbled_ratio_threshold:
        reasons.append("garbled_text_ratio")
    if bbox_overlap:
        reasons.append("critical_label_bbox_overlap")
    return PageQuality(
        char_count=char_count,
        garbled_ratio=garbled_ratio,
        table_count=len(tables),
        key_labels=labels,
        bbox_overlap=bbox_overlap,
        text_layer_qualified=not reasons,
        reasons=tuple(reasons),
    )


def _format_pdf_page(page: Any, page_number: int) -> str:
    lines = [f"## Page {page_number}", "", "### Text", ""]
    text = (page.extract_text() or "").strip()
    if text:
        lines.extend(f"_l{index}_ {line.rstrip()}" for index, line in enumerate(text.split("\n"), 1))
    else:
        lines.append("_no text layer on this page_")
    lines.append("")
    try:
        tables = page.extract_tables() or []
    except Exception:
        tables = []
    for table_index, table in enumerate(tables, 1):
        lines.extend((f"### Table {page_number}.{table_index}", ""))
        if not table:
            lines.extend(("_empty table_", ""))
            continue
        column_count = max(len(row) for row in table)
        columns = [_conv._cell_col_letter(index + 1) for index in range(column_count)]
        lines.append("| " + " | ".join(["_row/col_", *columns]) + " |")
        lines.append("| " + " | ".join(["---"] * (column_count + 1)) + " |")
        for row_index, row in enumerate(table, 1):
            cells = [str(row_index)]
            for column_index in range(column_count):
                value = row[column_index] if column_index < len(row) else ""
                cells.append(_conv._clean_cell(value).replace("|", "\\|"))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines).strip()


def _render_page_to_png(page: Any, scale: float) -> bytes:
    """Render one pdfium page to PNG bytes, closing only its bitmap."""
    bitmap = None
    try:
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        if bitmap is not None:
            bitmap.close()


def _render_pdf_page(file_bytes: bytes, page_index: int, *, scale: float) -> bytes:
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(file_bytes)
        try:
            page = pdf[page_index]
            try:
                return _render_page_to_png(page, scale)
            finally:
                page.close()
        finally:
            pdf.close()
    except Exception as exc:
        raise ConvertError(
            f"PDF page render failed: page={page_index + 1}, {exc.__class__.__name__}: {exc}"
        ) from exc


def _parse_ocr_page(file_bytes: bytes, filename: str, page_index: int, quality: PageQuality) -> ParsedPage:
    image = _render_pdf_page(
        file_bytes,
        page_index,
        scale=settings.vision_pdf_render_scale,
    )
    page_number = page_index + 1
    page_filename = f"{Path(filename).stem}-page-{page_number}.png"
    try:
        parsed = mineru.parse_document(image, page_filename, mime_type="image/png")
    except mineru.MinerUContractError as exc:
        raise ConvertError(f"MinerU contract check failed: {exc}") from exc
    except Exception as exc:
        if not settings.mineru_fallback_enabled:
            raise ConvertError(
                f"MinerU convert failed on page {page_number}: {exc.__class__.__name__}"
            ) from exc
        return ParsedPage(
            page_number=page_number,
            parser="vision",
            quality=quality,
            confidence="low",
            vision_image=image,
            vision_mime="image/png",
            issues=[
                ParseIssue(
                    code="mineru_failed",
                    message="MinerU 页面解析失败，已转 vision，必须人工复核",
                    page=page_number,
                    source_values=(exc.__class__.__name__,),
                )
            ],
        )

    if parsed.low_confidence:
        return ParsedPage(
            page_number=page_number,
            parser="vision",
            markdown=parsed.markdown,
            quality=quality,
            confidence="low",
            vision_image=image,
            vision_mime="image/png",
            issues=[
                ParseIssue(
                    code="mineru_low_confidence",
                    message="MinerU 页面结果低置信，已转 vision，必须人工复核",
                    page=page_number,
                    source_values=parsed.low_confidence_reasons,
                )
            ],
        )
    return ParsedPage(
        page_number=page_number,
        parser="mineru",
        markdown=parsed.markdown,
        quality=quality,
    )


def _convert_pdf_with_page_routing(file_bytes: bytes, filename: str) -> ParseResult:
    import pdfplumber

    try:
        pdf = pdfplumber.open(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ValueError("pdf quality probe failed") from exc

    # Phase 1: probe every page and format qualified text-layer pages inside
    # the pdfplumber context (page objects are only valid while the PDF is
    # open).  OCR candidates are collected for parallel processing below.
    ocr_jobs: list[tuple[int, PageQuality]] = []
    formatted_pages: dict[int, ParsedPage] = {}
    total_pages = 0
    try:
        for page_index, page in enumerate(pdf.pages):
            total_pages += 1
            quality = probe_pdf_page(page)
            if quality.text_layer_qualified:
                formatted_pages[page_index] = ParsedPage(
                    page_number=page_index + 1,
                    parser="pdfplumber",
                    markdown=_format_pdf_page(page, page_index + 1),
                    quality=quality,
                )
            else:
                ocr_jobs.append((page_index, quality))
    finally:
        pdf.close()

    if not total_pages:
        raise ConvertError("PDF has no pages")

    # Phase 2: run MinerU OCR on unqualified pages in parallel.  Each call
    # opens its own httpx client and pypdfium2 document, so they are safe to
    # run concurrently.  Bounded by mineru_ocr_concurrency to avoid
    # overwhelming the local MinerU service.
    ocr_results: dict[int, ParsedPage] = {}
    if ocr_jobs:
        if len(ocr_jobs) == 1:
            page_index, quality = ocr_jobs[0]
            ocr_results[page_index] = _parse_ocr_page(
                file_bytes, filename, page_index, quality
            )
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            max_workers = min(len(ocr_jobs), settings.mineru_ocr_concurrency)
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="mineru-ocr",
            ) as pool:
                future_to_index = {
                    pool.submit(
                        _parse_ocr_page,
                        file_bytes,
                        filename,
                        page_index,
                        quality,
                    ): page_index
                    for page_index, quality in ocr_jobs
                }
                for future in as_completed(future_to_index):
                    page_index = future_to_index[future]
                    ocr_results[page_index] = future.result()

    # Merge pages back in document order.
    pages = [
        ocr_results.get(i) or formatted_pages.get(i)
        for i in range(total_pages)
    ]
    if any(p is None for p in pages):
        raise ConvertError("PDF page routing left a gap")

    # Enforce vision page limit after the fact.  Pre-checking would be
    # stricter, but counting actual vision_image pages keeps the original
    # semantics: a page that MinerU handles with high confidence does not
    # carry a vision_image and should not count against the budget.
    vision_page_count = sum(page.vision_image is not None for page in pages)
    if vision_page_count > settings.vision_max_pdf_pages:
        raise ConvertError(
            "PDF has too many pages for complete vision conversion",
            code="pdf_page_limit_exceeded",
            details={
                "vision_page_count": vision_page_count,
                "max_pages": settings.vision_max_pdf_pages,
            },
        )
    return ParseResult(input_format="pdf", pages=pages)


def convert_image_to_parse_result(file_bytes: bytes, filename: str) -> ParseResult:
    mime_type, image_format = detect_image_mime(file_bytes, filename)
    if not settings.mineru_enabled:
        return ParseResult(
            input_format=image_format,
            pages=[
                ParsedPage(
                    page_number=1,
                    parser="vision",
                    confidence="low",
                    vision_image=file_bytes,
                    vision_mime=mime_type,
                    issues=[
                        ParseIssue(
                            code="vision_only_unverified",
                            message="图片仅由 vision 识别，没有独立 OCR 文本可交叉核验，必须人工复核",
                            page=1,
                        )
                    ],
                )
            ],
        )
    try:
        parsed = mineru.parse_document(file_bytes, filename, mime_type=mime_type)
    except mineru.MinerUContractError as exc:
        raise ConvertError(f"MinerU contract check failed: {exc}") from exc
    except Exception as exc:
        if not settings.mineru_fallback_enabled:
            raise ConvertError(f"MinerU convert failed: {exc.__class__.__name__}") from exc
        return ParseResult(
            input_format=image_format,
            pages=[
                ParsedPage(
                    page_number=1,
                    parser="vision",
                    confidence="low",
                    vision_image=file_bytes,
                    vision_mime=mime_type,
                    issues=[
                        ParseIssue(
                            code="mineru_failed",
                            message="MinerU 图片解析失败，已转 vision，必须人工复核",
                            page=1,
                            source_values=(exc.__class__.__name__,),
                        )
                    ],
                )
            ],
        )

    if parsed.low_confidence:
        return ParseResult(
            input_format=image_format,
            pages=[
                ParsedPage(
                    page_number=1,
                    parser="vision",
                    markdown=parsed.markdown,
                    confidence="low",
                    vision_image=file_bytes,
                    vision_mime=mime_type,
                    issues=[
                        ParseIssue(
                            code="mineru_low_confidence",
                            message="MinerU 图片结果低置信，已转 vision，必须人工复核",
                            page=1,
                            source_values=parsed.low_confidence_reasons,
                        )
                    ],
                )
            ],
        )
    return ParseResult(
        input_format=image_format,
        pages=[
            ParsedPage(
                page_number=1,
                parser="mineru",
                markdown=parsed.markdown,
                # MinerU text alone cannot prove that free-form OCR text is
                # visible in the uploaded image.  Keep the original image as
                # independent evidence even when MinerU reports high confidence.
                vision_image=file_bytes,
                vision_mime=mime_type,
            )
        ],
    )


def convert_to_markdown(file_bytes: bytes, filename: str) -> str:
    """把上传文件字节转成 markdown。

    - Word/Excel：使用本地确定性转换器
    - PDF：启用 MinerU 时按页探测质量并路由，否则保持本地兼容路径
    - 图片：由 ``convert_image_to_parse_result`` 原图直传 MinerU
    """
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise BadRequestError(
            f"unsupported extension: {ext}",
            details={"supported": SUPPORTED_EXTS},
        )

    validate_document_content(file_bytes, filename)

    # 图片：跳过转换，交由 vision 通道
    if is_image(ext):
        return f"SCAN_OR_IMAGE_HINT: {filename}  # image → vision"

    # PDF 启用 MinerU 后先做页级质量探测，只把不合格页送去 OCR。
    if ext == ".pdf" and settings.mineru_enabled:
        try:
            result = _convert_pdf_with_page_routing(file_bytes, filename)
            log.info(
                "pdf_page_routing_succeeded",
                extra={"file": filename, "page_routes": result.meta()["page_routes"]},
            )
            return ConversionText(
                result.markdown,
                parser=result.parser,
                parser_fallback=result.parser_fallback,
                parse_result=result,
            )
        except ValueError:
            # Compatibility for nonstandard PDFs that pdfplumber cannot open.
            # MinerU still receives the original bytes and remains version-pinned.
            try:
                markdown = mineru.parse_pdf(file_bytes, filename)
                result = ParseResult(
                    input_format="pdf",
                    pages=[ParsedPage(page_number=1, parser="mineru", markdown=markdown)],
                )
                return ConversionText(markdown, parser="mineru", parse_result=result)
            except mineru.MinerUContractError as exc:
                raise ConvertError(f"MinerU contract check failed: {exc}") from exc
            except Exception as exc:
                if not settings.mineru_fallback_enabled:
                    raise ConvertError(
                        f"MinerU convert failed: {exc.__class__.__name__}"
                    ) from exc
                # 本地 pdfplumber 正是因打不开该文件才进入此分支，再走本地
                # 解析必然失败；改为给出可行动的错误提示（转图片走 vision）。
                raise ConvertError(
                    "PDF cannot be opened by the local parser and MinerU parse failed; "
                    "convert the PDF to images and retry so it can go through vision",
                    code="pdf_parse_failed",
                    details={
                        "file": Path(filename).name,
                        "mineru_error": f"{exc.__class__.__name__}: {exc}",
                    },
                ) from exc
        except mineru.MinerUContractError as exc:
            raise ConvertError(f"MinerU contract check failed: {exc}") from exc

    handler = _DISPATCH[ext]
    with _conv._make_temp_dir(prefix="tuoshu-convert-") as tmp_dir:
        # 保持原名落盘：markdown 首行标题取自文件名（golden 已固定）。
        tmp_path = Path(tmp_dir) / (Path(filename).name or f"document{ext}")
        tmp_path.write_bytes(file_bytes)
        try:
            converted, conversion_report = handler(str(tmp_path))
            if not converted.strip():
                raise ConvertError(
                    "converter returned empty content",
                    code="empty_converted_content",
                    details={"file": Path(filename).name},
                )
            parser = "pdfplumber" if ext == ".pdf" else "local"
            report = conversion_report if isinstance(conversion_report, dict) else {}
            report_issues = list(report.get("issues") or [])
            missing_formula_values = list(report.get("formula_values_missing") or [])
            if missing_formula_values:
                report_issues.append(
                    {
                        "code": "formula_value_unavailable",
                        "message": "Excel 公式没有可用缓存值，必须重新计算或人工复核",
                        "source_values": missing_formula_values,
                    }
                )
            coverage = dict(report.get("coverage") or {})
            if coverage.get("complete") is False and not report_issues:
                report_issues.append(
                    {
                        "code": "conversion_coverage_incomplete",
                        "message": "附件存在未覆盖内容，必须对照原文件复核",
                        "source_values": list(coverage.get("omissions") or []),
                    }
                )
            parse_issues = [
                ParseIssue(
                    code=str(issue["code"]),
                    message=str(issue["message"]),
                    source_values=tuple(str(value) for value in issue.get("source_values") or []),
                )
                for issue in report_issues
                if isinstance(issue, dict) and issue.get("code") and issue.get("message")
            ]
            pages = [
                ParsedPage(
                    page_number=1,
                    parser=parser,
                    markdown=converted,
                    issues=parse_issues,
                )
            ]
            embedded_images = list(report.get("embedded_images") or [])
            processed_images = 0
            for embedded_image in embedded_images:
                if not isinstance(embedded_image, dict):
                    continue
                image_bytes = embedded_image.get("bytes")
                image_filename = str(embedded_image.get("filename") or "embedded-image")
                source_part = str(embedded_image.get("part") or "word/document.xml")
                if not isinstance(image_bytes, bytes):
                    continue
                try:
                    image_result = convert_image_to_parse_result(image_bytes, image_filename)
                except (BadRequestError, ConvertError) as exc:
                    pages[0].issues.append(
                        ParseIssue(
                            code="embedded_image_unprocessed",
                            message="Word 内嵌图片无法处理，必须对照原文件复核",
                            source_values=(image_filename, source_part, exc.code),
                        )
                    )
                    omissions = list(coverage.get("omissions") or [])
                    omissions.append(f"embedded_image:{image_filename}")
                    coverage["omissions"] = omissions
                    coverage["complete"] = False
                    continue

                processed_images += 1
                for embedded_page in image_result.pages:
                    embedded_markdown = embedded_page.markdown.strip()
                    if embedded_markdown:
                        embedded_markdown = (
                            f"### Embedded image OCR: {image_filename}\n\n{embedded_markdown}"
                        )
                    image_issues = list(embedded_page.issues)
                    image_issues.append(
                        ParseIssue(
                            code="embedded_image_requires_review",
                            message="Word 内嵌图片内容已送识别，需确认其属于正文而非印章、logo 或水印",
                            source_values=(image_filename, source_part),
                        )
                    )
                    pages.append(
                        ParsedPage(
                            page_number=len(pages) + 1,
                            parser=embedded_page.parser,
                            markdown=embedded_markdown,
                            quality=embedded_page.quality,
                            confidence=embedded_page.confidence,
                            vision_image=embedded_page.vision_image,
                            vision_mime=embedded_page.vision_mime,
                            source_part=f"{source_part}#{image_filename}",
                            issues=image_issues,
                        )
                    )
            if embedded_images:
                coverage["embedded_images_processed"] = processed_images
                if processed_images != len(embedded_images):
                    coverage["complete"] = False
            result = ParseResult(
                input_format=ext.lstrip("."),
                pages=pages,
                coverage=coverage,
                fallback_used=ext == ".pdf" and settings.mineru_enabled,
            )
            final_markdown = converted if len(pages) == 1 else result.markdown
            return ConversionText(
                final_markdown,
                parser=result.parser,
                parser_fallback=result.parser_fallback,
                parse_result=result,
            )
        except ConvertError:
            raise
        except Exception as e:
            raise ConvertError(f"convert failed: {e.__class__.__name__}: {e}") from e


def render_pdf_pages(
    file_bytes: bytes,
    *,
    max_pages: int,
    scale: float,
) -> list[bytes]:
    """Render every PDF page, failing instead of silently truncating the document."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(file_bytes)
        images: list[bytes] = []
        try:
            page_count = len(pdf)
            if page_count > max_pages:
                raise ConvertError(
                    "PDF has too many pages for complete vision conversion",
                    code="pdf_page_limit_exceeded",
                    details={"page_count": page_count, "max_pages": max_pages},
                )
            for page_index in range(page_count):
                page = pdf[page_index]
                try:
                    images.append(_render_page_to_png(page, scale))
                finally:
                    page.close()
        finally:
            pdf.close()
    except ConvertError:
        raise
    except Exception as e:
        raise ConvertError(f"scan PDF render failed: {e.__class__.__name__}: {e}") from e

    if not images:
        raise ConvertError("scan PDF has no renderable pages")
    return images
