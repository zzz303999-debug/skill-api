from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

import app.orders.http_client as http_client_module
from app.config import settings
from app.main import app
from app.orders.client import publish_create_order_async
from app.orders.extractor import (
    _split_loading_value,
    _split_vessel_voyage,
    extract_order_text,
)
from app.orders.mapper import OrderNotReadyError, build_order_data
from app.orders.schema import OrderTextExtraction

pytestmark = pytest.mark.asyncio

client = TestClient(app)

SAMPLE_TEXT = (
    "门点地址：浙江省嘉兴市嘉善县姚庄镇利群路269号；"
    "做箱时间：7月20日；"
    "船名航次：ZHONG GU YING KOU V.2605N；"
    "提单号：KMTCSHAP950393；"
    "箱型箱量：1*20RF；"
    "托运人/公司名称：海丰；"
    "目的港：BUSAN；"
    "中转港代码：KRPUS；"
    "开港时间/开航时间：2026-07-26"
)


def _configure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "order_api_url", "https://orders.example/create")


async def test_sample_text_is_parsed_verbatim():
    extracted, meta = extract_order_text(SAMPLE_TEXT)
    order_data = build_order_data(extracted)

    assert meta == {"extractor": "explicit_labels", "value_mode": "verbatim"}
    assert order_data["order_num1"] == "KMTCSHAP950393"
    assert order_data["c_title"] == "海丰"
    assert order_data["factory_bei"] == "浙江省嘉兴市嘉善县姚庄镇利群路269号"
    assert "factory_name" not in order_data
    assert order_data["b_ship_name"] == "ZHONG GU YING KOU"
    assert order_data["b_ship_num"] == "2605N"
    assert order_data["box"] == [{"b_type": "20RF", "box_num": 1}]
    # 做箱时间缺年份：按当前年份推断归一为 YYYY-MM-DD
    assert order_data["driver"] == [{"b_date": "2026-07-20"}]
    assert order_data["b_end_dock"] == "BUSAN"
    assert order_data["b_end_port"] == "KRPUS"
    assert order_data["b_open_ship_time"] == "2026-07-26"
    assert order_data["data"] == [{"b_order_num": "KMTCSHAP950393"}]


@pytest.mark.parametrize(
    "value,expected_name,expected_num",
    [
        # VOY 前缀（既有形态，回归）
        ("ZHONG GU YING KOU V.2605N", "ZHONG GU YING KOU", "2605N"),
        # 斜杠分隔（既有形态，回归）
        ("MSC CRAPOLLA/QB633W", "MSC CRAPOLLA", "QB633W"),
        ("MAERSK SARNIA/752E", "MAERSK SARNIA", "752E"),
        # 末尾独立 token 兜底：数字开头航次
        ("MAERSK SARNIA 752E", "MAERSK SARNIA", "752E"),
        ("EVER GIVEN 2617N", "EVER GIVEN", "2617N"),
        # 末尾独立 token 兜底：MSC 式（字母+数字+字母）
        ("MSC CRAPOLLA QB633W", "MSC CRAPOLLA", "QB633W"),
        # 无航次形态：整串归船名，不误伤
        ("CMA CGM ALEXANDER VON HUMBOLDT", "CMA CGM ALEXANDER VON HUMBOLDT", None),
        ("MSC CRAPOLLA", "MSC CRAPOLLA", None),
    ],
)
async def test_split_vessel_voyage(value, expected_name, expected_num):
    name, num = _split_vessel_voyage(value)
    assert name == expected_name
    assert num == expected_num


async def test_extract_vessel_voyage_plain_space_separated():
    """生产案例：船名航次合写无 VOY 前缀/斜杠（MSC CRAPOLLA QB633W）→ 拆出航次。"""
    text = (
        "门点地址：江苏省昆山市巴城镇石牌东泰丰路68号；"
        "做箱时间：2026-08-26 09:00；"
        "船名航次：MSC CRAPOLLA QB633W；"
        "提单号：177FZEZES711236；"
        "箱型箱量：20GPx1；"
        "托运人/公司名称：上海集行供应链管理有限公司"
    )
    extracted, meta = extract_order_text(text)
    assert meta == {"extractor": "explicit_labels", "value_mode": "verbatim"}
    assert extracted.b_ship_name == "MSC CRAPOLLA"
    assert extracted.b_ship_num == "QB633W"
    order_data = build_order_data(extracted)
    assert order_data["b_ship_name"] == "MSC CRAPOLLA"
    assert order_data["b_ship_num"] == "QB633W"


@pytest.mark.parametrize(
    "value,expected_b_date,expected_time_start",
    [
        # 完整年月日（含中文写法）→ 归一为 YYYY-MM-DD
        ("2026-07-20", "2026-07-20", None),
        ("2026年7月20日", "2026-07-20", None),
        # 年月日 + 中文时间尾巴（月/日单数字）→ 拆日期 + 时间描述
        ("2026-08-2 8点到厂", "2026-08-02", "8点到厂"),
        ("2026-08-02 8点到厂", "2026-08-02", "8点到厂"),
        ("2026年8月2日 8点到厂", "2026-08-02", "8点到厂"),
        ("2026-8-2", "2026-08-02", None),
        # 数字粘连尾巴（OCR 噪声）→ 不产出脏 b_date（原文归时间描述）
        ("2026-08-2030", None, "2026-08-2030"),
        ("2026-08-0208:00", None, "2026-08-0208:00"),
        # 非法日历日期（格式合法但日历不合法）→ 不产出脏数据
        ("2026-13-40", None, None),
        ("2026-13-40 8点到厂", None, None),
        # 既有格式零回归
        ("2026-07-20 08:00", "2026-07-20", "08:00:00"),
        ("7月20日 8点", f"{date.today().year}-07-20", "8点"),
        # 缺年份中文日期 → 按当前年份补全（动态年份，避免跨年测试失败）
        ("7月20日", f"{date.today().year}-07-20", None),
        ("12月5日", f"{date.today().year}-12-05", None),
        # 时间描述 → b_date_time_start 原文
        ("早上8点", None, "早上8点"),
        ("9:00", None, "9:00"),
        ("下午2点", None, "下午2点"),
        # 无法识别 / 空
        ("周五", None, None),
        ("", None, None),
        ("  ", None, None),
    ],
)
async def test_split_loading_value(value, expected_b_date, expected_time_start):
    assert _split_loading_value(value) == (expected_b_date, expected_time_start)


async def test_split_loading_value_invalid_date_returns_none():
    """非法日历日期（如 13月40日）不产出脏数据。"""
    assert _split_loading_value("13月40日") == (None, None)


async def test_source_fields_preserve_unsupported_label_and_value():
    from app.orders.extractor import parse_source_fields

    fields = parse_source_fields("自定义字段：AbC-001 原样；提单号：KMTCSHAP950393")

    assert fields == {
        "自定义字段": "AbC-001 原样",
        "提单号": "KMTCSHAP950393",
    }


async def test_build_order_data_requires_bill_and_shipper():
    with pytest.raises(OrderNotReadyError) as caught:
        build_order_data(OrderTextExtraction())

    assert caught.value.details["missing_fields"] == ["order_num1", "c_title"]


@pytest.mark.parametrize(
    "box_value,expected",
    [
        # 带数量两种写法保持原语义
        ("1*20GP", [{"b_type": "20GP", "box_num": 1}]),
        ("2x40HQ", [{"b_type": "40HQ", "box_num": 2}]),
        ("40HQ*3", [{"b_type": "40HQ", "box_num": 3}]),
        # 仅箱型未写数量 → 默认箱量 1
        ("20GP", [{"b_type": "20GP", "box_num": 1}]),
        ("40HQ", [{"b_type": "40HQ", "box_num": 1}]),
        # 混写：带数量条目中的箱型不重复计入
        ("1*20GP+40HQ", [{"b_type": "20GP", "box_num": 1}, {"b_type": "40HQ", "box_num": 1}]),
        ("20GP+2*40HQ", [{"b_type": "20GP", "box_num": 1}, {"b_type": "40HQ", "box_num": 2}]),
        # 同箱型混写：位置不重叠的裸箱型按 1 计入并累加
        ("1*20GP+20GP", [{"b_type": "20GP", "box_num": 2}]),
        # 零数量条目视为无效并忽略（兜底不得复活）
        ("0*20GP", []),
        ("20GP*0", []),
        ("20GPx0", []),
        # 数字前缀/后缀不产生幽灵箱型（100GP 中的 00GP、20GP2 的粘连）
        ("100GP", []),
        ("20GP2", []),
    ],
)
async def test_parse_box_quantity_defaults_to_one(box_value, expected):
    extracted, _meta = extract_order_text(f"提单号：KMTCSHAP950393；箱型箱量：{box_value}")
    assert [item.model_dump() for item in extracted.box] == expected


async def test_create_from_text_extracts_then_publishes(monkeypatch):
    import app.main as main_module

    _configure(monkeypatch)
    captured = {}

    async def fake_publish(order_data, *, room_id, user_id):
        captured["order_data"] = order_data
        captured["room_id"] = room_id
        captured["user_id"] = user_id
        return {
            "code": "200",
            "msg": "添加成功",
            "data": [{"sn": "EX26040001", "sns": "EX26040001-1"}],
        }

    monkeypatch.setattr(main_module, "_publish_order", fake_publish)

    response = client.post(
        "/orders",
        json={"content": SAMPLE_TEXT, "roomId": "ewewdsdw121", "userId": "10"},
    )

    assert response.status_code == 200
    assert response.json()["roomId"] == "ewewdsdw121"
    assert response.json()["source_fields"]["做箱时间"] == "7月20日"
    assert response.json()["source_fields"]["船名航次"] == "ZHONG GU YING KOU V.2605N"
    assert captured["order_data"]["order_num1"] == "KMTCSHAP950393"
    assert captured["room_id"] == "ewewdsdw121"
    assert captured["user_id"] == "10"
    assert response.json()["meta"]["extractor"] == "explicit_labels"
    assert response.json()["upstream"]["data"][0]["sn"] == "EX26040001"


async def test_create_from_text_requires_content_and_room_id(monkeypatch):
    _configure(monkeypatch)

    response = client.post(
        "/orders",
        json={"text": SAMPLE_TEXT},
    )

    assert response.status_code == 422
    locations = {tuple(item["loc"]) for item in response.json()["detail"]}
    assert ("body", "content") in locations
    assert ("body", "roomId") in locations
    assert ("body", "userId") in locations
    assert ("body", "text") in locations


async def test_publish_create_order_sends_exact_documented_wrapper(monkeypatch):

    _configure(monkeypatch)
    monkeypatch.setattr(settings, "order_api_url", "https://orders.example/create")
    captured = {}

    class FakeResponse:
        is_success = True
        status_code = 200

        @staticmethod
        def json():
            return {
                "code": "200",
                "msg": "添加成功",
                "data": [{"sn": "EX26040001", "sns": "EX26040001-1"}],
            }

    async def fake_post(url, *, payload=None, name=None, timeout=None, headers=None, payload_kind=None):
        captured.update(url=url, json=payload, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(http_client_module, "_post_async", fake_post)
    extracted, _meta = extract_order_text(SAMPLE_TEXT)
    order_data = build_order_data(extracted)

    result = await publish_create_order_async(order_data, room_id="ewewdsdw121", user_id="10")

    assert captured == {
        "url": "https://orders.example/create",
        "json": {
            "data": order_data,
            "userId": "10",
            "roomId": "ewewdsdw121",
        },
        "timeout": settings.order_api_timeout_seconds,
    }
    assert result["code"] == "200"


async def test_publish_accepts_numeric_code_and_object_data(monkeypatch):

    _configure(monkeypatch)

    class FakeResponse:
        is_success = True
        status_code = 200

        @staticmethod
        def json():
            return {"code": 200, "msg": "添加成功", "data": {"sn": "EX26040001"}}

    async def _fake_post(*_args, **_kwargs):
        return FakeResponse()

    monkeypatch.setattr(http_client_module, "_post_async", _fake_post)

    result = await publish_create_order_async({"order_num1": "TEST-1"}, room_id="room-1", user_id="10")

    assert result == {"code": 200, "msg": "添加成功", "data": {"sn": "EX26040001"}}


async def test_publish_reports_non_json_response_details(monkeypatch):
    from app.orders.client import OrderUpstreamError

    _configure(monkeypatch)

    class FakeResponse:
        is_success = True
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "upstream maintenance"

        @staticmethod
        def json():
            raise ValueError("not JSON")

    async def _fake_post(*_args, **_kwargs):
        return FakeResponse()

    monkeypatch.setattr(http_client_module, "_post_async", _fake_post)

    with pytest.raises(OrderUpstreamError) as caught:
        await publish_create_order_async({"order_num1": "TEST-1"}, room_id="room-1", user_id="10")

    assert caught.value.details == {
        "status_code": 200,
        "content_type": "text/html",
        "body_preview": "upstream maintenance",
    }


async def test_publish_passes_through_full_upstream_error(monkeypatch):
    """下游拒绝时完整透传错误（code/msg/原始响应体），message 含下游提示。"""
    from app.orders.client import OrderUpstreamError

    _configure(monkeypatch)

    class FakeResponse:
        is_success = True
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"code": "204", "msg": "no: userId", "data": [{"sn": "EX26040001"}]}'

        @staticmethod
        def json():
            return {"code": "204", "msg": "no: userId", "data": [{"sn": "EX26040001"}]}

    async def _fake_post(*_args, **_kwargs):
        return FakeResponse()

    monkeypatch.setattr(http_client_module, "_post_async", _fake_post)

    with pytest.raises(OrderUpstreamError) as caught:
        await publish_create_order_async({"order_num1": "TEST-1"}, room_id="room-1", user_id="10")

    assert caught.value.http_status == 502
    assert caught.value.code == "order_upstream_error"
    assert "no: userId" in caught.value.message
    assert caught.value.details == {
        "upstream_code": "204",
        "upstream_message": "no: userId",
        # upstream_response 为结构化对象（嵌套 JSON 已展开，响应序列化时自然多行）
        "upstream_response": {
            "code": "204",
            "msg": "no: userId",
            "data": [{"sn": "EX26040001"}],
        },
    }


async def test_publish_reports_http_error_with_body(monkeypatch):
    """下游 HTTP 非 2xx 时带响应体预览。"""
    from app.orders.client import OrderUpstreamError

    _configure(monkeypatch)

    class FakeResponse:
        is_success = False
        status_code = 500
        headers = {}
        text = "internal server error"

        @staticmethod
        def json():
            raise ValueError("not JSON")

    async def _fake_post(*_args, **_kwargs):
        return FakeResponse()

    monkeypatch.setattr(http_client_module, "_post_async", _fake_post)

    with pytest.raises(OrderUpstreamError) as caught:
        await publish_create_order_async({"order_num1": "TEST-1"}, room_id="room-1", user_id="10")

    assert caught.value.details == {
        "status_code": 500,
        "upstream_response": "internal server error",
    }
