"""托书格式转换：bytes → markdown 文本。

复用 `converter.py`（原 tuoshu-extractor/scripts/to_text.py），
通过临时文件 + stdout 重定向的方式包装成函数调用，避免重写 400+ 行。
"""

from __future__ import annotations

import io
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from app.errors import BadRequestError, ConvertError

from . import converter as _conv

# 复用底层字典
_DISPATCH = _conv.DISPATCH
_IMAGE_EXTS = _conv.IMAGE_EXTS

SUPPORTED_EXTS = list(_DISPATCH.keys()) + list(_IMAGE_EXTS)


def is_image(ext: str) -> bool:
    return ext.lower() in _IMAGE_EXTS


def convert_to_markdown(file_bytes: bytes, filename: str) -> str:
    """把上传文件字节转成 markdown。

    - 文本类（xlsx/xls/docx/doc/pdf）：直接输出 markdown
    - 图片：不转换，返回 SCAN_OR_IMAGE_HINT，由上层走 vision LLM 分支
    - 扫描版 PDF：底层脚本会输出 SCAN_OR_IMAGE_HINT，同样交由上层
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

    handler = _DISPATCH[ext]
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
        tf.write(file_bytes)
        tmp_path = tf.name
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler(tmp_path)
        return buf.getvalue()
    except Exception as e:
        raise ConvertError(f"convert failed: {e.__class__.__name__}: {e}") from e
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


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
