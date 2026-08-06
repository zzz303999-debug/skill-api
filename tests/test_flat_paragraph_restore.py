"""表格展平段落流的确定性恢复规则测试。

textutil 把老式 .doc 表格展平为 `_pN_` 段落流（标签带与值带分离），
这些用例覆盖跨空段标签-值关联与提单号格式特征恢复，以及误判过滤。
"""

from __future__ import annotations

from app.skills.tuoshu.postprocessor import finalize_extraction


def _finalize(source_text: str, **fields) -> dict:
    base = {
        "shipper_company": "某托运人公司",
        "factory": {"name": "某门点"},
    }
    base.update(fields)
    return finalize_extraction(base, source_text=source_text)


def _issue_codes(result: dict) -> set[str]:
    return {issue["code"] for issue in result["review_issues"]}


def test_separated_fm_value_restores_customer():
    result = _finalize(
        "_p20_ TO:\n"
        "_p22_ FM:\n"
        "_p23: (empty)_\n"
        "_p24: (empty)_\n"
        "_p26_ 海丰国际\n"
        "_p28_ CNCC074827\n"
        "托运人：某托运人公司"
    )

    assert result["customer"] == "海丰国际"
    assert "missing_customer" not in _issue_codes(result)


def test_separated_fm_stops_at_label_style_line():
    result = _finalize("_p1_ FM:\n托运人：某托运人公司\n请准时到达")

    assert result["customer"] is None
    assert "missing_customer" in _issue_codes(result)


def test_mbl_restored_by_unique_format_token_with_blocking_issue():
    result = _finalize(
        "_p1_ 托运人：某托运人公司\n"
        "_p2_ FM:\n"
        "_p3_ 海丰国际\n"
        "_p4_ CNCC074827\n"
        "_p5_ 船名：CMA CGM CHRISTOPHE COLOMB"
    )

    assert result["mbl_no"] == "CNCC074827"
    assert result["customer"] == "海丰国际"
    issue = next(
        issue for issue in result["review_issues"] if issue["code"] == "mbl_no_by_format"
    )
    assert issue["field"] == "mbl_no"
    assert issue["blocking"] is True
    assert issue["source_values"] == ["CNCC074827"]
    assert result["ready_for_order"] is False


def test_mbl_label_across_gap_restores():
    result = _finalize(
        "_p1_ 托运人：某托运人公司\n"
        "_p2_ 提 单 号:\n"
        "_p3: (empty)_\n"
        "_p4: (empty)_\n"
        "_p5_ CNCC074827"
    )

    assert result["mbl_no"] == "CNCC074827"
    assert "mbl_no_by_format" in _issue_codes(result)


def test_mbl_restored_from_prefixed_label_value_line():
    result = _finalize("_p5_ 提单号 : MSKU8890207\n托运人：某托运人公司")

    assert result["mbl_no"] == "MSKU8890207"
    assert "mbl_no_by_format" in _issue_codes(result)


def test_mbl_restore_rejects_quantity_phone_and_ship_name():
    result = _finalize(
        "_p1_ 托运人：某托运人公司\n"
        "_p2_ 件数：1100CTNS\n"
        "_p3_ 电话：13611802064\n"
        "_p4_ 船名：CMA CGM CHRISTOPHE COLOMB"
    )

    assert result["mbl_no"] is None
    assert "missing_mbl_no" in _issue_codes(result)
    assert "mbl_no_by_format" not in _issue_codes(result)


def test_mbl_restore_skips_when_multiple_tokens_exist():
    result = _finalize(
        "_p1_ 托运人：某托运人公司\n"
        "_p2_ 箱号：MSCU1234567\n"
        "_p3_ 提单号值：CNCC074827"
    )

    assert result["mbl_no"] is None
    assert "missing_mbl_no" in _issue_codes(result)


def test_mbl_restore_skips_child_bill_token():
    result = _finalize("子提单号：HBL240001\n托运人：某托运人公司")

    assert result["mbl_no"] is None
    assert result["hbl_no"] == "HBL240001"
    assert "missing_mbl_no" in _issue_codes(result)
    assert "mbl_no_by_format" not in _issue_codes(result)


def test_mbl_restore_keeps_existing_valid_value():
    result = _finalize(
        "提单号：MBL240001\n托运人：某托运人公司", mbl_no="MBL240001"
    )

    assert result["mbl_no"] == "MBL240001"
    assert "mbl_no_by_format" not in _issue_codes(result)
    assert "invalid_mbl_no" not in _issue_codes(result)


def test_mbl_restored_from_customs_no_label():
    """关单号即提单号：原文有关单号标签时 mbl_no 取关单号（运编号不作为提单号）。"""
    result = _finalize(
        "运编号：MAX202011824A/B/C\n关单号：CNWW036474\n托运人：某托运人公司"
    )

    assert result["mbl_no"] == "CNWW036474"
    assert "mbl_no_by_format" in _issue_codes(result)


def test_mbl_bill_label_takes_priority_over_customs_no():
    """提单号标签优先：提单号与关单号并存且值不同时，取提单号标签值。"""
    result = _finalize(
        "提单号：KMTCSHAP950393\n关单号：CNWW036474\n托运人：某托运人公司"
    )

    assert result["mbl_no"] == "KMTCSHAP950393"
    assert "mbl_no_by_format" in _issue_codes(result)


def test_mbl_not_restored_from_customs_declaration_no():
    """报关单号不是提单号：原文只有报关单号时不恢复 mbl_no。"""
    result = _finalize("报关单号：CUS12345678\n托运人：某托运人公司")

    assert result["mbl_no"] is None
    assert "missing_mbl_no" in _issue_codes(result)
    assert "mbl_no_by_format" not in _issue_codes(result)
