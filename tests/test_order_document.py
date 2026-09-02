"""附件文档 → 下单字段转换接口测试（不下单）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orders import document as document_module
from app.orders.document import (
    _extract_company_name,
    _extract_header_company,
    _is_header_company,
    _revise_bill_no,
    _revise_c_title_to_value,
    _revise_loading_time,
    _revise_port_fields,
    build_document_order_data,
    normalize_document_extraction,
    parse_document_to_order_async,
)

pytestmark = pytest.mark.asyncio

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
    "b_end_port": "SINGAPORE",
    "b_end_dock": "FELIXSTOWE",
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


async def test_normalize_bill_no_accepts_8_plus_alphanumeric():
    extracted = normalize_document_extraction({**COMPLETE_RAW, "order_num1": "KMTCSHAP950393"})
    assert extracted.order_num1 == "KMTCSHAP950393"

    extracted = normalize_document_extraction({**COMPLETE_RAW, "order_num1": "OOLU2120860080"})
    assert extracted.order_num1 == "OOLU2120860080"

    extracted = normalize_document_extraction({**COMPLETE_RAW, "order_num1": "12345678"})
    assert extracted.order_num1 == "12345678"


@pytest.mark.parametrize(
    "value",
    [
        "1234567",  # 不足 8 位
        "提单号1234567",  # 混合文字
        "1234_5678",  # 下划线视为非法字符
        "ABCDEFGH",  # 纯字母（船名/人名）
    ],
)
async def test_normalize_bill_no_rejects_invalid(value):
    extracted = normalize_document_extraction({**COMPLETE_RAW, "order_num1": value})
    assert extracted.order_num1 is None


async def test_normalize_bill_no_strips_separators():
    extracted = normalize_document_extraction({**COMPLETE_RAW, "order_num1": "KMTC SHAP-950393"})
    assert extracted.order_num1 == "KMTCSHAP950393"


# ---------- 关单号即提单号（兜底修复） ----------


async def test_revise_bill_no_customs_no_overrides_llm_value():
    """原文含关单号时覆盖 LLM 输出的运编号（关单号即提单号）。"""
    source = (
        "上海麦可斯国际物流有限公司\n装箱通知\n"
        "运编号：MAX202011824A/B/C\n关单号：CNWW036474\n"
        "船名航次：COSCO SHIPPING RHINE / 019W"
    )
    # LLM 误取运编号 → 覆盖为关单号
    assert _revise_bill_no("MAX202011824A", source) == "CNWW036474"
    # LLM 缺失 → 补全为关单号
    assert _revise_bill_no(None, source) == "CNWW036474"
    # LLM 已输出关单号 → 保持
    assert _revise_bill_no("CNWW036474", source) == "CNWW036474"


async def test_revise_bill_no_no_customs_no_keeps_value():
    """原文有提单号标签时以提单号标签值为准（提单号优先）。"""
    source = "运编号：MAX202011824A/B/C\n提单号：KMTCSHAP950393"
    assert _revise_bill_no("KMTCSHAP950393", source) == "KMTCSHAP950393"
    # LLM 误取运编号时，也会被提单号标签纠正
    assert _revise_bill_no("MAX202011824A", source) == "KMTCSHAP950393"
    assert _revise_bill_no(None, None) is None


async def test_revise_bill_no_bill_label_takes_priority_over_customs_no():
    """提单号标签优先：提单号与关单号并存且值不同时，取提单号标签值。"""
    source = "提单号：KMTCSHAP950393\n关单号：CNWW036474\n船名航次：COSCO SHIPPING RHINE / 019W"
    assert _revise_bill_no(None, source) == "KMTCSHAP950393"
    # LLM 输出关单号时，仍以提单号标签为准
    assert _revise_bill_no("CNWW036474", source) == "KMTCSHAP950393"


async def test_revise_bill_no_ignores_invalid_customs_no():
    """关单号不合提单号格式（如不足 8 位）时不覆盖，保持 LLM 值。"""
    source = "运编号：MAX202011824A\n关单号：1234567\n船名航次：COSCO SHIPPING RHINE / 019W"
    assert _revise_bill_no("MAX202011824A", source) == "MAX202011824A"


async def test_revise_bill_no_ignores_customs_declaration_no():
    """报关单号不是提单号：原文只有报关单号时不覆盖、不补全。"""
    source = "报关单号：CUS12345678\n船名航次：COSCO SHIPPING RHINE / 019W"
    assert _revise_bill_no("MAX202011824A", source) == "MAX202011824A"
    assert _revise_bill_no(None, source) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 中文间的 OCR 断字空格删除
        ("上海凯福国际物流有限公 司", "上海凯福国际物流有限公司"),
        ("上海凯 福国际物流有限公司", "上海凯福国际物流有限公司"),
        # 英文公司名内部空格保留（如双词名）
        ("XILINMEN GRID", "XILINMEN GRID"),
        # 首尾空白去除
        (" 海丰国际 ", "海丰国际"),
    ],
)
async def test_normalize_c_title_cleans_ocr_break_spaces(raw, expected):
    extracted = normalize_document_extraction({**COMPLETE_RAW, "c_title": raw})
    assert extracted.c_title == expected


async def test_normalize_boxes_valid_types():
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "box": [{"b_type": "40HQ", "box_num": "2"}, {"b_type": "20GP", "box_num": 1}]}
    )
    assert [(b.b_type, b.box_num) for b in extracted.box] == [("40HQ", 2), ("20GP", 1)]


async def test_normalize_boxes_merges_same_type():
    """多数据行同箱型时 box_num 累加（3×1*40HC → 一条 40HC/3）。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "box": [
                {"b_type": "40HC", "box_num": 1},
                {"b_type": "40HC", "box_num": 1},
                {"b_type": "40HC", "box_num": 1},
            ],
        }
    )
    assert [(b.b_type, b.box_num) for b in extracted.box] == [("40HC", 3)]


async def test_normalize_boxes_merges_mixed_types_preserving_order():
    """不同箱型分别累加，保持首次出现顺序。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "box": [
                {"b_type": "20GP", "box_num": 2},
                {"b_type": "40HC", "box_num": 1},
                {"b_type": "20GP", "box_num": 1},
            ],
        }
    )
    assert [(b.b_type, b.box_num) for b in extracted.box] == [("20GP", 3), ("40HC", 1)]


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
async def test_normalize_boxes_rejects_invalid_type(b_type):
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "box": [{"b_type": b_type, "box_num": 1}]}
    )
    assert extracted.box == []


async def test_normalize_boxes_rejects_zero_qty():
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "box": [{"b_type": "40HQ", "box_num": 0}]}
    )
    assert extracted.box == []


MULTI_ROW_DATA = [
    {"b_order_num": "OOLU2120860080", "j": "680", "m": "8602", "t": "33.92"},
    {"b_order_num": "OOLU2120860081", "j": "450", "m": "5036", "t": "22.18"},
    {"b_order_num": "OOLU2120860082", "j": "10", "m": "106", "t": "0.66"},
]


async def test_normalize_data_items_multiple_rows():
    """一票多客户/多提单号：data 每行一条，完整保留。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": MULTI_ROW_DATA,
            "packages": None,
            "gross_weight": None,
            "volume": None,
        }
    )
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("OOLU2120860080", "680", "8602", "33.92"),
        ("OOLU2120860081", "450", "5036", "22.18"),
        ("OOLU2120860082", "10", "106", "0.66"),
    ]


async def test_normalize_data_items_skips_incomplete_row():
    """行内件数/毛重/体积不全时整行跳过，不产生脏数据。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": [
                {"b_order_num": "OOLU2120860080", "j": "680", "m": "8602", "t": "33.92"},
                {"b_order_num": "OOLU2120860081", "j": "450", "m": None, "t": "22.18"},
            ],
        }
    )
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("OOLU2120860080", "680", "8602", "33.92"),
    ]


async def test_normalize_data_items_falls_back_to_single_values():
    """LLM 未输出 data 键时回退到单值字段组装一条（向后兼容）。"""
    extracted = normalize_document_extraction(COMPLETE_RAW)
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("KMTCSHAP950393", "100", "1234.568", "25.5"),
    ]


async def test_normalize_data_items_empty_data_falls_back_bill_no():
    """显式输出 data: [] 且单值缺失时，回退一条仅含提单号的行（data 不允许为空）。"""
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "data": [], "packages": None, "gross_weight": None, "volume": None}
    )
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("KMTCSHAP950393", None, None, None),
    ]
    # 件数/毛重/体积仍缺失，走人工确认
    assert document_module._missing_fields(extracted) == [
        "packages",
        "gross_weight",
        "volume",
    ]


async def test_normalize_data_items_ignores_non_list():
    """data 非列表（如 dict）时忽略，单值三项齐全则回退组装完整行。"""
    extracted = normalize_document_extraction({**COMPLETE_RAW, "data": {"j": "680"}})
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("KMTCSHAP950393", "100", "1234.568", "25.5"),
    ]


async def test_normalize_data_items_single_row_falls_back_bill_no():
    """单行明细且行内提单号缺失/非法时，回退主提单号（对齐 mapper 约定）。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": [{"j": "680", "m": "8602", "t": "33.92"}],
            "packages": None,
            "gross_weight": None,
            "volume": None,
        }
    )
    assert extracted.data[0].b_order_num == "KMTCSHAP950393"


async def test_normalize_data_items_accepts_numeric_types():
    """LLM 把 j/m/t 输出为 JSON number 时也能归一化，不整行丢弃。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": [
                {"b_order_num": "OOLU2120860080", "j": 680, "m": 8602, "t": 33.92}
            ],
        }
    )
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("OOLU2120860080", "680", "8602", "33.92"),
    ]


async def test_normalize_data_items_incomplete_rows_fall_back_to_single_values():
    """data 行内三项缺失/非法被过滤时，单值三项齐全则回退组装完整行。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": [{"b_order_num": "KMTCSHAP950393", "j": None, "m": None, "t": None}],
        }
    )
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("KMTCSHAP950393", "100", "1234.568", "25.5"),
    ]
    # 单值字段与回退行对齐，缺失校验通过
    assert extracted.packages == "100"
    assert document_module._missing_fields(extracted) == []


async def test_missing_fields_marks_measurements_when_rows_dropped():
    """data 行不全被过滤且单值不全时，回退仅含提单号的行并标记缺失（防静默丢数据）。"""
    extracted = normalize_document_extraction(
        {
            **{k: v for k, v in COMPLETE_RAW.items() if k != "volume"},
            "data": [{"b_order_num": "OOLU2120860080", "j": "680", "m": "8602"}],
        }
    )
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("KMTCSHAP950393", None, None, None),
    ]
    assert document_module._missing_fields(extracted) == [
        "packages",
        "gross_weight",
        "volume",
    ]

    extracted = normalize_document_extraction(
        {
            **{k: v for k, v in COMPLETE_RAW.items() if k != "gross_weight"},
            "data": [],
        }
    )
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("KMTCSHAP950393", None, None, None),
    ]
    assert document_module._missing_fields(extracted) == [
        "packages",
        "gross_weight",
        "volume",
    ]


async def test_missing_fields_cleared_when_single_values_fill_data_rows():
    """data 行不全/为空但单值三项齐全时，用单值组装完整行且不再标记缺失。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": [{"b_order_num": "OOLU2120860080", "j": "680", "m": "8602"}],
        }
    )
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("KMTCSHAP950393", "100", "1234.568", "25.5"),
    ]
    assert document_module._missing_fields(extracted) == []

    extracted = normalize_document_extraction({**COMPLETE_RAW, "data": []})
    assert [(d.b_order_num, d.j, d.m, d.t) for d in extracted.data] == [
        ("KMTCSHAP950393", "100", "1234.568", "25.5"),
    ]
    assert document_module._missing_fields(extracted) == []


async def test_build_document_order_data_fills_missing_bill_no():
    """多行明细中无提单号的行在组装层回填主提单号（对齐 mapper 契约）。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": [
                {"b_order_num": "OOLU2120860080", "j": "680", "m": "8602", "t": "33.92"},
                {"j": "450", "m": "5036", "t": "22.18"},
            ],
            "packages": None,
            "gross_weight": None,
            "volume": None,
        }
    )
    order_data = build_document_order_data(extracted)
    assert [d["b_order_num"] for d in order_data["data"]] == [
        "OOLU2120860080",
        "KMTCSHAP950393",
    ]


async def test_normalize_data_items_keeps_missing_bill_no_in_multi_row():
    """多行明细行内无提单号保持 null，不回退整票提单号。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": [
                {"b_order_num": "OOLU2120860080", "j": "680", "m": "8602", "t": "33.92"},
                {"j": "450", "m": "5036", "t": "22.18"},
            ],
            "packages": None,
            "gross_weight": None,
            "volume": None,
        }
    )
    assert [d.b_order_num for d in extracted.data] == ["OOLU2120860080", None]


async def test_single_values_aligned_with_first_data_row():
    """单值字段与 data[0] 不一致时，以 data[0] 为准保证响应自洽。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": [{"b_order_num": "OOLU2120860080", "j": "680", "m": "8602", "t": "33.92"}],
            "packages": "999",
            "gross_weight": "8888",
            "volume": "77.7",
        }
    )
    assert extracted.packages == "680"
    assert extracted.gross_weight == "8602"
    assert extracted.volume == "33.92"


async def test_normalize_measurements():
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


async def test_normalize_measurements_invalid_values():
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "packages": "100.5", "gross_weight": "0", "volume": "ABC"}
    )
    assert extracted.packages is None
    assert extracted.gross_weight is None
    assert extracted.volume is None


async def test_normalize_date_accepts_chinese_format():
    extracted = normalize_document_extraction({**COMPLETE_RAW, "b_date": "2026年7月20日"})
    assert extracted.b_date == "2026-07-20"


async def test_normalize_date_rejects_invalid():
    extracted = normalize_document_extraction({**COMPLETE_RAW, "b_date": "下周二"})
    assert extracted.b_date is None


async def test_normalize_standard_fields():
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
    assert extracted.b_end_port == "SINGAPORE"
    assert extracted.b_end_dock == "FELIXSTOWE"
    assert extracted.b_wharf == "上海港"
    assert extracted.b_open_ship_time == "2020-08-31"
    assert extracted.c_name == "华鑫老板娘"
    assert extracted.c_phone == "15267966360"
    assert extracted.c_note == "托卡价格: 3000；注意不要破损"


async def test_normalize_standard_fields_blank_or_invalid():
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


async def test_build_document_order_data_driver_empty_object():
    """做箱日期缺失时 driver 显示空对象 [{}]，对齐标准格式。"""
    extracted = normalize_document_extraction({**COMPLETE_RAW, "b_date": None})
    order_data = build_document_order_data(extracted)
    assert order_data["driver"] == [{}]


async def test_build_document_order_data_succeeds():
    extracted = normalize_document_extraction(COMPLETE_RAW)
    order_data = build_document_order_data(extracted)

    assert order_data == {
        "order_num1": "KMTCSHAP950393",
        "type": 1,
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
        "b_end_port": "SINGAPORE",
        "b_end_dock": "FELIXSTOWE",
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
                "hh": None,
                "mt": None,
            }
        ],
        "box": [{"b_type": "40HQ", "box_num": 2}, {"b_type": "20GP", "box_num": 1}],
        "driver": [{"b_date": "2026-07-20"}],
    }


async def test_build_document_order_data_keeps_all_data_rows():
    """多客户明细行全部保留进 order_data.data。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": MULTI_ROW_DATA,
            "packages": None,
            "gross_weight": None,
            "volume": None,
        }
    )
    order_data = build_document_order_data(extracted)
    assert order_data["data"] == [
        {"b_order_num": "OOLU2120860080", "j": "680", "m": "8602", "t": "33.92", "hh": None, "mt": None},
        {"b_order_num": "OOLU2120860081", "j": "450", "m": "5036", "t": "22.18", "hh": None, "mt": None},
        {"b_order_num": "OOLU2120860082", "j": "10", "m": "106", "t": "0.66", "hh": None, "mt": None},
    ]


async def test_missing_fields_satisfied_by_data_rows():
    """data 含完整明细行时，单值件数/毛重/体积缺失不算缺失。"""
    extracted = normalize_document_extraction(
        {
            **COMPLETE_RAW,
            "data": MULTI_ROW_DATA,
            "packages": None,
            "gross_weight": None,
            "volume": None,
        }
    )
    assert document_module._missing_fields(extracted) == []


async def test_missing_fields_reported_when_no_complete_row():
    """无完整明细行且单值缺失时，仍按单值字段标记缺失。"""
    raw = {
        **{k: v for k, v in COMPLETE_RAW.items() if k not in ("packages", "gross_weight", "volume")},
        "data": [{"b_order_num": "OOLU2120860080", "j": "680"}],
    }
    extracted = normalize_document_extraction(raw)
    assert document_module._missing_fields(extracted) == [
        "packages",
        "gross_weight",
        "volume",
    ]


async def test_missing_fields_returns_all_when_empty():
    extracted = normalize_document_extraction({})
    # 无提单号可回退时 data 返回 null，不静默为空数组
    assert extracted.data is None
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


async def test_data_null_when_no_bill_and_no_complete_row():
    """无完整明细行、单值不全且无提单号：data 返回 null 而非空数组（不静默）。"""
    extracted = normalize_document_extraction({"c_title": "某公司", "data": [{"j": "680"}]})
    assert extracted.data is None
    assert document_module._missing_fields(extracted) == [
        "order_num1",
        "box",
        "factory_bei",
        "b_date",
        "packages",
        "gross_weight",
        "volume",
    ]
    order_data = build_document_order_data(extracted)
    assert order_data["data"] is None


async def test_missing_fields_reports_partial():
    """单值三项不全时无法组装完整明细行，件数/毛重/体积全部标记缺失。"""
    extracted = normalize_document_extraction({**COMPLETE_RAW, "volume": "ABC"})
    assert document_module._missing_fields(extracted) == [
        "packages",
        "gross_weight",
        "volume",
    ]


# ---------- 主流程 ----------


async def test_parse_document_to_order_extracts_without_publishing(monkeypatch):
    captured = {}

    def fake_convert(file_bytes, filename):
        captured["convert_called"] = True
        return _fake_convert(file_bytes, filename)

    async def fake_chat_json(messages, **_kwargs):
        captured["messages"] = messages
        return dict(COMPLETE_RAW), {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "_convert_file", fake_convert)
    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-pdf", "order.pdf")

    assert captured["convert_called"] is True
    assert result["file"] == "order.pdf"
    assert result["order_data"]["order_num1"] == "KMTCSHAP950393"
    assert result["order_data"]["b_ship_name"] == "CMA CGM ZEPHYR"
    assert result["order_data"]["b_end_port"] == "SINGAPORE"
    assert result["order_data"]["driver"] == [{"b_date": "2026-07-20"}]
    assert result["meta"]["order_created"] is False
    assert result["needs_manual_confirmation"] is False
    assert result["missing_fields"] == []
    assert "upstream" not in result


async def test_parse_document_customs_no_overrides_llm_bill_no(monkeypatch):
    """文档同时含运编号与关单号：order_num1 取关单号（关单号即提单号）。"""

    def fake_convert(file_bytes, filename):
        markdown = (
            "上海麦可斯国际物流有限公司\n装箱通知\n"
            "运编号：MAX202011824A/B/C\n关单号：CNWW036474\n"
            "船名航次：COSCO SHIPPING RHINE / 019W\n"
            "目的港：PORT SAID WEST"
        )
        return markdown, "pdf", {"parser": "pdfplumber"}, ("markdown:" + markdown)

    async def fake_chat_json(messages, **_kwargs):
        return {"order_num1": "MAX202011824A"}, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "_convert_file", fake_convert)
    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-pdf", "order.pdf")
    assert result["extracted"]["order_num1"] == "CNWW036474"
    assert result["order_data"]["order_num1"] == "CNWW036474"


async def test_parse_document_data_keeps_bill_no_when_no_cargo_rows(monkeypatch):
    """整票无货物明细行（件数/毛重/体积缺失）时，data 输出一条仅含提单号的行。"""

    def fake_convert(file_bytes, filename):
        markdown = (
            "_p1_ 运编号：MAX202011824A/B/C\n"
            "_p2_ TO：上海乐博/老蔡\n"
            "_p4_ 上海麦可斯国际物流有限公司\n"
            "_p5_ 装箱通知\n"
            "_p7_ 地址：浙江省湖州练市工业区彩蝶路1号 湖州彩蝶纺织有限公司\n"
            "_p8_ 安排车子 2020-11-20（周五）上午8:00去装柜\n"
            "_p9_ 船名航次：COSCO SHIPPING RHINE / 019W\n"
            "_p11_ 关单号：CNWW036474\n"
            "_p15_ CTNR：1X20GP\n"
            "_p16_ 船期：2020-11-25\n"
            "_p17_ 目的港：PORT SAID WEST\n"
            "_p21_ 周五不管开港与否，都正常做箱！"
        )
        return markdown, "doc", {"parser": "local"}, ("markdown:" + markdown)

    async def fake_chat_json(messages, **_kwargs):
        return {
            "order_num1": "CNWW036474",
            "c_title": "上海麦可斯国际物流有限公司",
            "c_name": "魏先生",
            "c_phone": "13757324928",
            "b_ship_name": "COSCO SHIPPING RHINE",
            "b_ship_num": "019W",
            "factory_name": "湖州彩蝶纺织有限公司",
            "factory_bei": "浙江省湖州练市工业区彩蝶路1号 魏先生：13757324928",
            "b_end_dock": "PORT SAID WEST",
            "b_open_ship_time": "2020-11-25",
            "b_date": "2020-11-20",
            "b_date_time_start": "上午8:00",
            "c_sn": "MAX202011824A/B/C",
            "c_note": "周五不管开港与否，都正常做箱！",
            "packages": None,
            "gross_weight": None,
            "volume": None,
            "data": [],
            "box": [{"b_type": "20GP", "box_num": 1}],
        }, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "_convert_file", fake_convert)
    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-doc", "湖州彩蝶(2).doc")
    # data 必须有提单号（对齐自由文本 mapper 契约：每行提单号非空）
    assert result["extracted"]["data"] == [
        {"b_order_num": "CNWW036474", "j": None, "m": None, "t": None, "hh": None, "mt": None}
    ]
    assert result["order_data"]["data"] == [
        {"b_order_num": "CNWW036474", "j": None, "m": None, "t": None, "hh": None, "mt": None}
    ]
    # 件数/毛重/体积原文缺失，仍标记人工确认
    assert result["missing_fields"] == ["packages", "gross_weight", "volume"]
    assert result["needs_manual_confirmation"] is True


async def test_parse_document_to_order_marks_missing_fields(monkeypatch):
    monkeypatch.setattr(document_module, "_convert_file", _fake_convert)

    async def fake_chat_json(messages, **_kwargs):
        return {"order_num1": "KMTCSHAP950393"}, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-pdf", "order.pdf")

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


@pytest.mark.parametrize(
    "end_port, end_dock, source_text, expected_port, expected_dock",
    [
        # 目的港值被误填进 b_end_port → 挪到 b_end_dock，b_end_port 取中转港标签值
        (
            "BANDAR ABBAS",
            None,
            "船名航次：PANCON GLORY V.2624E\n中转港：INCHON\n目的港：BANDAR ABBAS",
            "INCHON",
            "BANDAR ABBAS",
        ),
        # 中转港值被误填进 b_end_dock → 挪到 b_end_port
        (
            None,
            "INCHON",
            "船名航次：PANCON GLORY V.2624E\n中转港：INCHON\n目的港：BANDAR ABBAS",
            "INCHON",
            "BANDAR ABBAS",
        ),
        # 只有中转港，无目的港 → b_end_dock 置空人工确认
        (
            None,
            "INCHON",
            "船名航次：PANCON GLORY V.2624E\n中转港：INCHON",
            "INCHON",
            None,
        ),
        # 原文同时含“中转港代码”与“目的港”
        (
            "TRIESTE",
            None,
            "中转港代码：ITTRS\n目的港：TRIESTE\n1*40HC",
            "ITTRS",
            "TRIESTE",
        ),
        # 正常提取（b_end_port=中转港、b_end_dock=目的港）→ 原样保留
        (
            None,
            "FELIXSTOWE",
            "起运港：SHANGHAI\n目的港：FELIXSTOWE\n件数：100 CTNS",
            None,
            "FELIXSTOWE",
        ),
        # 英文标签 TRANSSHIPMENT PORT / PORT OF DISCHARGE
        (
            "JEDDAH",
            None,
            "TRANSSHIPMENT PORT: SINGAPORE\nPORT OF DISCHARGE: JEDDAH",
            "SINGAPORE",
            "JEDDAH",
        ),
        # 无原文 → 原样返回
        ("INCHON", "FELIXSTOWE", None, "INCHON", "FELIXSTOWE"),
    ],
)
async def test_revise_port_fields_avoids_confusion(
    end_port, end_dock, source_text, expected_port, expected_dock
):
    assert _revise_port_fields(end_port, end_dock, source_text) == (
        expected_port,
        expected_dock,
    )


async def test_parse_document_to_order_fixes_port_confusion(monkeypatch):
    """LLM 把目的港填进 b_end_port 时，后处理修正为 b_end_port=中转港、b_end_dock=目的港。"""
    def fake_convert(file_bytes, filename):
        markdown = ("运输委托书\n船名航次：PANCON GLORY V.2624E\n"
                    "中转港：INCHON\n目的港：BANDAR ABBAS\n件数：37\n毛重：20001\n体积：41.453")
        return markdown, "pdf", {"parser": "pdfplumber"}, ("markdown:" + markdown)

    async def fake_chat_json(messages, **_kwargs):
        raw = dict(COMPLETE_RAW)
        raw["b_end_port"] = "BANDAR ABBAS"  # LLM 误把目的港填进 b_end_port（中转港字段）
        raw["b_end_dock"] = None
        return raw, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "_convert_file", fake_convert)
    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-pdf", "order.pdf")

    assert result["extracted"]["b_end_port"] == "INCHON"
    assert result["extracted"]["b_end_dock"] == "BANDAR ABBAS"
    assert result["order_data"]["b_end_port"] == "INCHON"
    assert result["order_data"]["b_end_dock"] == "BANDAR ABBAS"


async def test_parse_document_to_order_keeps_transit_port(monkeypatch):
    """parse-document 按订单创建接口文档语义：b_end_port=中转港、b_end_dock=目的港。"""
    def fake_convert(file_bytes, filename):
        markdown = ("做箱通知书\n做箱时间：2021-03-31\n做箱工厂：宝时得园龙\n"
                    "地址：金泰路转诚泰路17号\n我司业务编号：ESFF21030770\n"
                    "客户编号：8650135180\n船名航次：EVER LAUREL 28046W\n"
                    "提单号：OOLU2120860080\n目的港：SANTOS\n中转港：见设\n"
                    "港区：洋一\n船期：2021-04-03\n件数：680\n毛重：8602\n体积：33.92\n箱型箱量：1*40HC")
        return markdown, "pdf", {"parser": "pdfplumber"}, ("markdown:" + markdown)

    async def fake_chat_json(messages, **_kwargs):
        raw = dict(COMPLETE_RAW)
        raw["b_end_port"] = "见设"
        raw["b_end_dock"] = "SANTOS"
        raw["b_wharf"] = "洋一"
        return raw, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "_convert_file", fake_convert)
    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-pdf", "order.pdf")

    assert result["extracted"]["b_end_port"] == "见设"
    assert result["extracted"]["b_end_dock"] == "SANTOS"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 时间描述正常透传
        ({"b_date_time_start": "早上8点"}, "早上8点"),
        ({"b_date_time_start": "9:00"}, "9:00"),
        ({"b_date_time_start": "下午2点"}, "下午2点"),
        # 日期形态拒绝（日期应入 b_date）
        ({"b_date_time_start": "2026-07-20"}, None),
        ({"b_date_time_start": "2026/7/20"}, None),
        ({"b_date_time_start": "2026年7月20日"}, None),
        ({"b_date_time_start": "20260720"}, None),
        # 空值
        ({"b_date_time_start": ""}, None),
        ({}, None),
    ],
)
async def test_normalize_b_date_time_start(raw, expected):
    extracted = normalize_document_extraction(raw)
    assert extracted.b_date_time_start == expected


async def test_build_driver_merges_date_and_time():
    extracted = normalize_document_extraction(
        {"b_date": "2026-07-20", "b_date_time_start": "早上8点"}
    )
    order_data = build_document_order_data(extracted)
    assert order_data["driver"] == [{"b_date": "2026-07-20", "b_date_time_start": "早上8点"}]


async def test_build_driver_time_only_without_date():
    extracted = normalize_document_extraction({"b_date_time_start": "9:00"})
    order_data = build_document_order_data(extracted)
    assert order_data["driver"] == [{"b_date_time_start": "9:00"}]


async def test_normalize_data_items_keeps_hh_mt_non_empty():
    extracted = normalize_document_extraction(
        {
            "data": [
                {"b_order_num": "OOLU2120860080", "j": "680", "m": "8602", "t": "33.92", "hh": "电子产品", "mt": "N/M"}
            ]
        }
    )
    assert extracted.data[0].hh == "电子产品"
    assert extracted.data[0].mt == "N/M"
    # 非字符串输入安全降级
    extracted2 = normalize_document_extraction(
        {
            "data": [
                {"b_order_num": "OOLU2120860080", "j": "680", "m": "8602", "t": "33.92", "hh": ["x"], "mt": 123}
            ]
        }
    )
    assert extracted2.data[0].hh is None
    assert extracted2.data[0].mt is None


@pytest.mark.parametrize(
    "end_port, end_dock, source_text, expected_port, expected_dock",
    [
        # 待查描述不允许作为目的港补全（占位值不进 b_end_dock）
        (
            None,
            None,
            "中转港：见设\n目的港：见设",
            "见设",
            None,
        ),
        # 单行混排：捕获值在下一标签起点截断，不被“港区/中转港”污染
        (
            None,
            None,
            "目的港：SANTOS 中转港：见设 港区：洋一",
            "见设",
            "SANTOS",
        ),
        # 英文变体 PORT OF TRANSSHIPMENT / FINAL PORT OF DISCHARGE
        (
            None,
            "JEDDAH",
            "PORT OF TRANSSHIPMENT: SINGAPORE\nFINAL PORT OF DISCHARGE: JEDDAH",
            "SINGAPORE",
            "JEDDAH",
        ),
        # 目的港补全遇占位描述 → None（人工确认）
        (
            None,
            None,
            "中转港：INCHON\n目的港：待定",
            "INCHON",
            None,
        ),
    ],
)
async def test_revise_port_fields_boundary(
    end_port, end_dock, source_text, expected_port, expected_dock
):
    assert _revise_port_fields(end_port, end_dock, source_text) == (
        expected_port,
        expected_dock,
    )


@pytest.mark.parametrize(
    "value",
    ["代码", "中转港代码", "中转港", "转运港", "目的港", "港区", " 代码 "],
)
async def test_normalize_port_rejects_label_token(value):
    """端口字段拒绝标签词残留（如“中转港代码：”空值被输出为“代码”）。"""
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "b_end_port": value, "b_end_dock": value}
    )
    assert extracted.b_end_port is None
    assert extracted.b_end_dock is None


async def test_normalize_port_keeps_real_value():
    extracted = normalize_document_extraction(
        {**COMPLETE_RAW, "b_end_port": "见设", "b_end_dock": "SANTOS"}
    )
    assert extracted.b_end_port == "见设"
    assert extracted.b_end_dock == "SANTOS"


@pytest.mark.parametrize(
    "value, source_text, expected",
    [
        # 截单时间被当成装箱时间 → 置 None
        (
            "早上9点",
            "做箱时间：1月7日（1X40HC）\n截单时间：1-7早上9点",
            None,
        ),
        # 做箱/装箱时间标签后的时间描述 → 保留
        (
            "早上8点",
            "装箱时间：早上8点\n截单时间：1-7下午2点",
            "早上8点",
        ),
        # 截关时间同样拒绝
        (
            "9:00",
            "截关时间：9:00\n预计开船：1-10",
            None,
        ),
        # 无原文 → 原样返回
        ("早上9点", None, "早上9点"),
        (None, "截单时间：1-7早上9点", None),
    ],
)
async def test_revise_loading_time_rejects_cutoff_time(value, source_text, expected):
    assert _revise_loading_time(value, source_text) == expected


async def test_parse_document_to_order_marks_vision_skipped(monkeypatch):
    """vision 交叉核验因图片超限被跳过时，即使字段完整也必须要求人工确认。"""
    def fake_convert_vision_degraded(file_bytes, filename):
        markdown, doc_format, _, user_content = _fake_convert(file_bytes, filename)
        return markdown, doc_format, {"vision_skipped_reason": "image_too_large"}, user_content

    async def fake_chat_json(messages, **_kwargs):
        return dict(COMPLETE_RAW), {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "_convert_file", fake_convert_vision_degraded)
    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-pdf", "order.pdf")

    assert result["missing_fields"] == []
    assert result["needs_manual_confirmation"] is True
    assert result["meta"]["vision_skipped_reason"] == "image_too_large"


async def test_parse_document_to_order_marks_invalid_values(monkeypatch):
    monkeypatch.setattr(document_module, "_convert_file", _fake_convert)

    async def fake_chat_json(messages, **_kwargs):
        return {
            "order_num1": "KMTCSHAP950393",
            "box": [{"b_type": "40HQ", "box_num": 2}],
            "factory_bei": "浙江省嘉兴市嘉善县姚庄镇利群路269号",
            "b_date": "下周二",
            "packages": "100.5",
            "gross_weight": "0",
            "volume": "ABC",
        }, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-pdf", "order.pdf")

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


async def test_parse_document_marks_measurement_not_in_row(monkeypatch):
    """单值毛重格式合法且已归一，但明细行缺件数/体积被跳过时，
    缺失原因标"已提取但明细行不完整"而非误导性的"格式不合法"。"""
    monkeypatch.setattr(document_module, "_convert_file", _fake_convert)

    async def fake_chat_json(messages, **_kwargs):
        return {
            "order_num1": "275005063",
            "c_sn": "HZC2608099",
            "gross_weight": "8000",
        }, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-doc", "275005063.doc")

    assert result["extracted"]["gross_weight"] == "8000"
    # 明细行缺件数/体积被跳过 → 回退仅含提单号的行，三项仍标记缺失
    assert {"packages", "gross_weight", "volume"} <= set(result["missing_fields"])
    assert result["missing_reasons"]["packages"] == "原文未找到，请人工确认"
    assert result["missing_reasons"]["gross_weight"] == "已提取但明细行不完整，请人工确认"
    assert result["missing_reasons"]["volume"] == "原文未找到，请人工确认"
    assert result["order_data"]["data"][0]["m"] is None
    assert result["order_data"]["data"][0]["j"] is None
    assert result["order_data"]["data"][0]["t"] is None


async def test_parse_document_to_order_rejects_unsupported_extension(monkeypatch):
    with pytest.raises(Exception) as caught:
        await parse_document_to_order_async(b"fake", "order.exe")

    assert caught.value.http_status == 400
    assert caught.value.code == "bad_request"


# ---------- API 路由 ----------


async def test_parse_document_route_success(monkeypatch):
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


async def test_parse_document_route_needs_manual_confirmation(monkeypatch):
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


async def test_parse_document_route_requires_file():
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


async def test_revise_c_title_ignores_fm_company_when_it_is_header_forwarder():
    """FM 公司是文档抬头货代（通知发出方）时，不得强制作为客户来源，置空走人工确认。"""
    assert _revise_c_title_to_value("俊泰", _NOTICE_TEXT) is None
    assert _revise_c_title_to_value("海丰", _NOTICE_TEXT) is None


async def test_revise_c_title_keeps_real_customer_fm():
    """FM 公司不是抬头货代时（真实客户），仍以 FM 后的公司名为准。"""
    text = "常州赛格威做箱通知\nTO：俊泰\n做箱时间：2月28号\nFROM: 苏州哈亚精密机械有限公司 陈小姐\n"
    assert _revise_c_title_to_value("俊泰", text) == "苏州哈亚精密机械有限公司"


async def test_revise_c_title_ignores_single_name_fm():
    """普通文本（非展平段落流）FM 单段人名同样不当作客户来源。"""
    text = "TO：俊泰\nFM：范颖晰\n提单号：ABC1234567\n"
    assert _revise_c_title_to_value("俊泰", text) is None


async def test_revise_c_title_skips_header_forwarder_fm_for_real_customer_fm():
    """FM 是抬头货代（通知发出方）时继续扫描后续 FROM 行，取真实客户公司名。"""
    text = (
        "常州赛格威做箱通知\n"
        "FM: 上海威世国际货物运输代理有限公司\n"
        "TO：俊泰\n"
        "FROM: 苏州哈亚精密机械有限公司 陈小姐\n"
    )
    assert _revise_c_title_to_value("俊泰", text) == "苏州哈亚精密机械有限公司"


async def test_is_header_company_avoids_short_and_table_cell_misjudge():
    """2 字简称与表格单元格不因子串匹配被误判为抬头货代。"""
    assert _is_header_company("倍联", ["江苏倍联现代物流有限公司"]) is False
    assert _is_header_company("东华工贸", ["3 | 做箱工厂 | 东华工贸"]) is False
    # 顶部区域 "FM: xxx" 行值即 FM 值 → 判定为抬头通知方（保守，不误放行货代）
    assert (
        _is_header_company("上海威世国际货物运输代理有限公司", ["FM: 上海威世国际货物运输代理有限公司"])
        is True
    )


async def test_revise_c_title_ignores_fm_person_name():
    """展平段落流的 FM 人名行不匹配（漏检安全），不会把人名当客户。"""
    text = (
        "_p1_ 运输委托书\n"
        "_p2_ TO：上海捷阳国际货物运输代理有限公司\n"
        "_p3_ FM：范颖晰\n"
        "_p5_ 我司编号：WXHYC22010107\n"
    )
    assert _revise_c_title_to_value("范颖晰", text) is None
    assert _revise_c_title_to_value("上海捷阳国际货物运输代理有限公司", text) is None


async def test_revise_c_title_reads_customer_label_after_flat_paragraph_prefix():
    """展平段落流中 _pN_ 前缀后的客户栏（行形式）仍可识别。"""
    text = "_p3_ 客户名称：特格威\n_p4_ 提单号：ABC1234567\n"
    assert _revise_c_title_to_value("特格威", text) == "特格威"
    # 值不匹配 LLM 提取值时不允许保留（防臆造）
    assert _revise_c_title_to_value("海丰", text) is None


async def test_revise_c_title_ignores_fm_when_it_is_door_point_notice_forwarder():
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


async def test_revise_c_title_returns_none_without_fm_from():
    """原文无 FM/FROM 行时置空（走人工确认），不放行 TO 收件方。"""
    text = "TO：俊泰\n关单号：SECU13842\n"
    assert _revise_c_title_to_value("俊泰", text) is None


async def test_revise_c_title_keeps_company_in_transport_order_title():
    """“公司名+海运出口运输委托单”连写标题：标题公司是合法客户来源。"""
    text = (
        "# +江苏亚东朗升国际物流有限公司海运出口运输委托单\n"
        "TO：上海柚理供应链管理有限公司\n"
        "基本信息：\n"
    )
    assert _revise_c_title_to_value("江苏亚东朗升国际物流有限公司", text) == (
        "江苏亚东朗升国际物流有限公司"
    )
    # TO 收件人仍不是客户来源
    assert _revise_c_title_to_value("上海柚理供应链管理有限公司", text) is None


async def test_extract_header_company_returns_company_only_from_transport_order_title():
    """委托单标题兜底补全只返回公司名部分，不得把整个标题行当公司名。"""
    text = "# +江苏亚东朗升国际物流有限公司海运出口运输委托单\nTO：上海柚理供应链管理有限公司\n"
    assert _extract_header_company(text) == "江苏亚东朗升国际物流有限公司"


async def test_extract_header_company_keeps_notice_heading_prefix():
    """原有“客户简称+装箱/做箱通知”标题前缀解析不受影响。"""
    text = "常州赛格威做箱通知\nTO：俊泰\n做箱时间：2月28号\n"
    assert _extract_header_company(text) == "常州赛格威"


async def test_extract_header_company_keeps_transport_order_title_short_prefix():
    """委托单标题前缀过短（如“运输委托书”纯标题行）不作为公司名。"""
    text = "运输委托书\nTO：某客户\n提单号：ABC1234567\n"
    assert _extract_header_company(text) is None


async def test_revise_c_title_rejects_fabricated_value():
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


async def test_revise_c_title_keeps_customer_label_value():
    """无 FM/FROM 行时，明确客户栏的值保留（行形式与表格形式）。"""
    assert _revise_c_title_to_value("特格威", "客户名称：特格威\n提单号：ABC1234567") == "特格威"
    assert (
        _revise_c_title_to_value(
            "海丰", "| 客户 | 海丰 |\n| 提单号 | ABC1234567 |"
        )
        == "海丰"
    )


async def test_revise_c_title_keeps_notice_heading_company():
    """无 FM/FROM 行时，'客户简称+装箱/做箱通知'标题前缀可作客户来源。"""
    text = "海丰装箱通知\nTO：俊泰\n提单号：ABC1234567\n"
    assert _revise_c_title_to_value("海丰", text) == "海丰"


async def test_revise_c_title_keeps_header_company_line():
    """无 FM/FROM 行时，顶部区域整行公司名保留。"""
    text = "江苏倍联现代物流有限公司\n常州赛格威做箱通知\nTO：俊泰\n"
    assert _revise_c_title_to_value("江苏倍联现代物流有限公司", text) == "江苏倍联现代物流有限公司"


async def test_revise_c_title_keeps_table_header_company():
    """表格形式的文档抬头公司（xlsx 转换场景）仍可通过兜底校验。"""
    text = "| 1 | 浙江经茂国际货运代理有限公司 |  |\n| 2 | 做箱通知 |  |\n"
    assert (
        _revise_c_title_to_value("浙江经茂国际货运代理有限公司", text)
        == "浙江经茂国际货运代理有限公司"
    )


async def test_revise_c_title_rejects_label_row_values():
    """表格标签行的值（联系人姓名/装箱工厂）不得验证为客户，防止绕过人工确认。"""
    text = "| 2 | 联系人 | 范颖晰 |\n| 12 | 装箱工厂 | 喜临门家具有限公司 |\n"
    assert _revise_c_title_to_value("范颖晰", text) is None
    assert _revise_c_title_to_value("喜临门家具有限公司", text) is None


# ---------- 抬头公司主动提取（LLM 未给出 c_title 时补全） ----------


async def test_extract_header_company_picks_company_line():
    """普通文档顶部独立公司名行 → 主动提取为客户，不依赖文件名。"""
    text = "江苏倍联现代物流有限公司\n常州赛格威做箱通知\nTO：俊泰\n做箱时间：2月28号\n"
    assert _extract_header_company(text) == "江苏倍联现代物流有限公司"


async def test_extract_header_company_picks_table_company_cell():
    """xlsx 表格转换的顶部公司名（表格第一行）也能识别。"""
    text = (
        "| 1 | 浙江经茂国际货运代理有限公司 |  |\n"
        "| 2 | 做箱通知 |  |\n"
        "| 3 | 联系人：徐 | 15925812246 |\n"
        "| 12 | 装箱工厂 | 喜临门家具 |\n"
    )
    assert _extract_header_company(text) == "浙江经茂国际货运代理有限公司"


async def test_extract_header_company_picks_notice_heading():
    """“客户简称+做箱通知”标题前缀可作客户来源。"""
    text = "常州赛格威做箱通知\nTO：俊泰\n做箱地址：常州市武进区夏城路395号\n"
    assert _extract_header_company(text) == "常州赛格威"


async def test_extract_header_company_ignores_pure_notice_title_and_factory():
    """纯单据标题 + 工厂行不作为客户（不误取装箱工厂）。"""
    text = "做箱通知书\n做箱时间：2021-03-31\n| 12 | 装箱工厂 | 喜临门家具 |\n"
    assert _extract_header_company(text) is None


async def test_extract_header_company_ignores_short_abbrev_heading():
    """2 字简称标题（海丰装箱通知）无法可靠区分，保守不提取。"""
    text = "海丰装箱通知\n提单号：KMTCSHAP950393\n"
    assert _extract_header_company(text) is None


async def test_extract_header_company_skips_label_rows_with_company_token():
    """表格标签行的值即使含公司特征词（装箱工厂/发货方）也不误取为客户。"""
    text = (
        "| 1 | 做箱通知 |  |\n"
        "| 12 | 装箱工厂 | 喜临门物流有限公司 |\n"
        "| 2 | 发货方 | 宁波俊泰贸易有限公司 |\n"
        "| 3 | 收货人 | 宁波俊泰贸易有限公司 |\n"
    )
    assert _extract_header_company(text) is None


async def test_extract_header_company_prefers_customer_label():
    """明确客户栏（表格形式）位于顶部时优先作为客户来源。"""
    text = (
        "| 1 | 客户 | 俊泰物流有限公司 |\n"
        "| 2 | 浙江经茂国际货运代理有限公司 |  |\n"
    )
    assert _extract_header_company(text) == "俊泰物流有限公司"
    text = "客户简称：特格威\n提单号：ABC1234567\n"
    assert _extract_header_company(text) == "特格威"


async def test_extract_header_company_keeps_company_with_goods_token():
    """含“货物”子串的货代公司名（长文本）不再被标签词拦截，可作抬头客户来源。"""
    text = "上海威世国际货物运输代理有限公司\n门点装箱通知\nTO: 北跃物流\n"
    assert _extract_header_company(text) == "上海威世国际货物运输代理有限公司"


async def test_extract_header_company_handles_none():
    assert _extract_header_company(None) is None
    assert _extract_header_company("") is None


async def test_parse_document_to_order_extracts_header_company_when_llm_empty(monkeypatch):
    """LLM 未提取客户时，从文档顶部区域主动补全 c_title。"""

    def fake_convert(file_bytes, filename):
        markdown = (
            "江苏倍联现代物流有限公司\n"
            "常州赛格威做箱通知\n"
            "TO：俊泰\n"
            "提单号：KMTCSHAP950393\n"
        )
        return markdown, "pdf", {"parser": "pdfplumber"}, ("markdown:" + markdown)

    async def fake_chat_json(messages, **_kwargs):
        return {**COMPLETE_RAW, "c_title": None}, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "_convert_file", fake_convert)
    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-pdf", "order.pdf")
    assert result["extracted"]["c_title"] == "江苏倍联现代物流有限公司"
    assert result["missing_fields"] == []


async def test_revise_c_title_handles_none():
    assert _revise_c_title_to_value(None, _NOTICE_TEXT) is None
    assert _revise_c_title_to_value("俊泰", None) == "俊泰"
    assert _revise_c_title_to_value(None, None) is None


async def test_extract_company_name_strips_contact():
    assert _extract_company_name("江苏倍联 陈俐玲") == "江苏倍联"
    assert _extract_company_name("海丰") == "海丰"
    assert _extract_company_name("CMA CGM") == "CMA CGM"
    assert _extract_company_name("江苏倍联现代物流有限公司") == "江苏倍联现代物流有限公司"


async def test_prompt_c_title_only_from_fm_from():
    """prompt 必须：客户取文档抬头或 FM/FROM 后的值，并禁止 TO/ATTN。"""
    prompt = document_module._SYSTEM_PROMPT
    assert "抬头" in prompt
    assert "FROM" in prompt
    assert "FM" in prompt
    assert "TO:" in prompt
    assert "禁止" in prompt
    assert "收件/通知对象" in prompt
    assert "对账时请注明" not in prompt


async def test_prompt_requires_all_data_rows():
    """prompt 必须要求列出每一个数据行，禁止只取第一行。"""
    prompt = document_module._SYSTEM_PROMPT
    assert "每一个数据行" in prompt
    assert "禁止只取第一行" in prompt
    assert "data[0]" in prompt


async def test_parse_document_to_order_keeps_multi_row_data(monkeypatch):
    """e2e：mock LLM 输出多行 data，order_data 完整保留。"""
    monkeypatch.setattr(document_module, "_convert_file", _fake_convert)

    async def fake_chat_json(messages, **_kwargs):
        return {
            **COMPLETE_RAW,
            "data": MULTI_ROW_DATA,
            "packages": "680",
            "gross_weight": "8602",
            "volume": "33.92",
        }, {"model": "fake", "usage": None}

    monkeypatch.setattr(document_module, "achat_json", fake_chat_json)

    result = await parse_document_to_order_async(b"fake-pdf", "order.pdf")

    assert result["extracted"]["data"] == [
        {"b_order_num": "OOLU2120860080", "j": "680", "m": "8602", "t": "33.92", "hh": None, "mt": None},
        {"b_order_num": "OOLU2120860081", "j": "450", "m": "5036", "t": "22.18", "hh": None, "mt": None},
        {"b_order_num": "OOLU2120860082", "j": "10", "m": "106", "t": "0.66", "hh": None, "mt": None},
    ]
    assert result["order_data"]["data"] == result["extracted"]["data"]
    assert result["missing_fields"] == []
