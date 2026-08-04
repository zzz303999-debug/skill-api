"""附件文档 → 下单字段转换接口测试（不下单）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orders import document as document_module
from app.orders.document import (
    _extract_company_name,
    _is_header_company,
    _revise_c_title_to_value,
    build_document_order_data,
    normalize_document_extraction,
    parse_document_to_order,
)

client = TestClient(app)

COMPLETE_RAW = {
    "order_num1": "KMTCSHAP950393",
    "c_title": "海丰",
    "c_name": "华鑫老板娘",
    "c_phone": "15267966360",
    "b_ship_name": "CMA CGM ZEPHYR",
    "b_ship_num": "0GM4FW",
    "b_ship_company": "CMA CGM",
    "factory_name": "姚庄工厂",
    "factory_bei": "浙江省嘉兴市嘉善县姚庄镇利群路269号",
    "b_end_port": "FELIXSTOWE",
    "b_end_dock": "FELIXSTOWE DOCK",
    "b_wharf": "上海港",
    "b_open_ship_time": "2020-08-31",
    "b_date": "2026-07-20",
    "c_note": "托卡价格: 3000；注意不要破损",
    "packages": "100 CTNS",
    "gross_weight": "1234.5678 KGS",
    "volume": "25.5 CBM",
    "box": [
        {"b_type": "40HQ", "box_num": 2},
        {"b_type": "20GP", "box_num": 1},
    ],
}


def _fake_convert(file_bytes, filename):
    markdown = "海丰装箱通知\n提单号：KMTCSHAP950393\nFM：海丰\n做箱地址：浙江省嘉兴市嘉善县姚庄镇利群路269号\n做箱日期：2026年7月20日\n件数：100 CTNS\n毛重：1234.5678 KGS\n体积：25.5 CBM\n箱型箱量：2*40HQ + 1*20GP"
    return markdown, "pdf", {"parser": "pdfplumber"}, (
        "markdown:" + markdown
    )


# ---------- 归一化 ----------


def test_normalize_bill_no_accepts_8_plus_alphanumeric():
    extracted = normalize_document_extraction({**COMPLETE_RAW, "order_num1": "KMTCSHAP950393"})
    assert extracted.order_num1 == "KMTCSHAP950393"

    extracted = normalize_document_extraction({**COMPLETE_RAW, "order_num1": "12345678"})
    assert extracted.order_num1 == "12345678"


@pytest.mark.parametrize(
    "value",
    [
        "1234567",  # 不足 8 位
        "提单号1234567",  # 混合文字
        "1234_5678",  # 下划线视为非法字符
    ],
)
def test_normalize_bill_no_rejects_invalid(value):
    extracted = normalize_document_extraction({**COMPLETE_RAW, "order_num1": value})
    assert extracted.order_num1 is None


def test_normalize_bill_no_strips_separators():
    extracted = normalize_document_extraction({**COMPLETE_RAW, "order_num1": "KMTC SHAP-950393"})
    assert extracted.order_num1 == "KMTCSHAP950393"


def test_normalize_boxes_valid_types():
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "box": [{"b_type": "40HQ", "box_num": "2"}, {"b_type": "20GP", "box_num": 1}]}
    )
    assert [(b.b_type, b.box_num) for b in extracted.box] == [("40HQ", 2), ("20GP", 1)]


@pytest.mark.parametrize(
    "b_type",
    [
        "4HQ",      # 长度不对
        "400HQ",    # 长度不对
        "40HQX",    # 后缀 3 位
        "50HQ",     # 箱长不合法
        "40XX",     # 后缀不在白名单
        "ABC",      # 无数字
    ],
)
def test_normalize_boxes_rejects_invalid_type(b_type):
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "box": [{"b_type": b_type, "box_num": 1}]}
    )
    assert extracted.box == []


def test_normalize_boxes_rejects_zero_qty():
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "box": [{"b_type": "40HQ", "box_num": 0}]}
    )
    assert extracted.box == []


def test_normalize_measurements():
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "packages": "约 100 CTNS",
            "gross_weight": "1234.5678 KGS",
            "volume": "25.5CBM",
        }
    )
    assert extracted.packages == "100"
    assert extracted.gross_weight == "1234.568"
    assert extracted.volume == "25.5"


def test_normalize_measurements_invalid_values():
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "packages": "100.5", "gross_weight": "0", "volume": "ABC"}
    )
    assert extracted.packages is None
    assert extracted.gross_weight is None
    assert extracted.volume is None


def test_normalize_date_accepts_chinese_format():
    extracted = normalize_document_extraction({**COMPLETE_RAW, "b_date": "2026年7月20日"})
    assert extracted.b_date == "2026-07-20"


def test_normalize_date_rejects_invalid():
    extracted = normalize_document_extraction({**COMPLETE_RAW, "b_date": "下周二"})
    assert extracted.b_date is None


def test_normalize_standard_fields():
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "b_ship_name": " CMA CGM ZEPHYR ",
            "b_open_ship_time": "2020年8月31日",
        }
    )
    assert extracted.b_ship_name == "CMA CGM ZEPHYR"
    assert extracted.b_ship_num == "0GM4FW"
    assert extracted.b_ship_company == "CMA CGM"
    assert extracted.factory_name == "姚庄工厂"
    assert extracted.b_end_port == "FELIXSTOWE"
    assert extracted.b_end_dock == "FELIXSTOWE DOCK"
    assert extracted.b_wharf == "上海港"
    assert extracted.b_open_ship_time == "2020-08-31"
    assert extracted.c_name == "华鑫老板娘"
    assert extracted.c_phone == "15267966360"
    assert extracted.c_note == "托卡价格: 3000；注意不要破损"


def test_normalize_standard_fields_blank_or_invalid():
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "c_title": "",
            "b_ship_name": "   ",
            "b_open_ship_time": "下周三",
            "c_phone": 15267966360,  # 非字符串视为缺失
        }
    )
    assert extracted.c_title is None
    assert extracted.b_ship_name is None
    assert extracted.b_open_ship_time is None
    assert extracted.c_phone is None


# ---------- 必填校验 ----------


def test_build_document_order_data_driver_empty_object():
    """做箱日期缺失时 driver 显示空对象 [{}]，对齐标准格式。"""
    extracted = normalize_document_extraction({**COMPLETE_RAW, "b_date": None})
    order_data = build_document_order_data(extracted)
    assert order_data["driver"] == [{}]


def test_build_document_order_data_succeeds():
    extracted = normalize_document_extraction(COMPLETE_RAW)
    order_data = build_document_order_data(extracted, customer_id="open-id")

    assert order_data == {
        "order_num1": "KMTCSHAP950393",
        "type": 1,
        "c_id": "open-id",
        "c_title": "海丰",
        "c_name": "华鑫老板娘",
        "c_phone": "15267966360",
        "b_ship_name": "CMA CGM ZEPHYR",
        "b_ship_num": "0GM4FW",
        "b_ship_company": "CMA CGM",
        "factory_name": "姚庄工厂",
        "factory_bei": "浙江省嘉兴市嘉善县姚庄镇利群路269号",
        "b_factory_not": None,
        "b_start_dock": None,
        "b_end_port": "FELIXSTOWE",
        "b_end_dock": "FELIXSTOWE DOCK",
        "b_wharf": "上海港",
        "b_open_ship_time": "2020-08-31",
        "c_sn": None,
        "c_note": "托卡价格: 3000；注意不要破损",
        "data": [
            {
                "b_order_num": "KMTCSHAP950393",
                "j": "100",
                "m": "1234.568",
                "t": "25.5",
            }
        ],
        "box": [{"b_type": "40HQ", "box_num": 2}, {"b_type": "20GP", "box_num": 1}],
        "driver": [{"b_date": "2026-07-20"}],
    }


def test_missing_fields_returns_all_when_empty():
    extracted = normalize_document_extraction({})
    assert document_module._missing_fields(extracted) == [
        "order_num1",
        "box",
        "c_title",
        "factory_bei",
        "b_date",
        "packages",
        "gross_weight",
        "volume",
    ]


def test_missing_fields_reports_partial():
    extracted = normalize_document_extraction({**COMPLETE_RAW, "volume": "ABC"})
    assert document_module._missing_fields(extracted) == ["volume"]


# ---------- 主流程 ----------


def test_parse_document_to_order_extracts_without_publishing(monkeypatch):
    captured = {}

    def fake_convert(file_bytes, filename):
        captured["convert_called"] = True
        return _fake_convert(file_bytes, filename)

    def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return dict(COMPLETE_RAW), {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "_convert_file", fake_convert)
    monkeypatch.setattr(document_module, "chat_json", fake_chat_json)

    result = parse_document_to_order(b"fake-pdf", "order.pdf")

    assert captured["convert_called"] is True
    assert result["file"] == "order.pdf"
    assert result["order_data"]["order_num1"] == "KMTCSHAP950393"
    assert result["order_data"]["b_ship_name"] == "CMA CGM ZEPHYR"
    assert result["order_data"]["b_end_port"] == "FELIXSTOWE"
    assert result["order_data"]["driver"] == [{"b_date": "2026-07-20"}]
    assert result["meta"]["order_created"] is False
    assert result["needs_manual_confirmation"] is False
    assert result["missing_fields"] == []
    assert "upstream" not in result


def test_parse_document_to_order_marks_missing_fields(monkeypatch):
    monkeypatch.setattr(document_module, "_convert_file", _fake_convert)

    def fake_chat_json(messages, **_kwargs):
        return {"order_num1": "KMTCSHAP950393"}, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "chat_json", fake_chat_json)

    result = parse_document_to_order(b"fake-pdf", "order.pdf")

    assert result["needs_manual_confirmation"] is True
    assert result["missing_fields"] == [
        "box",
        "c_title",
        "factory_bei",
        "b_date",
        "packages",
        "gross_weight",
        "volume",
    ]
    assert result["missing_reasons"] == {
        field: "原文未找到，请人工确认"
        for field in result["missing_fields"]
    }
    # order_data 始终返回：缺字段显示 null，driver 为空时显示 [{}]
    assert result["order_data"] is not None
    assert result["order_data"]["driver"] == [{}]
    assert result["order_data"]["c_title"] is None


def test_parse_document_to_order_marks_invalid_values(monkeypatch):
    monkeypatch.setattr(document_module, "_convert_file", _fake_convert)

    def fake_chat_json(messages, **_kwargs):
        return {
            "order_num1": "KMTCSHAP950393",
            "box": [{"b_type": "40HQ", "box_num": 2}],
            "factory_bei": "浙江省嘉兴市嘉善县姚庄镇利群路269号",
            "b_date": "下周二",
            "packages": "100.5",
            "gross_weight": "0",
            "volume": "ABC",
        }, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "chat_json", fake_chat_json)

    result = parse_document_to_order(b"fake-pdf", "order.pdf")

    assert result["needs_manual_confirmation"] is True
    assert result["missing_fields"] == ["c_title", "b_date", "packages", "gross_weight", "volume"]
    assert result["missing_reasons"] == {
        "c_title": "原文未找到，请人工确认",
        "b_date": "格式不合法",
        "packages": "格式不合法",
        "gross_weight": "格式不合法",
        "volume": "格式不合法",
    }
    assert result["order_data"] is not None
    assert result["order_data"]["driver"] == [{}]


def test_parse_document_to_order_rejects_unsupported_extension(monkeypatch):
    with pytest.raises(Exception) as caught:
        parse_document_to_order(b"fake", "order.exe")

    assert caught.value.http_status == 400
    assert caught.value.code == "bad_request"


# ---------- API 路由 ----------


def test_parse_document_route_success(monkeypatch):
    import app.main as main_module

    async def fake_parse(file_bytes, filename):
        return {
            "file": filename,
            "extracted": normalize_document_extraction(COMPLETE_RAW).model_dump(),
            "order_data": {
                "order_num1": "KMTCSHAP950393",
                "type": 1,
                "c_title": "海丰",
            },
            "needs_manual_confirmation": False,
            "missing_fields": [],
            "missing_reasons": {},
            "meta": {"order_created": False, "model": "fake"},
        }

    monkeypatch.setattr(main_module, "_parse_document_to_order", fake_parse)

    response = client.post(
        "/orders/parse-document",
        files={"file": ("order.pdf", b"fake-pdf", "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["file"] == "order.pdf"
    assert body["order_data"]["order_num1"] == "KMTCSHAP950393"
    assert body["needs_manual_confirmation"] is False
    assert body["missing_fields"] == []
    assert body["missing_reasons"] == {}
    assert "meta" not in body


def test_parse_document_route_needs_manual_confirmation(monkeypatch):
    import app.main as main_module

    async def fake_parse(file_bytes, filename):
        return {
            "file": filename,
            "extracted": normalize_document_extraction({}).model_dump(),
            "order_data": {"order_num1": "KMTCSHAP950393", "driver": [{}]},
            "needs_manual_confirmation": True,
            "missing_fields": ["c_title", "b_date"],
            "missing_reasons": {
                "c_title": "原文未找到，请人工确认",
                "b_date": "格式不合法",
            },
            "meta": {"order_created": False, "model": "fake"},
        }

    monkeypatch.setattr(main_module, "_parse_document_to_order", fake_parse)

    response = client.post(
        "/orders/parse-document",
        files={"file": ("order.pdf", b"fake-pdf", "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["needs_manual_confirmation"] is True
    assert body["missing_fields"] == ["c_title", "b_date"]
    assert body["missing_reasons"] == {
        "c_title": "原文未找到，请人工确认",
        "b_date": "格式不合法",
    }
    # 缺字段时 order_data 仍返回（缺失项为 null，driver 为 [{}]）
    assert body["order_data"] == {"order_num1": "KMTCSHAP950393", "driver": [{}]}


def test_parse_document_route_requires_file():
    response = client.post("/orders/parse-document")
    assert response.status_code == 422


# ---------- c_title 兜底修复（只认 FM/FROM） ----------

_NOTICE_TEXT = (
    "江苏倍联现代物流有限公司\n"
    "常州赛格威做箱通知\n"
    "TO：俊泰\n"
    "客户编号:（对账时请注明：常州赛格威）\n"
    "做箱时间：2月28号\n"
    "关单号：SECU13842\n"
    "提货联系人及地址：\n"
    "常州市武进区夏城路395号 赛格威科技有限公司\n"
    "杨浩三 15851930053\n"
    "FROM:江苏倍联 陈俐玲"
)


def test_revise_c_title_ignores_fm_company_when_it_is_header_forwarder():
    """FM 公司是文档抬头货代（通知发出方）时，不得强制作为客户来源，置空走人工确认。"""
    assert _revise_c_title_to_value("俊泰", _NOTICE_TEXT) is None
    assert _revise_c_title_to_value("海丰", _NOTICE_TEXT) is None


def test_revise_c_title_keeps_real_customer_fm():
    """FM 公司不是抬头货代时（真实客户），仍以 FM 后的公司名为准。"""
    text = "常州赛格威做箱通知\nTO：俊泰\n做箱时间：2月28号\nFROM: 苏州哈亚精密机械有限公司 陈小姐\n"
    assert _revise_c_title_to_value("俊泰", text) == "苏州哈亚精密机械有限公司"


def test_revise_c_title_ignores_single_name_fm():
    """普通文本（非展平段落流）FM 单段人名同样不当作客户来源。"""
    text = "TO：俊泰\nFM：范颖晰\n提单号：ABC1234567\n"
    assert _revise_c_title_to_value("俊泰", text) is None


def test_revise_c_title_skips_header_forwarder_fm_for_real_customer_fm():
    """FM 是抬头货代（通知发出方）时继续扫描后续 FROM 行，取真实客户公司名。"""
    text = (
        "常州赛格威做箱通知\n"
        "FM: 上海威世国际货物运输代理有限公司\n"
        "TO：俊泰\n"
        "FROM: 苏州哈亚精密机械有限公司 陈小姐\n"
    )
    assert _revise_c_title_to_value("俊泰", text) == "苏州哈亚精密机械有限公司"


def test_is_header_company_avoids_short_and_table_cell_misjudge():
    """2 字简称与表格单元格不因子串匹配被误判为抬头货代。"""
    assert _is_header_company("倍联", ["江苏倍联现代物流有限公司"]) is False
    assert _is_header_company("东华工贸", ["3 | 做箱工厂 | 东华工贸"]) is False
    # 顶部区域 "FM: xxx" 行值即 FM 值 → 判定为抬头通知方（保守，不误放行货代）
    assert (
        _is_header_company("上海威世国际货物运输代理有限公司", ["FM: 上海威世国际货物运输代理有限公司"])
        is True
    )


def test_revise_c_title_ignores_fm_person_name():
    """展平段落流的 FM 人名行不匹配（漏检安全），不会把人名当客户。"""
    text = (
        "_p1_ 运输委托书\n"
        "_p2_ TO：上海捷阳国际货物运输代理有限公司\n"
        "_p3_ FM：范颖晰\n"
        "_p5_ 我司编号：WXHYC22010107\n"
    )
    assert _revise_c_title_to_value("范颖晰", text) is None
    assert _revise_c_title_to_value("上海捷阳国际货物运输代理有限公司", text) is None


def test_revise_c_title_reads_customer_label_after_flat_paragraph_prefix():
    """展平段落流中 _pN_ 前缀后的客户栏（行形式）仍可识别。"""
    text = "_p3_ 客户名称：特格威\n_p4_ 提单号：ABC1234567\n"
    assert _revise_c_title_to_value("特格威", text) == "特格威"
    # 值不匹配 LLM 提取值时不允许保留（防臆造）
    assert _revise_c_title_to_value("海丰", text) is None


def test_revise_c_title_ignores_fm_when_it_is_door_point_notice_forwarder():
    """门点装箱通知：FM=抬头货代不得强制覆盖 TO/门点值；抬头公司名本身仍是合法来源。"""
    text = (
        "上海威世国际货物运输代理有限公司\n"
        "门点装箱通知\n"
        "TO: 北跃物流\n"
        "FM: 上海威世国际货物运输代理有限公司\n"
        "提单号：TEST000011\n"
    )
    assert _revise_c_title_to_value("北跃物流", text) is None
    assert _revise_c_title_to_value("江阴市信腾新颖地面材料有限公司", text) is None
    # 抬头公司名本身是合法客户来源（与 prompt 规则一致）
    assert _revise_c_title_to_value("上海威世国际货物运输代理有限公司", text) == (
        "上海威世国际货物运输代理有限公司"
    )


def test_revise_c_title_returns_none_without_fm_from():
    """原文无 FM/FROM 行时置空（走人工确认），不放行 TO 收件方。"""
    text = "TO：俊泰\n关单号：SECU13842\n"
    assert _revise_c_title_to_value("俊泰", text) is None


def test_revise_c_title_rejects_fabricated_value():
    """原文无 FM/FROM、无客户栏、无抬头公司时，臆造值（如从文件名推断）置空。"""
    text = (
        "提单号：EASEK2615SB7008\n"
        "船名航次：EASLINE KWANGYANG V.2615E\n"
        "做箱地址：浙江省嘉兴市嘉善县姚庄镇利群路269号\n"
        "目的港：BUSAN\n"
        "开船时间：2026-04-25\n"
        "箱型：1*20GP\n"
    )
    assert _revise_c_title_to_value("集行供应链", text) is None


def test_revise_c_title_keeps_customer_label_value():
    """无 FM/FROM 行时，明确客户栏的值保留（行形式与表格形式）。"""
    assert _revise_c_title_to_value("特格威", "客户名称：特格威\n提单号：ABC1234567") == "特格威"
    assert (
        _revise_c_title_to_value(
            "海丰", "| 客户 | 海丰 |\n| 提单号 | ABC1234567 |"
        )
        == "海丰"
    )


def test_revise_c_title_keeps_notice_heading_company():
    """无 FM/FROM 行时，'客户简称+装箱/做箱通知'标题前缀可作客户来源。"""
    text = "海丰装箱通知\nTO：俊泰\n提单号：ABC1234567\n"
    assert _revise_c_title_to_value("海丰", text) == "海丰"


def test_revise_c_title_keeps_header_company_line():
    """无 FM/FROM 行时，顶部区域整行公司名保留。"""
    text = "江苏倍联现代物流有限公司\n常州赛格威做箱通知\nTO：俊泰\n"
    assert _revise_c_title_to_value("江苏倍联现代物流有限公司", text) == "江苏倍联现代物流有限公司"


def test_revise_c_title_handles_none():
    assert _revise_c_title_to_value(None, _NOTICE_TEXT) is None
    assert _revise_c_title_to_value("俊泰", None) == "俊泰"
    assert _revise_c_title_to_value(None, None) is None


def test_extract_company_name_strips_contact():
    assert _extract_company_name("江苏倍联 陈俐玲") == "江苏倍联"
    assert _extract_company_name("海丰") == "海丰"
    assert _extract_company_name("CMA CGM") == "CMA CGM"
    assert _extract_company_name("江苏倍联现代物流有限公司") == "江苏倍联现代物流有限公司"


def test_prompt_c_title_only_from_fm_from():
    """prompt 必须：客户取文档抬头或 FM/FROM 后的值，并禁止 TO/ATTN。"""
    prompt = document_module._SYSTEM_PROMPT
    assert "抬头" in prompt
    assert "FROM" in prompt
    assert "FM" in prompt
    assert "TO:" in prompt
    assert "禁止" in prompt
    assert "收件/通知对象" in prompt
    assert "对账时请注明" not in prompt
