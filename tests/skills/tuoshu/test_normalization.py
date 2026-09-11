"""LLM 输出归一化与日期/字段规范化测试。"""

from __future__ import annotations

import pytest

from app.core.errors import ParseError
from app.skills.tuoshu.mapping.finalize import finalize_extraction
from app.skills.tuoshu.normalize import normalize_llm_output


def test_business_number_and_customs_number_are_not_conflated():
    result = normalize_llm_output(
        {
            "我司业务编号": "WXHYC22010107",
            "报关单号": "223120220000123456",
        }
    )

    assert result["internal_ref"] == "WXHYC22010107"
    assert result["customs_declaration_no"] == "223120220000123456"


def test_structured_review_issue_aliases_are_normalized():
    result = normalize_llm_output(
        {
            "review_issues": [
                {
                    "问题代码": "known_issue",
                    "字段": "carrier",
                    "描述": "承运人需确认",
                    "候选值": "OOCL",
                    "是否阻断": False,
                }
            ]
        }
    )

    assert result["review_issues"] == [
        {
            "code": "known_issue",
            "field": "carrier",
            "message": "承运人需确认",
            "source_values": ["OOCL"],
            "blocking": False,
        }
    ]


@pytest.mark.parametrize(
    "review_issues",
    [
        ["carrier_by_vessel: 船名 OOCL 属 OOCL"],
        [{"问题": "箱号无法辨认"}],
        [{"code": "unstructured_review_issue", "field": "carrier", "message": "兜底"}],
        "missing_container_measurements: 件数无具体值",
    ],
)
def test_unstructured_review_issues_are_logged_and_rejected(review_issues, caplog):
    with caplog.at_level("ERROR"), pytest.raises(ParseError):
        normalize_llm_output({"review_issues": review_issues})

    assert "tuoshu_review_issue_parse_failed" in caplog.text


def test_finalize_cannot_silently_drop_unstructured_review_issue(caplog):
    with caplog.at_level("ERROR"), pytest.raises(ParseError):
        finalize_extraction(
            {"review_issues": ["missing_container_measurements: 件数无具体值"]},
            source_text=None,
        )

    assert "tuoshu_review_issue_parse_failed" in caplog.text


def test_chinese_combined_fields_are_normalized_to_schema():
    result = normalize_llm_output(
        {
            "船名航次": "RESURGENCE V.1728S",
            "开航时间": "2026-07-21",
            "开港时间": "2026-07-20",
            "拆装箱日期": "2026-07-19",
            "箱型箱量": "3*40HC",
            "件数": "2,150 CTNS",
            "毛重": "5,375.0 KGS",
            "体积": "64.518 CBM",
            "门点地址": "无锡市惠山区示例路1号",
            "工厂联系人": "范颖晔",
            "工厂电话": "13800000000",
            "发货人公司": "无锡某进出口有限公司",
            "备注": "原备注",
        }
    )

    assert result["vessel"] == "RESURGENCE"
    assert result["voyage"] == "1728S"
    assert result["etd"] == "2026-07-21"
    assert result["loading_time"] == "2026-07-19"
    assert result["remark"] == "原备注；开港时间：2026-07-20"
    assert result["containers"] == [
        {
            "type": "40HC",
            "qty": 3,
            "packages": 2150,
            "gross_weight_kg": 5375.0,
            "volume_cbm": 64.518,
            "packages_unit": "CTNS",
        }
    ]
    assert result["factory"] == {
        "address": "无锡市惠山区示例路1号",
        "contact": "范颖晔",
        "phone": "13800000000",
    }
    assert result["shipper_company"] == "无锡某进出口有限公司"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026/7/20", "2026-07-20"),
        ("2026.7.20", "2026-07-20"),
        ("2026年7月20日", "2026-07-20"),
        ("2026-07-20T08:30:00", "2026-07-20"),
    ],
)
def test_etd_common_date_spellings_are_normalized(value, expected):
    result = normalize_llm_output({"etd": value})

    assert result["etd"] == expected


def test_document_and_loading_dates_are_normalized_before_schema_validation():
    result = normalize_llm_output(
        {
            "doc_date": "2026年7月15日",
            "loading_time": "2026年7月16日 0:00",
        }
    )

    assert result["doc_date"] == "2026-07-15"
    assert result["loading_time"] == "2026-07-16T00:00:00"


def test_loading_time_without_space_after_chinese_day_is_normalized():
    result = normalize_llm_output({"loading_time": "2026年7月16日0:00"})

    assert result["loading_time"] == "2026-07-16T00:00:00"


def test_invalid_calendar_date_is_not_silently_rewritten():
    result = normalize_llm_output({"etd": "2026/2/30"})

    assert result["etd"] == "2026/2/30"


def test_slash_vessel_voyage_is_split():
    result = normalize_llm_output({"船名航次": "BALLENITA/0PPT4E"})

    assert result["vessel"] == "BALLENITA"
    assert result["voyage"] == "0PPT4E"


def test_spaced_table_aliases_normalize_to_schema_fields():
    result = normalize_llm_output(
        {"船  公  司": "EMC CPS", "中转港（卸港）": "USLAX", "船 期": "2026-01-21"}
    )

    assert result["carrier"] == "EMC CPS"
    assert result["transit_port"] == "USLAX"
    assert result["etd"] == "2026-01-21"


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [("40HQ", "40HQ"), ("40'HQ", "40HQ"), ("40'HC", "40HC"), ("40HC", "40HC")],
)
def test_container_type_preserves_hq_or_hc_suffix(raw_type, expected):
    normalized = normalize_llm_output({"containers": [{"type": raw_type}]})

    assert normalized["containers"][0]["type"] == expected
