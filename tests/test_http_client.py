"""http_client 纯函数测试：unpack_json 解析清洗与 pretty_json 美化截断。

HTTP 封装日志语义（同步版已退役）由 tests/test_async_clients.py 的 async 版覆盖。"""

from __future__ import annotations

from app.orders.http_client import pretty_json, unpack_json


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
