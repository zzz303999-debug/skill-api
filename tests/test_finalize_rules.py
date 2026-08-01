"""finalize 确定性规则与人工复核校验测试。"""

from __future__ import annotations

import pytest

from app.skills.tuoshu.chinese_schema import to_chinese
from app.skills.tuoshu.normalizer import normalize_llm_output
from app.skills.tuoshu.postprocessor import finalize_extraction
from app.skills.tuoshu.prompt import format_to_chat_text
from app.skills.tuoshu.schema import TuoshuOutput


def test_invalid_optional_date_becomes_blocking_review_issue():
    result = finalize_extraction(
        {
            "loading_time": "2026年2月30日",
            "shipper_company": "测试托运人有限公司",
            "factory": {"name": "测试工厂"},
            "containers": [
                {"type": "20GP", "qty": 1, "packages": 1, "volume_cbm": 1}
            ],
        },
        source_text=None,
    )

    assert result["loading_time"] is None
    assert result["ready_for_order"] is False
    assert {
        "code": "invalid_date_format",
        "field": "loading_time",
        "message": "日期值格式或日历日期无效，已清空并需人工确认",
        "source_values": ["2026年2月30日"],
        "blocking": True,
    } in result["review_issues"]
    result["source"] = {"file": "invalid-date.pdf", "doc_format": "pdf"}
    TuoshuOutput.model_validate(result)


def test_table_dates_restore_loading_time_without_copying_etd():
    result = finalize_extraction(
        {
            "etd": "2026-07-19",
            "loading_time": "2026-07-19",
            "shipper_company": "上海柚理供应链管理有限公司",
            "factory": {"name": "皇裕精密技术（苏州）股份有限公司"},
            "containers": [
                {"type": "20GP", "qty": 1, "packages": 42, "volume_cbm": 70}
            ],
        },
        source_text=(
            "托运人：上海柚理供应链管理有限公司\n"
            "| 装箱日期 | 船期 | 截AMS时间 |\n"
            "| --- | --- | --- |\n"
            "| 2026年7月15日 0:00 | 2026/7/19 | |"
        ),
    )

    assert result["loading_time"] == "2026-07-15T00:00:00"
    assert result["etd"] == "2026-07-19"


def test_finalize_preserves_transit_lookup_and_builds_order_mapping():
    result = finalize_extraction(
        {
            "internal_ref": "WXHYC22010107",
            "transit_port": None,
            "shipper_company": "无锡某进出口有限公司",
            "factory": {"name": "宜兴门点"},
            "containers": [{"po_no": "D0483/0908"}],
            "remark": "下单前核对船期",
        },
        source_text=(
            "我司业务编号：WXHYC22010107\n"
            "托运人：无锡某进出口有限公司\n"
            "中转港：见设备交接单\nPO：D0483/0908"
        ),
    )

    assert result["transit_port"] == "见设备交接单"
    assert result["order_mapping"] == {
        "c_sn": "WXHYC22010107",
        "mbl_no": None,
        "hbl_no": None,
        "c_title": "无锡某进出口有限公司",
        "factory_name": "宜兴门点",
        "c_note": "PO号：D0483/0908",
    }
    issue = next(item for item in result["review_issues"] if item["code"] == "ungrounded_text")
    assert issue["source_values"] == ["下单前核对船期"]
    assert result["ready_for_order"] is False


def test_child_bill_aliases_never_fall_back_to_master_bill():
    normalized = normalize_llm_output({"子提单号": "HBL240001"})

    assert normalized["hbl_no"] == "HBL240001"
    assert normalized.get("mbl_no") is None

    result = finalize_extraction(
        {
            "mbl_no": "HBL240001",
            "hbl_no": None,
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text="子提单号：HBL240001",
    )

    assert result["mbl_no"] is None
    assert result["hbl_no"] == "HBL240001"
    assert result["order_mapping"]["mbl_no"] is None
    assert result["order_mapping"]["hbl_no"] == "HBL240001"
    assert any(issue["code"] == "hbl_misclassified_as_mbl" for issue in result["review_issues"])
    assert result["ready_for_order"] is False


@pytest.mark.parametrize("bill_no", ["12345678", "ABC12345", "HLCUSHA2111JWDA1"])
def test_valid_bill_number_formats_are_preserved(bill_no):
    result = finalize_extraction(
        {
            "mbl_no": bill_no,
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=f"提单号：{bill_no}\n托运人：某托运人公司",
    )

    assert result["mbl_no"] == bill_no
    assert not any(issue["code"] == "invalid_mbl_no" for issue in result["review_issues"])


@pytest.mark.parametrize("bill_no", ["1234567", "ABC-12345", "提单12345678"])
def test_invalid_bill_number_formats_are_cleared(bill_no):
    result = finalize_extraction(
        {
            "mbl_no": bill_no,
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=f"提单号：{bill_no}\n托运人：某托运人公司",
    )

    assert result["mbl_no"] is None
    issue = next(issue for issue in result["review_issues"] if issue["code"] == "invalid_mbl_no")
    assert issue["field"] == "mbl_no"
    assert issue["blocking"] is True


def test_explicit_master_and_child_bills_are_both_preserved():
    result = finalize_extraction(
        {
            "mbl_no": "MBL240001",
            "hbl_no": "HBL240001",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text="主提单号：MBL240001\n子提单号：HBL240001",
    )

    assert result["mbl_no"] == "MBL240001"
    assert result["hbl_no"] == "HBL240001"
    assert result["order_mapping"]["mbl_no"] == "MBL240001"
    assert result["order_mapping"]["hbl_no"] == "HBL240001"
    assert not any(
        issue["code"] == "hbl_misclassified_as_mbl" for issue in result["review_issues"]
    )


def test_finalize_blocks_non_verbatim_person_and_missing_order_fields():
    result = finalize_extraction(
        {
            "sender_contact": "范颖晰",
            "factory": {"name": None, "contact": "范颖晰"},
            "containers": [],
        },
        source_text="发货联系人：范颖晔",
    )

    issue_keys = {(issue["code"], issue["field"]) for issue in result["review_issues"]}
    assert ("sender_contact_not_from_from_field", "sender_contact") in issue_keys
    assert ("person_name_not_verbatim", "factory.contact") in issue_keys
    assert result["sender_contact"] is None
    assert ("missing_mbl_no", "mbl_no") in issue_keys
    assert ("missing_address", "factory.address") in issue_keys
    assert result["order_mapping"]["c_title"] is None
    assert result["order_mapping"]["factory_name"] is None
    assert result["ready_for_order"] is False
    assert result["raw_text_snippet"] == "发货联系人：范颖晔"


def test_incident_document_keeps_header_agent_out_of_c_title_and_restores_fields():
    source_text = """# 运输委托书
上海秉晟国际物流有限公司
| 订单编号： | BSSE2105280058 | 日期：20 | 21.5.28 |
| TO: | 上海运嘉货运代理有限公司 | 海运出口 | |
| 主单号 | 船名 | 航次 |
| 1SHA044022 | MAERSK LAVRAS | 122W |
| 件数 | 毛重 | 体积 |
| 0 | 0 | 0 |
| 地址类型 | 地址 | 时间 |
| 拆/装箱地址 | 杨会计 13921910878 江苏省扬州市宝应县望直港汽配工业园 | 2021-06-01 08:00:00 |
图章 OCR：28GSHEN S
"""
    result = finalize_extraction(
        {
            "shipper_company": "上海秉晟国际物流有限公司",
            "shipper_agent": None,
            "recipient": None,
            "doc_date": None,
            "carrier": "MSK",
            "vessel": "MAERSK LAVRAS",
            "loading_time": "2021-06-01",
            "factory": {"name": None},
            "containers": [
                {
                    "seal_no": "28GSHEN S",
                    "packages": 0,
                    "gross_weight_kg": 0,
                    "volume_cbm": 0,
                }
            ],
            "remark": "TO：上海运嘉货运代理有限公司；海运出口；封号：28GSHEN S",
        },
        source_text=source_text,
    )

    assert result["shipper_company"] is None
    assert result["shipper_agent"] == "上海秉晟国际物流有限公司"
    assert result["order_mapping"]["c_title"] is None
    assert result["recipient"] == "上海运嘉货运代理有限公司"
    assert result["doc_date"] == "2021-05-28"
    assert result["loading_time"] == "2021-06-01T08:00:00"
    assert result["containers"][0]["seal_no"] is None
    assert result["containers"][0]["packages"] is None
    assert result["containers"][0]["gross_weight_kg"] is None
    assert result["containers"][0]["volume_cbm"] is None
    assert result["remark"] == "海运出口"
    assert result["order_mapping"]["c_note"] == "海运出口"
    issue_keys = {(issue["code"], issue["field"]) for issue in result["review_issues"]}
    assert ("shipper_company_not_from_explicit_field", "shipper_company") in issue_keys
    assert ("invalid_seal_no", "containers[0].seal_no") in issue_keys
    assert ("missing_container_measurements", "containers[0]") in issue_keys
    assert ("missing_address", "factory.address") in issue_keys
    assert ("carrier_by_vessel", "carrier") in issue_keys
    assert result["ready_for_order"] is False


@pytest.mark.parametrize("label", ["致", "ATTN"])
def test_recipient_aliases_route_to_recipient_and_leave_remark_clean(label):
    result = finalize_extraction(
        {
            "recipient": None,
            "remark": f"{label}：上海硕豪物流有限公司；请准时到达",
            "shipper_company": None,
            "factory": {"name": "江西杰盛医疗制品有限公司"},
        },
        source_text=f"{label}：上海硕豪物流有限公司\n请准时到达",
    )

    assert result["recipient"] == "上海硕豪物流有限公司"
    assert result["remark"] == "请准时到达"
    assert result["order_mapping"]["c_note"] == "请准时到达"


def test_explicit_body_shipper_is_the_only_c_title_source():
    result = finalize_extraction(
        {
            "shipper_company": "抬头货代有限公司",
            "factory": {"name": "某门点"},
        },
        source_text="抬头货代有限公司\n托运人：正文出口有限公司",
    )

    assert result["shipper_company"] == "正文出口有限公司"
    assert result["order_mapping"]["c_title"] == "正文出口有限公司"
    assert any(
        issue["code"] == "shipper_company_not_from_explicit_field"
        for issue in result["review_issues"]
    )
    assert result["ready_for_order"] is False


def test_bingsheng_header_company_is_used_for_c_title():
    source_text = """# 运输委托书
上海秉晟国际物流有限公司
| 订单编号： | BSSE2108100016 | 日期： | 2021.8.10 |
| TO： | 上海运嘉货运代理有限公司 | 海运出口 | |
| 主单号 | 船名 | 航次 |
| 1KT251889 | MAERSK HAMBURG | 131W |
"""
    result = finalize_extraction(
        {
            "shipper_company": None,
            "shipper_agent": "上海秉晟国际物流有限公司",
            "factory": {"name": "太仓门点"},
        },
        source_text=source_text,
        template_hint="bingsheng_transport",
    )

    assert result["shipper_company"] == "上海秉晟国际物流有限公司"
    assert result["shipper_agent"] == "上海秉晟国际物流有限公司"
    assert result["order_mapping"]["c_title"] == "上海秉晟国际物流有限公司"
    assert not any(
        issue["code"] == "missing_shipper_company"
        for issue in result["review_issues"]
    )


def test_bolian_segway_header_company_is_used_for_c_title():
    source_text = """# 1-10号赛格威海出 托书7X40HC CNCT538619.docx
_p1_ 江苏倍联现代物流有限公司
_p2_ 常州赛格威做箱通知
_p4_ TO：俊泰
_p5_ DATE: 2022年1月5日
_p8_ 做箱时间：开港装
_p20_ 报关员电话：刘浩 18932397170
_p21_ FROM:江苏倍联 陈俐玲
"""
    result = finalize_extraction(
        {
            "shipper_company": None,
            "shipper_agent": "江苏倍联现代物流有限公司",
            "factory": {"name": "江苏倍联现代物流有限公司"},
            "remark": "做箱时间：开港装；加拼：；停靠港区：；报关员电话：刘浩 18932397170",
            "review_issues": [
                {
                    "code": "missing_shipper_company",
                    "field": "shipper_company",
                    "message": "缺少托运人公司，订单必填字段 c_title 需人工确认",
                    "source_values": ["江苏倍联现代物流有限公司", "常州赛格威做箱通知"],
                    "blocking": True,
                }
            ],
        },
        source_text=source_text,
        template_hint="bolian_segway",
    )

    assert result["shipper_company"] == "江苏倍联现代物流有限公司"
    assert result["shipper_agent"] == "江苏倍联现代物流有限公司"
    assert result["factory"]["name"] == "常州赛格威"
    assert result["order_mapping"]["c_title"] == "江苏倍联现代物流有限公司"
    assert result["order_mapping"]["factory_name"] == "常州赛格威"
    assert result["remark"] == "做箱时间：开港装；报关员电话：刘浩 18932397170"
    assert not any(
        issue["code"] == "missing_shipper_company"
        for issue in result["review_issues"]
    )


def test_zuoxiang_std_template_maps_factory_and_reads_transit_column():
    source_text = """# 1-20 东华 派车托书.xlsx
| _row/col_ | A | B | C | D | E | F | G | H | I | J | K | L |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3 | 做箱工厂 | 东华工贸 | | | | | | | | | | |
| 6 | 我司业务编号 | 客户编号 | 提单号 | 船名航次 | 目的港 | 中转港 | 港区 | 船期 | 件数 | 毛重 | 体积 | 箱型箱量 |
| 7 | ESFF22010398 | DH- | ONEYSH1FD3312900 | HMM DUBLIN / 006W | Hamburg | 见设 | 洋山 | 2022-01-26 | | | | 1*40HQ |
"""
    result = finalize_extraction(
        {
            "factory": {"name": "东华工贸"},
            "shipper_company": None,
            "transit_port": "港区",
            "containers": [
                {"type": "40HQ", "qty": 1, "packages": None, "volume_cbm": None}
            ],
            "review_issues": [
                {
                    "code": "missing_shipper_company",
                    "field": "shipper_company",
                    "message": "模型认为托运人缺失",
                    "source_values": [],
                    "blocking": True,
                },
                {
                    "code": "missing_container_measurements",
                    "field": "containers",
                    "message": "模型生成的柜量缺失提示",
                    "source_values": ["件数为空", "体积为空", "1*40HQ"],
                    "blocking": True,
                },
            ],
        },
        source_text=source_text,
        template_hint="zuoxiang_std_esff",
    )

    assert result["shipper_company"] == "东华工贸"
    assert result["order_mapping"]["c_title"] == "东华工贸"
    assert result["factory"]["name"] == "东华工贸"
    assert result["transit_port"] == "见设"
    assert not any(
        issue["code"] in {"missing_shipper_company", "conflicting_transit_port"}
        for issue in result["review_issues"]
    )
    measurement_issues = [
        issue
        for issue in result["review_issues"]
        if issue["code"] == "missing_container_measurements"
    ]
    assert len(measurement_issues) == 1
    assert measurement_issues[0]["field"] == "containers[0]"


def test_container_number_format_and_verbatim_rules_are_blocking():
    result = finalize_extraction(
        {
            "factory": {"name": "某门点"},
            "containers": [
                {"container_no": "28GSHEN S"},
                {"container_no": "SEGU1234567", "seal_no": "SEAL-99"},
            ],
        },
        source_text="箱号：SEGU1234567",
    )

    assert result["containers"][0]["container_no"] is None
    assert result["containers"][1]["container_no"] == "SEGU1234567"
    assert result["containers"][1]["seal_no"] is None
    issue_keys = {(issue["code"], issue["field"]) for issue in result["review_issues"]}
    assert ("invalid_container_no", "containers[0].container_no") in issue_keys
    assert ("seal_no_not_verbatim", "containers[1].seal_no") in issue_keys


def test_carrier_prefix_wins_and_indexed_remark_stays_with_container():
    result = finalize_extraction(
        {
            "mbl_no": "HLCUSHA12345678",
            "carrier": "HMM",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [{"remark": None}],
            "remark": "柜1备注：博特装柜；下单前核对",
        },
        source_text="提单号：HLCUSHA12345678\n柜1：博特装柜",
    )

    assert result["carrier"] == "HLC"
    assert result["containers"][0]["remark"] == "博特装柜"
    assert any(issue["code"] == "carrier_prefix_mismatch" for issue in result["review_issues"])
    assert result["ready_for_order"] is False


def test_explicit_spaced_carrier_wins_over_mbl_prefix_and_restores_booking_fields():
    result = finalize_extraction(
        {
            "carrier": "EMC",
            "mbl_no": "EGLV12345678",
            "etd": None,
            "transit_port": None,
            "doc_date": None,
            "remark": None,
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=(
            "内装箱委托书\n"
            "| 船 公 司 | 船 期 | 中转港（卸港） | 要求进港时间 |\n"
            "| --- | --- | --- | --- |\n"
            "| EMC CPS | 1月21日 | USLAX | 装好就进港 |"
        ),
        reference_year=2026,
    )

    assert result["carrier"] == "EMC CPS"
    assert result["etd"] == "2026-01-21"
    assert result["transit_port"] == "USLAX"
    assert result["remark"] == "装好就进港"
    assert not any(
        issue["code"] in {"carrier_by_mbl", "carrier_prefix_mismatch"}
        for issue in result["review_issues"]
    )


@pytest.mark.parametrize("source_text", ["船期：1月21日", None])
def test_partial_etd_without_reference_year_is_nullable_with_review_issue(source_text):
    result = finalize_extraction(
        {
            "etd": "1月21日",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=source_text,
    )

    assert result["etd"] is None
    issue = next(issue for issue in result["review_issues"] if issue["code"] == "etd_year_missing")
    assert issue["source_values"] == ["1月21日"]


def test_visual_person_name_is_blocked_when_verbatim_check_is_unavailable():
    result = finalize_extraction(
        {
            "sender_contact": "范颖晰",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=None,
    )

    assert result["raw_text_snippet"] is None
    assert any(issue["code"] == "person_name_unverified" for issue in result["review_issues"])
    assert result["ready_for_order"] is False


def test_finalize_blocks_container_data_conflict_left_in_remark():
    conflict = "主值 2150 CTNS / 5375.0 KGS / 64.518 CBM；另有记录 2149 / 5372.5 / 64.487"
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [{"packages": 2150, "remark": conflict}],
        },
        source_text=conflict,
    )

    assert any(issue["code"] == "conflicting_container_data" for issue in result["review_issues"])
    assert result["ready_for_order"] is False


def test_review_issues_are_single_source_for_missing_measurements_and_renderer():
    result = finalize_extraction(
        {
            "mbl_no": "COSU6886686750",
            "carrier": "OOCL",
            "factory": {"name": "江西杰盛医疗制品有限公司"},
            "containers": [
                {
                    "type": "40HC",
                    "qty": 1,
                    "packages": None,
                    "volume_cbm": None,
                }
            ],
        },
        source_text="提单号：COSU6886686750 承运人：OOCL 件数： 体积：",
    )

    assert {
        issue["code"] for issue in result["review_issues"]
    } == {
        "missing_container_measurements",
        "missing_customer",
        "missing_loading_time",
        "missing_address",
    }
    assert result["carrier"] == "OOCL"
    assert len(result["review_issues"]) == 4

    rendered = format_to_chat_text(
        {
            **result,
            "source": {"file": "test.pdf", "doc_format": "pdf"},
        }
    )
    assert rendered.count("复核项:") == len(result["review_issues"])
    assert "集装箱数据不完整" not in rendered
    assert "集装箱件数和体积为空，需人工确认" in rendered


def test_review_issues_are_deduplicated_by_code_before_output():
    result = finalize_extraction(
        {
            "mbl_no": "COSU6886686750",
            "carrier": "COSCO",
            "factory": {"name": "江西杰盛医疗制品有限公司"},
            "containers": [{"type": "40HC", "packages": None, "volume_cbm": None}],
            "review_issues": [
                {
                    "code": "missing_container_measurements",
                    "field": "containers[0]",
                    "message": "模型生成的缺失提示",
                    "source_values": [],
                    "blocking": True,
                },
                {
                    "code": "missing_container_measurements",
                    "field": "containers[0]",
                    "message": "另一条重复提示",
                    "source_values": ["件数为空"],
                    "blocking": True,
                },
            ],
        },
        source_text="提单号：COSU6886686750 件数： CTNS 体积： CBM",
    )

    codes = [issue["code"] for issue in result["review_issues"]]
    assert len(codes) == len(set(codes))
    assert codes.count("missing_container_measurements") == 1
    assert codes.count("carrier_by_mbl") == 1
    assert all(code != "unstructured_review_issue" for code in codes)


def test_review_issue_code_is_unique_across_multiple_affected_fields():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [
                {"type": "40HC", "packages": None, "volume_cbm": None},
                {"type": "20GP", "packages": None, "volume_cbm": None},
            ],
        },
        source_text="件数： CTNS 体积： CBM",
    )

    matching = [
        issue
        for issue in result["review_issues"]
        if issue["code"] == "missing_container_measurements"
    ]
    assert len(matching) == 1
    assert matching[0]["field"] == "containers"


def test_model_false_missing_measurement_is_dropped_when_values_present():
    result = finalize_extraction(
        {
            "mbl_no": "ABC1234567890",
            "factory": {"name": "某门点", "address": "某市某区某路1号"},
            "customer": "某客户",
            "containers": [
                {
                    "type": "40HQ",
                    "packages": 256,
                    "volume_cbm": 68.0,
                }
            ],
            "review_issues": [
                {
                    "code": "missing_container_measurements",
                    "field": "containers[0].packages/containers[0].volume_cbm",
                    "message": "Container measurements are incomplete for order review "
                    "because required package or volume value is missing or needs "
                    "confirmation.",
                    "source_values": ["件数：256", "体积：68.0000"],
                    "blocking": True,
                },
            ],
        },
        source_text=None,
    )

    assert result["containers"][0]["packages"] == 256
    assert result["containers"][0]["volume_cbm"] == 68.0
    assert not any(
        issue["code"] == "missing_container_measurements"
        for issue in result["review_issues"]
    )


def test_non_positive_measurement_hint_survives_false_missing_drop():
    result = finalize_extraction(
        {
            "mbl_no": "ABC1234567890",
            "factory": {"name": "某门点", "address": "某市某区某路1号"},
            "customer": "某客户",
            "containers": [
                {
                    "type": "40HQ",
                    "packages": 256,
                    "gross_weight_kg": 0,
                    "volume_cbm": 68.0,
                }
            ],
            "review_issues": [
                {
                    "code": "missing_container_measurements",
                    "field": "containers[0].packages/containers[0].volume_cbm",
                    "message": "Container measurements are incomplete for order review.",
                    "source_values": ["件数：256", "体积：68.0000"],
                    "blocking": True,
                },
            ],
        },
        source_text=None,
    )

    assert result["containers"][0]["gross_weight_kg"] is None
    measurement_issues = [
        issue
        for issue in result["review_issues"]
        if issue["code"] == "missing_container_measurements"
    ]
    assert len(measurement_issues) == 1
    assert measurement_issues[0]["message"] == (
        "集装箱件数、毛重或体积包含非正数，已清空并需人工确认"
    )


def test_llm_invented_issue_codes_are_filtered_out():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [
                {"type": "40HC", "packages": None, "volume_cbm": None}
            ],
            "review_issues": [
                {
                    "code": "missing_container_measurements",
                    "field": "containers[]",
                    "message": "件数和体积均缺失。",
                    "source_values": [],
                    "blocking": True,
                },
                {
                    "code": "missing_container_packages",
                    "field": "containers[].packages",
                    "message": "缺失柜件数。",
                    "source_values": [],
                    "blocking": True,
                },
                {
                    "code": "missing_gross_weight",
                    "field": "containers[].gross_weight_kg",
                    "message": "缺失柜毛重。",
                    "source_values": [],
                    "blocking": True,
                },
                {
                    "code": "missing_volume",
                    "field": "containers[].volume_cbm",
                    "message": "缺失柜体积。",
                    "source_values": [],
                    "blocking": True,
                },
            ],
        },
        source_text=None,
    )

    codes = [issue["code"] for issue in result["review_issues"]]
    assert "missing_container_packages" not in codes
    assert "missing_gross_weight" not in codes
    assert "missing_volume" not in codes
    assert codes.count("missing_container_measurements") == 1
    assert result["review_issues"][0]["field"] == "containers[0]"


def test_llm_invented_issue_codes_kept_known_codes_and_grounded_text():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [
                {"type": "40HC", "packages": 256, "volume_cbm": 68.0}
            ],
            "review_issues": [
                {
                    "code": "ungrounded_text",
                    "field": "remark",
                    "message": "自由文本未在 parser 原文中找到依据，已剔除并需人工复核",
                    "source_values": ["关单号备注：我们直接找黄牛放单，放好通知你们"],
                    "blocking": True,
                },
                {
                    "code": "invented_code_x",
                    "field": "whatever",
                    "message": "LLM 自创提示。",
                    "source_values": [],
                    "blocking": True,
                },
            ],
        },
        source_text=None,
    )

    codes = [issue["code"] for issue in result["review_issues"]]
    assert "invented_code_x" not in codes
    assert "ungrounded_text" in codes


def test_container_type_is_restored_from_source_instead_of_hq_to_hc_rewrite():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [{"type": "40HC", "packages": 10, "volume_cbm": 20}],
        },
        source_text="托运人：某托运人公司\n箱型箱量：1 X 40HQ\n件数：10\n体积：20CBM",
    )

    assert result["containers"][0]["type"] == "40HQ"
    issue = next(
        item
        for item in result["review_issues"]
        if item["code"] == "container_type_not_verbatim"
    )
    assert issue["source_values"] == ["40HC", "40HQ"]
    assert issue["blocking"] is False


def test_detail_container_type_wins_over_stale_total_container_value():
    result = finalize_extraction(
        {
            "shipper_company": "上海柚理供应链管理有限公司",
            "factory": {"name": "皇裕精密技术（苏州）股份有限公司"},
            "containers": [
                {"type": "40HQ", "qty": 1},
                {
                    "type": "20GP",
                    "qty": 1,
                    "packages": 42,
                    "gross_weight_kg": 35000,
                    "volume_cbm": 70,
                },
            ],
            "review_issues": [
                {
                    "code": "conflicting_container_data",
                    "field": "containers",
                    "message": "总箱量标注40HQ×1 20GP×1，但详情箱型栏仅显示20GP×1，存在冲突",
                    "source_values": ["40HQ×1 20GP×1", "20GP×1"],
                    "blocking": True,
                }
            ],
        },
        source_text=(
            "托运人：上海柚理供应链管理有限公司\n"
            "总箱量：40HQ×1 20GP×1\n"
            "货名：热镀锌钢带等 箱型：20GP×1\n"
            "件数：42 毛重：35000 体积：70"
        ),
    )

    assert result["containers"] == [
        {
            "type": "20GP",
            "qty": 1,
            "packages": 42,
            "gross_weight_kg": 35000,
            "volume_cbm": 70,
        }
    ]
    assert not any(
        issue["code"] == "conflicting_container_data"
        for issue in result["review_issues"]
    )


def test_detail_container_precedence_does_not_hide_measurement_conflicts():
    result = finalize_extraction(
        {
            "shipper_company": "上海柚理供应链管理有限公司",
            "factory": {"name": "皇裕精密技术（苏州）股份有限公司"},
            "containers": [
                {
                    "type": "20GP",
                    "qty": 1,
                    "packages": 42,
                    "gross_weight_kg": 35000,
                    "volume_cbm": 70,
                }
            ],
            "review_issues": [
                {
                    "code": "conflicting_container_data",
                    "field": "containers[0]",
                    "message": "同一柜存在两组件数、毛重或体积，需人工裁决",
                    "source_values": ["42/35000/70", "41/34990/69"],
                    "blocking": True,
                }
            ],
        },
        source_text=(
            "托运人：上海柚理供应链管理有限公司\n"
            "总箱量：40HQ×1 20GP×1\n"
            "货名：热镀锌钢带等 箱型：20GP×1\n"
            "件数：42 毛重：35000 体积：70"
        ),
    )

    assert any(
        issue["code"] == "conflicting_container_data"
        for issue in result["review_issues"]
    )


def test_ungrounded_remark_is_removed_before_building_c_note():
    hallucination = "出口清关的装完箱后请及时进港作业资水！"
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "江阴市信腾新颖地面材料有限公司"},
            "remark": f"数据准确，箱单填好过去；{hallucination}",
            "order_mapping": {"c_note": hallucination},
            "containers": [{"type": "40HQ", "packages": 10, "volume_cbm": 10}],
        },
        source_text=(
            "托运人：某托运人公司\n"
            "箱型箱量：1 X 40HQ\n"
            "江阴市信腾新颖地面材料有限公司\n"
            "数据准确，箱单填好过去"
        ),
    )

    assert result["remark"] == "数据准确，箱单填好过去"
    assert result["order_mapping"]["c_note"] == "数据准确，箱单填好过去"
    assert hallucination not in (result["remark"] or "")
    assert hallucination not in (result["order_mapping"]["c_note"] or "")
    issue = next(item for item in result["review_issues"] if item["code"] == "ungrounded_text")
    assert issue["field"] == "remark"
    assert issue["source_values"] == ["出口清关的装完箱后请及时进港"]
    assert issue["blocking"] is True
    artifact_issue = next(
        item for item in result["review_issues"] if item["code"] == "confirmed_ocr_artifact"
    )
    assert artifact_issue["source_values"] == ["作业资水"]


def test_confirmed_ocr_artifact_is_removed_even_when_mineru_text_contains_it():
    artifact = "作业资水"
    source_remark = f"货名：无纺布；作业要求：出口清关的装完箱后请及时进港 {artifact}"
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "太仓本杰明纺织科技有限公司"},
            "remark": source_remark,
            "containers": [{"type": "40HQ", "packages": 249, "volume_cbm": 68}],
        },
        source_text=f"托运人：某托运人公司\n备注：{source_remark}",
    )

    expected = "货名：无纺布；作业要求：出口清关的装完箱后请及时进港"
    assert result["remark"] == expected
    assert result["order_mapping"]["c_note"] == expected
    assert artifact not in result["remark"]
    issue = next(
        item for item in result["review_issues"] if item["code"] == "confirmed_ocr_artifact"
    )
    assert issue["field"] == "remark"
    assert issue["source_values"] == [artifact]
    assert issue["blocking"] is True


def test_grounded_conflict_wrapper_in_container_remark_is_retained():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [
                {
                    "type": "40HC",
                    "packages": 2150,
                    "volume_cbm": 64.518,
                    "remark": "另有记录 2149CTNS, 5372.5KGS, 64.487CBM，待人工确认",
                }
            ],
        },
        source_text=(
            "托运人：某托运人公司\n箱型：40HC\n"
            "2150CTNS, 5375KGS, 64.518CBM\n2149CTNS, 5372.5KGS, 64.487CBM"
        ),
    )

    assert "另有记录 2149CTNS" in result["containers"][0]["remark"]
    assert not any(item["code"] == "ungrounded_text" for item in result["review_issues"])


def test_remark_label_is_removed_while_grounded_value_is_kept():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "remark": "备注说明：1750",
        },
        source_text="托运人：某托运人公司\n备注说明\n1750",
    )

    assert result["remark"] == "1750"
    assert result["order_mapping"]["c_note"] == "1750"
    assert not any(item["code"] == "ungrounded_text" for item in result["review_issues"])


def test_footer_contact_cannot_be_used_as_sender_contact():
    result = finalize_extraction(
        {
            "sender_contact": "ADA",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text="FM: 上海威世国际货物运输代理有限公司\n联系人：ADA",
    )

    assert result["sender_contact"] is None
    issue = next(
        item
        for item in result["review_issues"]
        if item["code"] == "sender_contact_not_from_from_field"
    )
    assert issue["source_values"] == ["ADA"]
    assert issue["blocking"] is True


def test_customer_prefers_fm_value_and_falls_back_to_header_company():
    fm_result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=(
            "上海凯福国际物流有限公司\nFM：海丰\n托运人：某托运人公司"
        ),
    )
    header_result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text="上海凯福国际物流有限公司\n托运人：某托运人公司",
    )
    labeled_result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text="客户名称：特格威\n托运人：某托运人公司",
    )
    notice_heading_result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text=(
            "_p2_ 海丰装箱通知\n_p5_ 提单号 : 8890207520\n"
            "_p7_ 箱型： 2*40HQ\n_p14_ 浙江省嘉兴市嘉善县姚庄镇利群路269号"
        ),
    )

    assert fm_result["customer"] == "海丰"
    assert header_result["customer"] == "上海凯福国际物流有限公司"
    assert labeled_result["customer"] == "特格威"
    assert notice_heading_result["customer"] == "海丰"
    assert not any(
        issue["code"] == "missing_customer"
        for issue in notice_heading_result["review_issues"]
    )
    assert not any(issue["code"] == "missing_customer" for issue in fm_result["review_issues"])
    assert (
        to_chinese(
            TuoshuOutput.model_validate(
                {
                    **fm_result,
                    "source": {"file": "test.docx", "doc_format": "docx"},
                }
            )
        )["客户"]
        == "海丰"
    )


def test_missing_customer_has_blocking_explanation():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
        },
        source_text="托运人：某托运人公司",
    )

    assert result["customer"] is None
    issue = next(issue for issue in result["review_issues"] if issue["code"] == "missing_customer")
    assert issue == {
        "code": "missing_customer",
        "field": "customer",
        "message": "缺少客户，未从 FM、客户栏或正文抬头提取到有效值，需人工确认",
        "source_values": [],
        "blocking": True,
    }
    assert result["ready_for_order"] is False


def test_four_character_container_type_and_measurement_precision_rules():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [
                {
                    "type": "25PL",
                    "packages": 10,
                    "packages_unit": "CTNS",
                    "gross_weight_kg": 123.45678,
                    "volume_cbm": 9.87654,
                }
            ],
        },
        source_text=(
            "托运人：某托运人公司\n箱型：25PL\n"
            "件数：10 CTNS\n毛重：123.45678 KGS\n体积：9.87654 CBM"
        ),
    )

    container = result["containers"][0]
    assert container["type"] == "25PL"
    assert container["packages"] == 10
    assert container["gross_weight_kg"] == 123.457
    assert container["volume_cbm"] == 9.877
    assert not any(issue["code"] == "unknown_container_type" for issue in result["review_issues"])


def test_unknown_container_type_is_preserved_with_non_blocking_review():
    result = finalize_extraction(
        {
            "mbl_no": "ABC1234567890",
            "customer": "测试客户",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点", "address": "某市某区某路1号"},
            "containers": [{"type": "40NOR", "packages": 10, "volume_cbm": 20}],
        },
        source_text=(
            "客户：测试客户\n托运人：某托运人公司\n"
            "箱型：40NOR\n件数：10\n体积：20"
        ),
    )

    assert result["containers"][0]["type"] == "40NOR"
    issue = next(issue for issue in result["review_issues"] if issue["code"] == "unknown_container_type")
    assert issue["blocking"] is False
    assert result["ready_for_order"] is True


def test_port_modifier_and_package_unit_are_restored_from_source():
    result = finalize_extraction(
        {
            "pod": "COLUMBUS",
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "containers": [
                {
                    "type": "40HQ",
                    "packages": None,
                    "packages_unit": None,
                    "volume_cbm": None,
                }
            ],
        },
        source_text=(
            "托运人：某托运人公司\n"
            "箱型/箱量：40HQ X 1\n"
            "件数：\nCTNS\n"
            "目的港：\nCOLUMBUS(OH)\n"
            "体积：CBM"
        ),
    )

    assert result["pod"] == "COLUMBUS, OH"
    assert result["containers"][0]["type"] == "40HQ"
    assert result["containers"][0]["packages"] is None
    assert result["containers"][0]["packages_unit"] == "CTNS"


def test_known_conflict_cannot_disable_blocking():
    result = finalize_extraction(
        {
            "shipper_company": "某托运人公司",
            "factory": {"name": "某门点"},
            "review_issues": [
                {
                    "code": "conflicting_container_data",
                    "field": "containers[2]",
                    "message": "柜3有两组数据",
                    "blocking": False,
                }
            ],
        },
        source_text=None,
    )

    assert result["review_issues"][0]["blocking"] is True
    assert result["ready_for_order"] is False
