"""托书格式转换：bytes → markdown 文本。

复用 `converter.py`（原 tuoshu-extractor/scripts/to_text.py），
通过临时文件 + stdout 重定向的方式包装成函数调用，避免重写 400+ 行。
"""

from __future__ import annotations

import io
import re
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from app.config import settings
from app.document_parsers import mineru
from app.document_parsers.models import PageQuality, ParsedPage, ParseIssue, ParseResult
from app.errors import BadRequestError, ConvertError
from app.logging_conf import get_logger

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


def detect_image_mime(file_bytes: bytes, filename: str) -> tuple[str, str]:
    """Validate image content by magic bytes instead of trusting the suffix."""
    detected: tuple[str, str, tuple[str, ...]] | None = None
    if file_bytes.startswith(b"\xff\xd8\xff"):
        detected = _IMAGE_SIGNATURES[0]
    elif file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = _IMAGE_SIGNATURES[1]
    elif file_bytes.startswith((b"GIF87a", b"GIF89a")):
        detected = _IMAGE_SIGNATURES[2]
    elif file_bytes.startswith(b"BM"):
        detected = _IMAGE_SIGNATURES[3]
    elif file_bytes.startswith((b"II*\x00", b"MM\x00*")):
        detected = _IMAGE_SIGNATURES[4]
    elif len(file_bytes) >= 12 and file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        detected = _IMAGE_SIGNATURES[5]

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
            details={
                "file": Path(filename).name,
                "extension": suffix,
                "detected_format": image_format,
            },
        )
    return mime, image_format


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


def _render_pdf_page(file_bytes: bytes, page_index: int, *, scale: float) -> bytes:
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(file_bytes)
        page = pdf[page_index]
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
            page.close()
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

    pages: list[ParsedPage] = []
    try:
        for page_index, page in enumerate(pdf.pages):
            quality = probe_pdf_page(page)
            if quality.text_layer_qualified:
                pages.append(
                    ParsedPage(
                        page_number=page_index + 1,
                        parser="pdfplumber",
                        markdown=_format_pdf_page(page, page_index + 1),
                        quality=quality,
                    )
                )
            else:
                pages.append(_parse_ocr_page(file_bytes, filename, page_index, quality))
    finally:
        pdf.close()
    if not pages:
        raise ConvertError("PDF has no pages")
    return ParseResult(input_format="pdf", pages=pages)


def convert_image_to_parse_result(file_bytes: bytes, filename: str) -> ParseResult:
    mime_type, image_format = detect_image_mime(file_bytes, filename)
    if not settings.mineru_enabled:
        return ParseResult(
            input_format=image_format,
            pages=[ParsedPage(page_number=1, parser="vision", vision_image=file_bytes)],
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
                log.warning(
                    "mineru_parse_fallback",
                    extra={"file": filename, "error_type": exc.__class__.__name__},
                )
        except mineru.MinerUContractError as exc:
            raise ConvertError(f"MinerU contract check failed: {exc}") from exc

    handler = _DISPATCH[ext]
    with tempfile.TemporaryDirectory(prefix="tuoshu-convert-") as tmp_dir:
        tmp_path = Path(tmp_dir) / (Path(filename).name or f"document{ext}")
        tmp_path.write_bytes(file_bytes)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                handler(str(tmp_path))
            converted = buf.getvalue()
            parser = "pdfplumber" if ext == ".pdf" else "local"
            result = ParseResult(
                input_format=ext.lstrip("."),
                pages=[ParsedPage(page_number=1, parser=parser, markdown=converted)],
            )
            return ConversionText(
                converted,
                parser=parser,
                parser_fallback=ext == ".pdf" and settings.mineru_enabled,
                parse_result=result,
            )
        except Exception as e:
            raise ConvertError(f"convert failed: {e.__class__.__name__}: {e}") from e


def render_pdf_pages(
    file_bytes: bytes,
    *,
    max_pages: int,
    scale: float,
) -> list[bytes]:
    """把扫描 PDF 的前几页渲染成 PNG，供 vision 模型识别。"""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(file_bytes)
        images: list[bytes] = []
        try:
            for page_index in range(min(len(pdf), max_pages)):
                page = pdf[page_index]
                bitmap = None
                try:
                    bitmap = page.render(scale=scale)
                    image = bitmap.to_pil()
                    buf = io.BytesIO()
                    image.save(buf, format="PNG")
                    images.append(buf.getvalue())
                finally:
                    if bitmap is not None:
                        bitmap.close()
                    page.close()
        finally:
            pdf.close()
    except Exception as e:
        raise ConvertError(f"scan PDF render failed: {e.__class__.__name__}: {e}") from e

    if not images:
        raise ConvertError("scan PDF has no renderable pages")
    return images
