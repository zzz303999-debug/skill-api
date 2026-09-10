"""英文舱单导入路由：托书/SI 解析，preview 预览 / create 创建 TMS 舱单（addBill）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile

from app.api.uploads import _read_upload
from app.core.errors import BadRequestError
from app.orders.manifest import ManifestImportResponse, build_manifest_result_async
from app.orders.manifest.import_response import manifest_import_outcome

router = APIRouter()


@router.post(
    "/orders/manifest/import",
    response_model=ManifestImportResponse,
    tags=["orders"],
    summary="Import an English manifest and optionally create a TMS bill",
)
async def import_manifest(
    file: Annotated[UploadFile, File()],
    request: Request,
    create_order: bool = Form(default=False),
) -> ManifestImportResponse:
    """上传英文舱单（托书/SI，.xlsx），解析后预览或创建 TMS 舱单（addBill）。

    create_order 缺省 false（只预览不触达 TMS）；显式传 true 时创建舱单
    （sk 由调用方登录 TMS 后经请求头透传，addBill 端点鉴权，见
    app/orders/manifest/client.py），响应附 orders[].create_result 与 summary。
    放开本地去重：重复上传不再拦截，照常重新提交（重复风险调用方自负）。
    家族识别/格式校验/坏文件等由 parse_manifest 覆盖，错误统一走全局异常处理；
    箱型白名单复用账单导入同一份配置（config/box_type_whitelist.yaml）：
    任一单含白名单外标准码箱型 → 全部未决单拒绝（unknown_box_type），
    preview 亦拒绝、不调下游（对齐账单导入 v1.3 语义）。

    响应统一外壳 {code, msg, data}（对齐账单录入口径）：
    - code="200" msg="请求成功"：preview 成功（整批被拒时 msg 为具体原因）
    - code="200" msg="添加成功"：create 创建成功
    - code="204" msg="添加失败"：create 全部失败（msg 优先为具体拦截原因）
    - HTTP 错误（400/401/413/422/429/503 等）同样套统一外壳：code 为机器可读
      错误码、msg 为中文说明（可直接展示）、data 为详情（原 details）
    """
    sk = (request.headers.get("sk") or "").strip()
    if create_order and not sk:
        raise BadRequestError(
            "missing sk header for create mode: login to TMS first",
            description="缺少 TMS token，请先登录 TMS 获取 token，并以 sk 请求头携带",
            details={
                "upstream": {
                    "code": "400",
                    "msg": "缺少 TMS token（sk 请求头），请先登录 TMS",
                    "data": [],
                },
            },
        )
    request.state.file_name = file.filename or "unnamed"
    content = await _read_upload(file)
    request.state.file_size = len(content)
    result = await build_manifest_result_async(
        filename=file.filename or "unnamed",
        file_bytes=content,
        create_order=create_order,
        sk=sk,
    )
    # 成功路径外壳语义（200/204/失败文案）由 orders 域判定（manifest/
    # import_response.py，与账单导入对称下沉）：路由只组装响应对象
    outcome = manifest_import_outcome(result, create_order)
    return ManifestImportResponse(code=outcome.code, msg=outcome.msg, data=result)
