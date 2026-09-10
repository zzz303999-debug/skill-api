"""账单导入成功路径的响应外壳判定（自路由下沉，路由薄壳化）。

统一响应外壳 {code, msg, data} 的业务语义归 orders 域（纯函数零 IO、零 HTTP
依赖；路由只按 outcome 落地 status_code / request.state 审计 / 响应对象）：

- create 全部命中成功单注册表（skipped 占满且无失败/新建）→ HTTP 409 +
  精简 data（重复上传提示场景，前端只消费 data.summary.success_sns；全量
  明细使 820 单响应 ≈1.5MB，灌 DOM/日志落盘成本大，模型副本裁掉明细）；
  审计 error_code 保持旧口径 duplicate_bill（改为 "409" 会让按旧码匹配的
  监控/告警静默失效；外壳 code 仍 "409" 不冲突）；
- create 有新建（含部分失败）→ "200/添加成功"；全部失败 → "204"（msg 按
  error_code 优先级：unknown_box_type → missing_bl_no，均兜底「添加失败」）；
- preview（或 create 无 summary）→ "200"（整批文件级被拒时 msg 给具体原因，
  code 保持 "200"——未产生下游动作，语义不冲突）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schema import BillParseResult

# 本地拦截文案扫描优先级（文件级连坐口径，提单号缺失对齐箱型）
_MSG_CODES: tuple[str, ...] = ("unknown_box_type", "missing_bl_no")


@dataclass
class BillImportOutcome:
    """成功路径外壳判定结果：路由按字段落地 HTTP/审计副作用（缺省即
    preview 成功口径 200/请求成功，slim 与审计仅 409 分支携带）。"""

    status_code: int = 200
    code: str = "200"
    msg: str = "请求成功"
    # 409 分支：裁剪明细后的响应模型（orders/canonical_orders 置空），
    # 非 409 时为空——路由用原始 result 作为 data
    slim: BillParseResult | None = None
    # 409 分支审计透传元数据（中间件 access_log 消费 request.state）
    audit_code: str | None = None
    audit_detail: dict[str, Any] | None = field(default=None)


def bill_import_outcome(
    result: BillParseResult, create_order: bool
) -> BillImportOutcome:
    """成功路径 code/msg/status/slim/审计判定（逻辑 1:1 迁移自原路由，行为零变更）。

    判定输入仅 result 与模式开关：summary（create 填充）计数定 409/200/204，
    orders/canonical_orders 的 create_result 错误定 preview 文案。
    """
    summary = result.summary
    if create_order and summary:
        # 409 语义（v2.2 修正）：仅当全部单均为重复上传才 409——skipped 与
        # failed 混合时本次存在被拒单，不应报「账单已全部创建过」，走 204 +
        # 具体失败原因（如箱型不符文案）
        if (
            summary.get("skipped", 0) > 0
            and summary.get("failed", 0) == 0
            and summary.get("created", 0) == 0
        ):
            return BillImportOutcome(
                status_code=409,
                code="409",
                msg="账单已全部创建过",
                slim=result.model_copy(update={"orders": [], "canonical_orders": []}),
                audit_code="duplicate_bill",
                audit_detail={
                    "code": "duplicate_bill",
                    "message": "账单已全部创建过，本次未录入",
                    "description": "账单已全部创建过，本次未录入",
                    "details": {"summary": summary},
                },
            )
        if summary.get("created", 0) > 0:
            # 有新建（含部分失败，明细在 data.summary）→ 成功口径
            return BillImportOutcome(code="200", msg="添加成功")
        failed_msg = next(
            (
                d.get("message")
                for code in _MSG_CODES
                for d in (summary.get("failed_details") or [])
                if d.get("code") == code
            ),
            None,
        )
        return BillImportOutcome(code="204", msg=failed_msg or "添加失败")
    # preview 口径（含 create 无 summary 的退化路径）：整批文件级被拒时
    # msg 给具体原因，其余成功文案
    msg = next(
        (
            o.create_result.error.message
            for o in (*result.orders, *result.canonical_orders)
            if o.create_result
            and o.create_result.error
            and o.create_result.error.code in _MSG_CODES
        ),
        None,
    )
    return BillImportOutcome(code="200", msg=msg or "请求成功")
