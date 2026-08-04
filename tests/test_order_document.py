"""附件文档 → 下单字段转换接口测试（不下单）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orders import document as document_module
from app.orders.document import (
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
    markdown = "提单号：KMTCSHAP950393\nFM：海丰\n做箱地址：浙江省嘉兴市嘉善县姚庄镇利群路269号\n做箱日期：2026年7月20日\n件数：100 CTNS\n毛重：1234.5678 KGS\n体积：25.5 CBM\n箱型箱量：2*40HQ + 1*20GP"
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
