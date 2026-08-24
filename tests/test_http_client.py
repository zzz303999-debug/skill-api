"""http_client 统一第三方日志封装测试：请求/响应/异常日志、截断、敏感头不落日志。"""

from __future__ import annotations

import httpx
import pytest

from app.orders.http_client import post_form, post_json, pretty_json, unpack_json


class FakeResponse:
    """下游响应替身：status_code/text（可缺 text，对齐既有测试的最小替身）。"""

    def __init__(self, text: str = "", status_code: int = 200):
        self.status_code = status_code
        self.text = text


def _records(caplog, event: str) -> list[dict]:
    """按事件名取日志记录：extra 字段已由 makeRecord setattr 到 record 上。"""
    return [
        record.__dict__
        for record in caplog.records
        if record.getMessage() == event
    ]


def test_post_json_logs_request_and_response(caplog, monkeypatch):
    captured = {}

    def fake_post(url, **_kwargs):
        captured.update(_kwargs)
        return FakeResponse(text='{"code": "200"}', status_code=200)

    monkeypatch.setattr(httpx, "post", fake_post)
    with caplog.at_level("INFO"):
        response = post_json(
            "https://downstream.test/api",
            {"data": {"order_num1": "B123"}},
            name="publishCreateOrder",
            timeout=30,
        )

    assert response.status_code == 200
    requests = _records(caplog, "third_party_request")
    responses = _records(caplog, "third_party_response")
    assert len(requests) == 1 and len(responses) == 1
    req = requests[0]
    assert req["endpoint"] == "publishCreateOrder"
    assert req["method"] == "POST"
    assert req["url"] == "https://downstream.test/api"
    assert req["payload_kind"] == "json"
    assert '"order_num1": "B123"' in req["body_preview"]
    resp = responses[0]
    assert resp["status_code"] == 200
    assert resp["duration_ms"] >= 0
    assert "code" in resp["body_preview"]
    # 请求参数原样透传（与 httpx.post 语义一致，既有测试 monkeypatch 兼容）
    assert captured == {"json": {"data": {"order_num1": "B123"}}, "timeout": 30}


def test_post_form_logs_payload_kind_and_headers(caplog, monkeypatch):
    captured = {}

    def fake_post(url, **_kwargs):
        captured.update(_kwargs)
        return FakeResponse(text="ok")

    monkeypatch.setattr(httpx, "post", fake_post)
    with caplog.at_level("INFO"):
        post_form(
            "https://downstream.test/form",
            {"b_order_num": "B123"},
            name="AddWork",
            headers={"sk": "sk-secret"},
            timeout=30,
        )

    req = _records(caplog, "third_party_request")[0]
    assert req["payload_kind"] == "form"
    assert "b_order_num" in req["body_preview"]
    # sk 等凭据值不落日志（仅透传请求头）
    assert captured["headers"]["sk"] == "sk-secret"
    assert "sk-secret" not in caplog.text


def test_network_error_logged_and_re_raised(caplog, monkeypatch):
    def fake_post(*_args, **_kwargs):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(httpx, "post", fake_post)
    with caplog.at_level("INFO"):
        with pytest.raises(httpx.TimeoutException):
            post_json("https://downstream.test/api", {}, name="addBill", timeout=30)

    errors = _records(caplog, "third_party_request_error")
    assert len(errors) == 1
    assert errors[0]["endpoint"] == "addBill"
    assert errors[0]["error_type"] == "TimeoutException"
    assert errors[0]["duration_ms"] >= 0


def test_long_response_body_truncated(caplog, monkeypatch):
    big = '{"data": "' + "x" * 5000 + '"}'

    def fake_post(*_args, **_kwargs):
        return FakeResponse(text=big)

    monkeypatch.setattr(httpx, "post", fake_post)
    with caplog.at_level("INFO"):
        post_json("https://downstream.test/api", {}, name="addBill", timeout=30)

    resp = _records(caplog, "third_party_response")[0]
    assert len(resp["body_preview"]) == 2000


def test_non_json_payload_falls_back_to_str(caplog, monkeypatch):
    def fake_post(*_args, **_kwargs):
        return FakeResponse(text="ok")

    monkeypatch.setattr(httpx, "post", fake_post)
    with caplog.at_level("INFO"):
        # object() 不可 JSON 序列化 → _preview 退化 str()，不抛
        post_json("https://downstream.test/api", object(), name="x", timeout=30)

    req = _records(caplog, "third_party_request")[0]
    assert isinstance(req["body_preview"], str)


# ---------- pretty_json：嵌套 JSON 字符串展开 ----------


def test_unpack_json_returns_object():
    """upstream_response 使用场景：返回结构化对象，嵌套 JSON 字符串已展开。"""
    raw = {
        "code": 204,
        "msg": "未匹配到客户和装柜地址",
        "parmasData": '{"order_num1": "I237127119", "box": [{"b_type": "40HC", "box_num": 1}]}',
    }
    assert unpack_json(raw) == {
        "code": 204,
        "msg": "未匹配到客户和装柜地址",
        "parmasData": {
            "order_num1": "I237127119",
            "box": [{"b_type": "40HC", "box_num": 1}],
        },
    }
    assert isinstance(unpack_json(raw)["parmasData"], dict)


def test_unpack_json_non_json_string_left_alone():
    """不可解析的字符串原样保留（不炸、不篡改）。"""
    raw = {"note": "{不是JSON"}
    assert unpack_json(raw) == {"note": "{不是JSON"}


def test_unpack_json_nan_in_nested_string_to_none():
    """嵌套 JSON 字符串中的 NaN/Infinity → None（防 JSONResponse 序列化 500）。"""
    raw = {"parmasData": '{"v": NaN, "w": Infinity}'}
    assert unpack_json(raw) == {"parmasData": {"v": None, "w": None}}


def test_unpack_json_top_level_nan_to_none():
    """顶层 NaN/Infinity（裸 response.json 无 parse_constant 的路径）→ None。"""
    raw = {"code": 204, "data": {"v": float("nan"), "w": float("inf")}}
    assert unpack_json(raw) == {"code": 204, "data": {"v": None, "w": None}}


def test_unpack_json_truncates_long_string_values():
    """展开后的字符串值限长 2000（对象进入错误响应/日志时有界）。"""
    raw = {"parmasData": '{"x": "' + "y" * 5000 + '"}'}
    result = unpack_json(raw)
    assert len(result["parmasData"]["x"]) == 2000
    # 非 JSON 长文本同样截断
    assert len(unpack_json({"note": "z" * 3000})["note"]) == 2000


def test_pretty_json_unpacks_nested_json_string():
    """下游响应中整段 JSON 被序列化成字符串（如 parmasData）→ 展开为多行。"""
    raw = {
        "code": 204,
        "msg": "未匹配到客户和装柜地址",
        "parmasData": '{"order_num1": "I237127119", "box": [{"b_type": "40HC", "box_num": 1}], "driver": [{"b_date_time_start": "8点到厂"}]}',
    }
    text = pretty_json(raw)
    # 不再是一行转义文本：原串的 \"order_num1\" 转义形态已消失
    assert '{\\"order_num1\\"' not in text
    # 展开为多行 JSON（键/值各自成行，嵌套 box 数组也展开）
    assert '"order_num1": "I237127119"' in text
    assert '"b_type": "40HC"' in text and '"box_num": 1' in text
    assert "未匹配到客户和装柜地址" in text


def test_pretty_json_recursively_unpacks_double_nested():
    """字符串内再套 JSON 字符串（双层）也能展开。"""
    raw = {"outer": '{"inner": "[1, 2, 3]"}'}
    text = pretty_json(raw)
    assert '{"inner": "[1, 2, 3]"}' not in text
    assert "1," in text and "2," in text and "3" in text


def test_pretty_json_keeps_non_json_strings():
    raw = {"msg": "添加成功", "data": []}
    assert pretty_json(raw) == '{\n  "msg": "添加成功",\n  "data": []\n}'


def test_pretty_json_unparseable_string_left_alone():
    """以 { 开头但不是 JSON 的文本（如 OCR 噪声）→ 原样保留不炸。"""
    raw = {"note": "{不是JSON"}
    assert pretty_json(raw) == '{\n  "note": "{不是JSON"\n}'


def test_pretty_json_truncates_long_output():
    raw = {"parmasData": '{"x": "' + "y" * 5000 + '"}'}
    text = pretty_json(raw, max_chars=100)
    assert len(text) == 100
