"""竞品账单导入路由：Excel 对账单解析归集，preview 预览 / create 下单。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, Response, UploadFile

from app.api.response_shell import create_mode_shell, preview_mode_shell
from app.api.uploads import _read_upload
from app.core.errors import BadRequestError
from app.orders.bill import BillImportResponse

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
    """上传竞品应收对账单（.xls/.xlsx/.xlsm），解析归集后返回订单预览。

    create_order 缺省 false（只预览不下单）；显式传 true 时逐单创建订单
    （sk 由调用方登录 TMS 后经请求头透传，AddWork/建档共用，见
    app/orders/bill/client.py；2026-08-19 起移除服务端 GetWebKey → login 换取），
    响应附 orders[].create_result 与 summary；create 模式缺 sk → 400 bad_request。
    表头识别/格式校验/坏文件等由 parse_bill 覆盖，错误统一走全局异常处理；
    请求自动记录访问日志（文件名/大小/耗时/状态码）。

    响应统一外壳 {code, msg, data}（对齐 TMS 通道口径，2026-08-25）：
    - code="200" msg="添加成功"：preview 成功 / create 有新建（可含部分失败，明细在 data.summary）
    - code="204" msg="添加失败"：create 全部失败（无新建）
    - code="409" msg="账单已全部创建过"（HTTP 409）：create 全部命中成功单注册表
    - HTTP 错误（400/401/413/422/429/503 等）同样套统一外壳：code 为机器可读
      错误码、msg 为中文说明（可直接展示）、data 为详情（原 details）
    """
    sk = (request.headers.get("sk") or "").strip()
    if create_order and not sk:
        # TMS 全部下游接口（AddWork/建档）均以 sk 头鉴权：create 模式缺 token
        # 直接拒绝（不进入解析/下单流程）；preview 零下游调用不要求
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
    # 编排入口经 app.main 命名空间解析：测试以 setattr(main_module,
    # "build_result", ...) 注入替身（保持拆分前的 patch 点不变，2026-09）；
    # main 绑定为 build_result_async（真异步编排，fake 测试替身亦为 async）。
    # 并发模型（2026-09 用户拍板）：preview（CPU 秒级）不设请求闸；create 的
    # 下游并发由 client 层进程级共享信号量约束（见 bill/client.py）
    from app.main import build_result

    result = await build_result(
        filename=file.filename or "unnamed",
        file_bytes=content,
        create_order=create_order,
        sk=sk,
    )
    # 统一响应外壳（code/msg/data，对齐 TMS 通道口径）：映射逻辑收口
    # response_shell（bill/manifest 共享）；409 分支含路由副作用（审计 +
    # 精简响应）保留在此：
    # - create 全部命中注册表（skipped 占满且无失败/新建）→ 409（业务码 "409"）
    # - create 有新建（含部分失败）→ "200"；全部失败 → "204"（204 文案按
    #   error_code 优先级：箱型白名单 → 提单号缺失）
    # - preview → "200"（整批箱型被拒时 msg 给具体原因）
    if create_order and result.summary:
        # 409 语义（v2.2 修正）：仅当全部单均为重复上传（skipped 占满且无失败/新建）
        # 才判 409——skipped 与 failed（箱型拒绝/下游失败）混合时本次存在被拒单，
        # 不应报「账单已全部创建过」，走 204 + 具体失败原因（如箱型不符文案）
        if (
            result.summary["skipped"] > 0
            and result.summary["failed"] == 0
            and result.summary["created"] == 0
        ):
            # 审计透传（409 直接返回不经过异常处理器）：error_code 保持旧口径
            # duplicate_bill（2026-08-27 审查修正：改为 "409" 会让按旧码匹配的
            # 监控/告警静默失效）；外壳 code 仍为 "409" 不冲突。
            request.state.error_code = "duplicate_bill"
            request.state.error_detail = {
                "code": "duplicate_bill",
                "message": "账单已全部创建过，本次未录入",
                "description": "账单已全部创建过，本次未录入",
                "details": {"summary": result.summary},
            }
            response.status_code = 409
            # 409 data 精简（审查修正 2026-08-27）：全量 orders/canonical_orders
            # 明细使 820 单响应 ≈1.5MB（前端 JSON.stringify 灌 DOM、日志同步落盘）；
            # 重复上传是提示场景，前端只消费 data.summary.success_sns，明细丢弃。
            slim = result.model_copy(update={"orders": [], "canonical_orders": []})
            return BillImportResponse(code="409", msg="账单已全部创建过", data=slim)
        code, msg = create_mode_shell(
            result.summary, codes=("unknown_box_type", "missing_bl_no")
        )
    else:
        # preview：整批箱型被拒时 msg 给具体原因（仅认 unknown_box_type）
        code, msg = preview_mode_shell(
            (*result.orders, *result.canonical_orders), code="unknown_box_type"
        )
    return BillImportResponse(code=code, msg=msg, data=result)
