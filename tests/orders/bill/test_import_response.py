"""成功路径外壳判定直测（2026-09-09 A 案：逻辑自路由下沉 import_response.py）。

锁定语义（迁移前后行为零变更）：
- create 全部命中注册表（skipped 占满且无失败/新建）→ 409 + 精简 data +
  审计元数据（error_code=duplicate_bill 旧口径防监控静默失效）
- skipped 与 failed 混合 → 非 409，走 204 + 具体失败原因
- create 有新建 → 200/添加成功；全部失败 → 204（msg 按 error_code 优先级
  unknown_box_type → missing_bl_no，兜底「添加失败」）
- preview（含 create 无 summary 退化路径）→ 200（整批文件级被拒 msg 给原因，
  只认 unknown_box_type / missing_bl_no 两个本地拦截码）

路由层 HTTP/审计副作用落地（status_code/state/响应对象）由 test_route.py
集成用例回归锁定，本文件只测纯判定。
"""

from __future__ import annotations

from app.orders.bill import BillOrder, BillParseResult
from app.orders.bill.import_response import bill_import_outcome

_BOX_MSG = "文件含非法箱型：40GOH，请联系客服"
_MISSING_MSG = "提单号为必填项；文件存在提单号缺失行时整批不录入，请补全提单号后重新导入"


def _summary(
    *,
    skipped: int = 0,
    failed: int = 0,
    created: int = 0,
    failed_details: list[dict] | None = None,
) -> dict:
    return {
        "total": skipped + failed + created,
        "success": created,
        "failed": failed,
        "skipped": skipped,
        "created": created,
        "success_sns": [f"EX{i}" for i in range(created)],
        "failed_details": failed_details or [],
    }


def _result(
    *,
    summary: dict | None = None,
    create_order: bool = True,
    orders: list[BillOrder] | None = None,
) -> BillParseResult:
    return BillParseResult(
        file="b.xlsx",
        total_rows=2,
        order_count=2,
        create_order=create_order,
        orders=orders or [],
        summary=summary,
        meta={},
    )


class TestDedupConflict:
    """409 语义：全部命中注册表才 409，混合场景不误报。"""

    def test_all_skipped_returns_409_with_slim_and_audit(self):
        outcome = bill_import_outcome(
            _result(summary=_summary(skipped=2)), create_order=True
        )
        assert outcome.status_code == 409
        assert outcome.code == "409"
        assert outcome.msg == "账单已全部创建过"
        assert outcome.slim is not None
        assert outcome.slim.orders == [] and outcome.slim.canonical_orders == []
        assert outcome.slim.summary == _summary(skipped=2)
        assert outcome.audit_code == "duplicate_bill"
        assert outcome.audit_detail["details"]["summary"]["skipped"] == 2

    def test_409_slim_is_copy_original_detail_kept(self):
        """model_copy 精简不动原 result 明细（重复上传场景数据仍可审计）。"""
        order = BillOrder(order_num1="BL1")
        result = _result(summary=_summary(skipped=1), orders=[order])
        outcome = bill_import_outcome(result, create_order=True)
        assert outcome.slim is not None and outcome.slim.orders == []
        assert result.orders == [order]  # 原模型明细保留

    def test_mixed_skipped_and_failed_not_409(self):
        """skipped 与 failed（箱型拒）混合且无新建 → 204 + 具体原因，非 409。"""
        summary = _summary(
            skipped=1,
            failed=2,
            failed_details=[
                {
                    "order_num": None,
                    "code": "unknown_box_type",
                    "message": _BOX_MSG,
                }
            ],
        )
        outcome = bill_import_outcome(_result(summary=summary), create_order=True)
        assert outcome.status_code == 200
        assert outcome.code == "204"
        assert outcome.msg == _BOX_MSG
        assert outcome.slim is None and outcome.audit_code is None

    def test_created_any_success_200(self):
        outcome = bill_import_outcome(
            _result(summary=_summary(skipped=1, created=1)), create_order=True
        )
        assert outcome.code == "200" and outcome.msg == "添加成功"
        assert outcome.status_code == 200


class TestAllFailedMsg:
    """204 文案：error_code 优先级 unknown_box_type → missing_bl_no → 兜底。"""

    def test_box_msg_beats_missing_bl_no_by_priority(self):
        """mixed 失败原因（缺提单号在前 + 箱型在后）→ msg 取箱型（优先级更高）。"""
        summary = _summary(
            failed=2,
            failed_details=[
                {
                    "order_num": None,
                    "code": "missing_bl_no",
                    "message": _MISSING_MSG,
                },
                {
                    "order_num": None,
                    "code": "unknown_box_type",
                    "message": _BOX_MSG,
                },
            ],
        )
        outcome = bill_import_outcome(_result(summary=summary), create_order=True)
        assert outcome.code == "204"
        assert outcome.msg == _BOX_MSG

    def test_missing_bl_no_msg_fallback(self):
        summary = _summary(
            failed=2,
            failed_details=[
                {
                    "order_num": None,
                    "code": "missing_bl_no",
                    "message": _MISSING_MSG,
                }
            ],
        )
        outcome = bill_import_outcome(_result(summary=summary), create_order=True)
        assert outcome.code == "204"
        assert outcome.msg == _MISSING_MSG

    def test_unrecognized_error_code_falls_back_generic(self):
        """非本地拦截码（下游失败 order_upstream_error）→ 兜底「添加失败」。"""
        summary = _summary(
            failed=1,
            failed_details=[
                {
                    "order_num": "BL1",
                    "code": "order_upstream_error",
                    "message": "AddWork rejected the order: 添加失败",
                }
            ],
        )
        outcome = bill_import_outcome(_result(summary=summary), create_order=True)
        assert outcome.code == "204"
        assert outcome.msg == "添加失败"


class TestPreviewShell:
    """preview 口径（含 create 无 summary 退化路径）：200 + 文件级拦截原因。"""

    def test_preview_ok_default_msg(self):
        outcome = bill_import_outcome(
            _result(summary=None, create_order=False), create_order=False
        )
        assert outcome.code == "200" and outcome.msg == "请求成功"

    def test_create_without_summary_falls_back_preview(self):
        """create 模式但 summary 为空（原路由 else 分支语义）→ preview 口径。"""
        outcome = bill_import_outcome(
            _result(summary=None, create_order=True), create_order=True
        )
        assert outcome.code == "200" and outcome.msg == "请求成功"

    def test_preview_blocked_box_msg_specific(self):
        order = BillOrder(
            order_num1="BL1",
            create_result={
                "success": False,
                "error": {"code": "unknown_box_type", "message": _BOX_MSG},
            },
        )
        outcome = bill_import_outcome(
            _result(summary=None, create_order=False, orders=[order]),
            create_order=False,
        )
        assert outcome.code == "200" and outcome.msg == _BOX_MSG

    def test_preview_unrecognized_error_code_keeps_generic_msg(self):
        """非本地拦截码（下游失败）preview 不给具体文案（保持「请求成功」）。"""
        order = BillOrder(
            order_num1="BL1",
            create_result={
                "success": False,
                "error": {"code": "order_upstream_error", "message": "boom"},
            },
        )
        outcome = bill_import_outcome(
            _result(summary=None, create_order=False, orders=[order]),
            create_order=False,
        )
        assert outcome.code == "200" and outcome.msg == "请求成功"
