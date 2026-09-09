"""账单导入响应 data 组装（2026-09-09 P3 自 service.py 下沉）。

与 import_response.py（外壳 code/msg/data 语义判定 + 409 精简 + 审计元数据）
形成「判定 / 组装」分工：本模块只产 data 内部结构——去重键段（导入去重预判
用）、create 结果统计 summary、成功单上游回显明细；路由与外壳判定不感知。

create 模式全部预判/回填完成后由编排方调用；failed_details/upstream 均不
伪造失败回显（2026-09-09 B 案/R3：前端只消费顶层 code/msg/data）。
"""

from __future__ import annotations


def order_dedup_parts(order) -> tuple[str | None, str | None]:
    """订单去重键段 (箱号, 行序号)：canonical 取结构化箱号 + row_seq，
    旧链路 BillOrder 取 container_no + row_seq；箱号缺失时键退化为行序号兜底
    （提单号|#seq），双缺再退化纯提单号（见 imported_registry.dedup_key）。"""
    container_no = None
    for container in getattr(order, "containers", None) or []:
        if getattr(container, "container_no", None):
            container_no = container.container_no
            break
    if container_no is None:
        container_no = getattr(order, "container_no", None)
    return container_no, getattr(order, "row_seq", None)


def build_failed_details(created: list) -> list[dict]:
    """失败单明细（summary.failed_details）：单号/错误码/消息。

    本地拦截不再模拟上游回显（2026-09-09 B 案：前端只消费顶层 code/msg/data，
    删除 error_upstream 透传与单级 details.upstream 模拟壳）。
    error 为模型（OrderError），键名与单级 error 对齐（R4 收敛）。"""
    return [
        {
            "order_num": getattr(o, "bl_no", None) or getattr(o, "order_num1", None),
            "code": o.create_result.error.code,
            "message": o.create_result.error.message,
        }
        for o in created
        if not o.create_result.success and o.create_result.error is not None
    ]


def build_summary(total: int, created: list) -> dict:
    """create 结果统计（六字段 + failed_details）；created = 有 create_result 的单。"""
    return {
        "total": total,
        "success": sum(1 for o in created if o.create_result.success),
        "failed": sum(1 for o in created if not o.create_result.success),
        "skipped": sum(1 for o in created if o.create_result.skipped),
        "created": sum(
            1
            for o in created
            if o.create_result.success and not o.create_result.skipped
        ),
        "success_sns": [
            o.create_result.sn for o in created if o.create_result.success
        ],
        "failed_details": build_failed_details(created),
    }


def build_upstream(created: list) -> dict | None:
    """成功单的上游回显明细：本批有新建成功单 → 200 壳 + 每单原始回显。

    无新建成功单（全部失败/全部 skipped/空）→ None——失败不伪造上游回显
    （本地拦截或上游拒绝均无成功回显可透传；前端按外壳 code/msg 分流，
    2026-09-09 R3 去伪）。"""
    if not created:
        return None
    upstream_data = [
        o.create_result.upstream
        for o in created
        if o.create_result.success and o.create_result.upstream
    ]
    created_ok = any(
        o.create_result.success and not o.create_result.skipped
        for o in created
    )
    if not created_ok:
        return None
    return {"code": "200", "msg": "添加成功", "data": upstream_data}
