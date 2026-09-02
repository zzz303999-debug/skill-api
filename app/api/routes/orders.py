"""订单路由：自由文本下单与文档解析（不实际下单的预解析）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Request, UploadFile

from app.api.executor import _extract_order_text
from app.api.uploads import _read_upload
from app.config import settings
from app.errors import BadRequestError
from app.orders import (
    CreateOrderFromTextRequest,
    CreateOrderFromTextResponse,
    ParseDocumentResponse,
    build_order_data,
    parse_source_fields,
)

router = APIRouter()


@router.post(
    "/orders",
    response_model=CreateOrderFromTextResponse,
    tags=["orders"],
    summary="Extract and create an order from free text",
)
async def create_order_from_text(body: CreateOrderFromTextRequest) -> dict[str, Any]:
    text = body.content.strip()
    encoded = text.encode("utf-8")
    if len(encoded) > settings.api_max_upload_bytes:
        raise BadRequestError(
            "text is too large",
            code="text_too_large",
            details={"max_bytes": settings.api_max_upload_bytes},
        )

    extracted, meta = await _extract_order_text(text)
    order_data = build_order_data(extracted)
    # 下单经 app.main 命名空间解析：测试以 setattr(main_module, "_publish_order", ...)
    # 注入替身（保持拆分前的 patch 点不变，2026-09）
    from app.main import _publish_order

    upstream = await _publish_order(
        order_data,
        room_id=body.roomId,
        user_id=body.userId,
    )
    return {
        "roomId": body.roomId,
        "source_fields": parse_source_fields(text),
        "extracted": extracted.model_dump(),
        "order_data": order_data,
        "upstream": upstream,
        "meta": meta,
    }


@router.post(
    "/orders/parse-document",
    response_model=ParseDocumentResponse,
    tags=["orders"],
    summary="Parse an uploaded document into order fields without creating an order",
)
async def parse_order_document(
    file: Annotated[UploadFile, File()],
    request: Request,
) -> dict[str, Any]:
    """上传附件（托书/做箱通知等），转换为下单接口字段但不实际下单。

    必填字段：提单号（≥8 位纯数字或字母数字）、箱型（4 位）、客户、
    地址、做箱日期、件数、毛重、体积。字段缺失或格式不合法时不报错，
    返回 200 + needs_manual_confirmation=true + missing_fields（缺失字段）
    + missing_reasons（缺失原因：原文未找到，请人工确认 / 格式不合法）。
    order_data 始终返回（缺失项为 null，做箱日期缺失时 driver 为 [{}]），
    由调用方人工确认后补充并提交。
    """
    request.state.file_name = file.filename or "unnamed"
    content = await _read_upload(file)
    request.state.file_size = len(content)
    # 解析经 app.main 命名空间解析：测试以 setattr(main_module,
    # "_parse_document_to_order", ...) 注入替身（保持拆分前的 patch 点不变，2026-09）
    from app.main import _parse_document_to_order

    return await _parse_document_to_order(content, file.filename or "unnamed")
