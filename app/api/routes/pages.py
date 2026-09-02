"""静态页面路由：竞品账单上传页与操作手册页（页面无数据，鉴权豁免见 main.py）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

# app/static 静态页目录（原 main.py 以 parent/static 定位，拆分后按包层级回溯）
_STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


@router.get("/bill-import", include_in_schema=False)
def bill_import_page() -> FileResponse:
    """竞品账单上传页面（静态页，无数据；上传接口鉴权豁免见 _AUTH_FREE_PATHS）。"""
    return FileResponse(_STATIC_DIR / "bill_import.html")


@router.get("/bill-import-help", include_in_schema=False)
def bill_import_help_page() -> FileResponse:
    """竞品账单导入操作手册页面（静态页，无数据；豁免见 _AUTH_FREE_PATHS）。"""
    return FileResponse(_STATIC_DIR / "bill_import_help.html")
