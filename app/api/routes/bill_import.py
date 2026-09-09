"""竞品账单导入路由：Excel 对账单解析归集，preview 预览 / create 下单。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, Response, UploadFile

from app.api.uploads import _read_upload
from app.orders.bill import BillImportResponse
from app.orders.bill.import_response import bill_import_outcome
from app.orders.bill.service import build_result_async

router = APIRouter()


@router.post(
    "/orders/bill/import",
    response_model=BillImportResponse,
    tags=["orders"],
    summary="Import a competitor bill and optionally create orders",
)
async def import_bill(
    file: Annotated[UploadFile, File()],
    request: Request,
    response: Response,
    create_order: bool = Form(default=False),
) -> BillImportResponse:
    """竞品账单导入：.xls/.xlsx/.xlsm 对账单解析归集 → preview 预览 / create 下单。

    统一外壳 {code, msg, data}：code "200"=成功/部分成功（preview 亦 200）、
    "204"=create 全部失败、"409"（HTTP 409）=create 全部重复上传；HTTP 错误
    同样套统一外壳（code 机器码、msg 中文说明）。成功路径语义与判定见 orders
    域 app/orders/bill/import_response.py。
    """
    sk = (request.headers.get("sk") or "").strip()
    request.state.file_name = file.filename or "unnamed"
    content = await _read_upload(file)
    request.state.file_size = len(content)
    result = await build_result_async(
        filename=file.filename or "unnamed",
        file_bytes=content,
        create_order=create_order,
        sk=sk,
    )
    outcome = bill_import_outcome(result, create_order)
    if outcome.status_code == 409:
        assert outcome.slim is not None
        request.state.error_code = outcome.audit_code
        request.state.error_detail = outcome.audit_detail
        response.status_code = outcome.status_code
        return BillImportResponse(code=outcome.code, msg=outcome.msg, data=outcome.slim)
    return BillImportResponse(code=outcome.code, msg=outcome.msg, data=result)
