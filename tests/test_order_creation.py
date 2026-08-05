from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.orders.client import publish_create_order
from app.orders.extractor import extract_order_text
from app.orders.mapper import OrderNotReadyError, build_order_data
from app.orders.schema import OrderTextExtraction

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
    monkeypatch.setattr(settings, "order_api_ext_app_id", "89")
    monkeypatch.setattr(settings, "order_api_ext_user_id", "jijuTms")
    monkeypatch.setattr(settings, "order_api_jxt_open_id", "open-id")
    monkeypatch.setattr(settings, "order_api_user_id", "dony")


def test_sample_text_is_parsed_verbatim():
    extracted, meta = extract_order_text(SAMPLE_TEXT)
    order_data = build_order_data(extracted, customer_id="open-id")

    assert meta == {"extractor": "explicit_labels", "value_mode": "verbatim"}
    assert order_data["order_num1"] == "KMTCSHAP950393"
    assert order_data["c_title"] == "海丰"
    assert order_data["factory_bei"] == "浙江省嘉兴市嘉善县姚庄镇利群路269号"
    assert "factory_name" not in order_data
    assert order_data["b_ship_name"] == "ZHONG GU YING KOU"
    assert order_data["b_ship_num"] == "2605N"
    assert order_data["box"] == [{"b_type": "20RF", "box_num": 1}]
    assert order_data["driver"] == [{"b_date": "7月20日"}]
    assert order_data["b_end_dock"] == "BUSAN"
    assert order_data["b_end_port"] == "KRPUS"
    assert order_data["b_open_ship_time"] == "2026-07-26"
    assert order_data["data"] == [{"b_order_num": "KMTCSHAP950393"}]


def test_source_fields_preserve_unsupported_label_and_value():
    from app.orders.extractor import parse_source_fields

    fields = parse_source_fields("自定义字段：AbC-001 原样；提单号：KMTCSHAP950393")

    assert fields == {
        "自定义字段": "AbC-001 原样",
        "提单号": "KMTCSHAP950393",
    }


def test_build_order_data_requires_bill_and_shipper():
    with pytest.raises(OrderNotReadyError) as caught:
        build_order_data(OrderTextExtraction())

    assert caught.value.details["missing_fields"] == ["order_num1", "c_title"]


def test_create_from_text_extracts_then_publishes(monkeypatch):
    import app.main as main_module

    _configure(monkeypatch)
    captured = {}

    async def fake_publish(order_data, *, room_id):
        captured["order_data"] = order_data
        captured["room_id"] = room_id
        return {
            "code": "200",
            "msg": "添加成功",
            "data": [{"sn": "EX26040001", "sns": "EX26040001-1"}],
        }

    monkeypatch.setattr(main_module, "_publish_order", fake_publish)

    response = client.post(
        "/orders",
        json={"content": SAMPLE_TEXT, "roomId": "ewewdsdw121"},
    )

    assert response.status_code == 200
    assert response.json()["roomId"] == "ewewdsdw121"
    assert response.json()["source_fields"]["做箱时间"] == "7月20日"
    assert response.json()["source_fields"]["船名航次"] == "ZHONG GU YING KOU V.2605N"
    assert captured["order_data"]["order_num1"] == "KMTCSHAP950393"
    assert captured["room_id"] == "ewewdsdw121"
    assert response.json()["meta"]["extractor"] == "explicit_labels"
    assert response.json()["upstream"]["data"][0]["sn"] == "EX26040001"


def test_create_from_text_requires_content_and_room_id(monkeypatch):
    _configure(monkeypatch)

    response = client.post(
        "/orders",
        json={"text": SAMPLE_TEXT},
    )

    assert response.status_code == 422
    locations = {tuple(item["loc"]) for item in response.json()["detail"]}
    assert ("body", "content") in locations
    assert ("body", "roomId") in locations
    assert ("body", "text") in locations


def test_publish_create_order_sends_exact_documented_wrapper(monkeypatch):
    import app.orders.client as client_module

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

    def fake_post(url, *, json, timeout):
        captured.update(url=url, json=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(client_module.httpx, "post", fake_post)
    extracted, _meta = extract_order_text(SAMPLE_TEXT)
    order_data = build_order_data(extracted, customer_id=settings.order_api_jxt_open_id)

    result = publish_create_order(order_data, room_id="ewewdsdw121")

    assert captured == {
        "url": "https://orders.example/create",
        "json": {
            "apiKeyInfo": {
                "ext_app_id": "89",
                "ext_user_id": "jijuTms",
                "jxt_open_id": "open-id",
            },
            "data": order_data,
            "userId": "dony",
            "roomId": "ewewdsdw121",
        },
        "timeout": settings.order_api_timeout_seconds,
    }
    assert result["code"] == "200"


def test_publish_accepts_numeric_code_and_object_data(monkeypatch):
    import app.orders.client as client_module

    _configure(monkeypatch)

    class FakeResponse:
        is_success = True
        status_code = 200

        @staticmethod
        def json():
            return {"code": 200, "msg": "添加成功", "data": {"sn": "EX26040001"}}

    monkeypatch.setattr(client_module.httpx, "post", lambda *_args, **_kwargs: FakeResponse())

    result = publish_create_order({"order_num1": "TEST-1"}, room_id="room-1")

    assert result == {"code": 200, "msg": "添加成功", "data": {"sn": "EX26040001"}}


def test_publish_reports_non_json_response_details(monkeypatch):
    import app.orders.client as client_module
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

    monkeypatch.setattr(client_module.httpx, "post", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(OrderUpstreamError) as caught:
        publish_create_order({"order_num1": "TEST-1"}, room_id="room-1")

    assert caught.value.details == {
        "status_code": 200,
        "content_type": "text/html",
        "body_preview": "upstream maintenance",
    }


def test_publish_passes_through_full_upstream_error(monkeypatch):
    """下游拒绝时完整透传错误（code/msg/原始响应体），message 含下游提示。"""
    import app.orders.client as client_module
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

    monkeypatch.setattr(client_module.httpx, "post", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(OrderUpstreamError) as caught:
        publish_create_order({"order_num1": "TEST-1"}, room_id="room-1")

    assert caught.value.http_status == 502
    assert caught.value.code == "order_upstream_error"
    assert "no: userId" in caught.value.message
    assert caught.value.details == {
        "upstream_code": "204",
        "upstream_message": "no: userId",
        "upstream_response": '{"code": "204", "msg": "no: userId", "data": [{"sn": "EX26040001"}]}',
    }


def test_publish_reports_http_error_with_body(monkeypatch):
    """下游 HTTP 非 2xx 时带响应体预览。"""
    import app.orders.client as client_module
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

    monkeypatch.setattr(client_module.httpx, "post", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(OrderUpstreamError) as caught:
        publish_create_order({"order_num1": "TEST-1"}, room_id="room-1")

    assert caught.value.details == {
        "status_code": 500,
        "upstream_response": "internal server error",
    }
