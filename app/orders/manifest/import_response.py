"""舱单导入成功路径的响应外壳判定（自 api/response_shell 下沉）。

与账单导入（app/orders/bill/import_response.py）同构的域侧判定：统一响应
外壳 {code, msg, data} 的成功路径业务语义归 orders 域（纯函数零 IO、零 HTTP
依赖），路由只负责响应对象组装——判定留在 api 表现层会破坏薄路由原则。

舱单口径（无账单式重复上传 409 语义：v1.9 起重复上传照常 200 重新提交，
重复风险调用方自负）：
- create 有新建（含部分失败）→ "200/添加成功"；全部失败 → "204"（msg 取
  首个非空 message，兜底「添加失败」）
- preview（或 create 无 summary 退化路径）→ "200"（整批文件级被拒时 msg
  给首个错误文案，code 保持 "200"——未产生下游动作，语义不冲突）
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import ManifestParseResult


@dataclass
class ManifestImportOutcome:
    """成功路径外壳判定结果（舱单恒 HTTP 200，无 409/精简/审计分支）。"""

    code: str = "200"
    msg: str = "请求成功"


def manifest_import_outcome(
    result: ManifestParseResult, create_order: bool
) -> ManifestImportOutcome:
    """成功路径 code/msg 判定（逻辑 1:1 迁移自 api/response_shell，行为零变更）。

    判定输入仅 result 与模式开关：summary（create 填充）的 created 计数定
    200/204，orders 的 create_result 错误定失败文案（无错误码优先级，
    取首个非空错误文案的舱单口径）。
    """
    summary = result.summary
    if create_order and summary:
        if summary.get("created", 0) > 0:
            # 有新建（含部分失败，明细在 data.summary）→ 成功口径
            return ManifestImportOutcome(code="200", msg="添加成功")
        failed_msg = next(
            (
                d.get("message")
                for d in (summary.get("failed_details") or [])
                if d.get("message")
            ),
            None,
        )
        return ManifestImportOutcome(code="204", msg=failed_msg or "添加失败")
    # preview 口径（含 create 无 summary 的退化路径）：整批文件级被拒时
    # msg 给首个错误文案，其余成功文案
    msg = next(
        (
            o.create_result.error.message
            for o in result.orders
            if o.create_result and o.create_result.error
        ),
        None,
    )
    return ManifestImportOutcome(code="200", msg=msg or "请求成功")
