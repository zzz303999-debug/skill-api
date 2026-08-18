"""下游客户端测试：GetWebKey → login → 下单（httpx mock，零网络）。

覆盖：凭证正常/失败、AddWork 成功/204 失败/部分失败隔离、凭证失败 502 不逐单、
表单展平与 §5.3 示例逐键比对、不自动重试（超时调用次数 == 1）、
超时/网络异常 → 单失败 error 不中断整批；JSON 通道（add_order_json，默认）：
body=order_data 嵌套原样 + sk 头、成功取 sn、失败 error 透传、不自动重试；
create_orders 按 JXT_CREATE_CHANNEL 分发（json 默认 / form 降级）。
"""

from __future__ import annotations

import json
import threading
import time
from urllib.parse import urlencode

import httpx
import pytest

import app.orders.bill.client as client_module
from app.errors import SkillAPIError
from app.orders.bill import BillOrder, BoxGroup, CanonicalOrder
from app.orders.bill.client import (
    UpstreamError,
    add_order_json,
    add_work,
    build_add_work_form,
    create_canonical_orders,
    create_orders,
    flatten_order,
)
from helpers import FakeResponse

# 测试用下游地址（避开真实域名，断言 url 传参）
TEST_URLS = {
    "getwebkey": "http://jxt.test/Api/Account/GetWebKey",
    "login": "http://jxt.test/Api/login",
    "addwork": "http://jxt.test/Car/WorkOut/AddWork",
    "order_api": "http://jxt.test/Car/publishCreateOrder",
}

# §5.3 示例订单（含应收费用 shou 中文键、box 累加、driver 合计）
ORDER_DATA = {
    "order_num1": "OOLU4044379500",
    "type": 1,
    "c_title": "湖州华凯",
    "c_sn": "17120321",
    "c_note": "客户编号：SIHK170619；箱号：OOLU7929509",
    "factory_name": "安吉洁美",
    "factory_bei": "安吉",
    "month": "2018-01",
    "data": [{"b_order_num": "OOLU4044379500"}, {"b_order_num": "OOLU4044379500"}],
    "box": [{"b_type": "40GP", "box_num": 6}],
    "driver": [
        {
            "b_date": "2018-01-27",
            "b_get_address": "洋山港三期",
            "b_back_address": "安吉",
            "get_ys_zj": 2500.0,
            "pay_yf_zj": 0.0,
        }
    ],
    "shou": [{"运费": {"money": 2100.0}}, {"其它费": {"money": 400.0}}],
}

# §2.2 实测（2026-08-12）+ 抓包（2026-08-11）展平基准：超集键集 + 值填充——
# 顶层 25 键、data[N] 7 子键、driver[0] 14 键全部恒发（无值空串）；shou 单条目
# 多费用名（全挂 shou[0]，每费用名 6 属性键）；note 下沉到全部嵌套条目；
# multiple_tare 与 duo_get/cost 合计键恒发。
EXPECTED_FLAT = {
    # 固定值字段（含抓包：multiple_tare、双拖/成本合计 0.00）
    "appendCost": "true",
    "o_id": "",
    "img_data": "[]",
    "img_data_id": "[]",
    "multiple_tare": "[]",
    "duo_get[0][duo_get_hj_zj]": "0.00",
    "cost[0][supplier_hj_zj]": "0.00",
    # 订单（type 固定 1；c_id/note 本期恒发空串）
    "order_num1": "OOLU4044379500",
    "type": "1",
    "c_title": "湖州华凯",
    "c_name": "",
    "c_phone": "",
    "c_sn": "17120321",
    "c_note": "客户编号：SIHK170619；箱号：OOLU7929509",
    "c_id": "",
    "month": "2018-01",
    "note": "",
    # 运输（本期无数据源 → 全空串）
    "b_ship_name": "",
    "b_ship_num": "",
    "b_ship_company": "",
    "b_start_dock": "",
    "b_end_port": "",
    "b_end_dock": "",
    "b_wharf": "",
    "b_open_ship_time": "",
    "b_close_ship_time": "",
    "b_operator": "",
    # 门点
    "factory_name": "安吉洁美",
    "factory_bei": "安吉",
    "factory_id": "",
    "b_factory_not": "",
    "b_tare": "",
    # 明细（data 7 子键：b_order_num 实际值，j/m/t/hh/mt/note 恒空）
    "data[0][b_order_num]": "OOLU4044379500",
    "data[0][j]": "",
    "data[0][m]": "",
    "data[0][t]": "",
    "data[0][hh]": "",
    "data[0][mt]": "",
    "data[0][note]": "",
    "data[1][b_order_num]": "OOLU4044379500",
    "data[1][j]": "",
    "data[1][m]": "",
    "data[1][t]": "",
    "data[1][hh]": "",
    "data[1][mt]": "",
    "data[1][note]": "",
    "box[0][b_type]": "40GP",
    "box[0][box_num]": "6",
    "box[0][note]": "",
    # driver[0] 固定 14 键（无值空串）
    "driver[0][d_id]": "",
    "driver[0][b_date]": "2018-01-27",
    "driver[0][b_date_time_start]": "",
    "driver[0][b_get_address]": "洋山港三期",
    "driver[0][b_back_address]": "安吉",
    "driver[0][d_name]": "",
    "driver[0][d_num]": "",
    "driver[0][d_phone]": "",
    "driver[0][distance]": "",
    "driver[0][you_hao]": "",
    "driver[0][driver_note]": "",
    "driver[0][get_ys_zj]": "2500.0",
    "driver[0][pay_yf_zj]": "0.0",
    "driver[0][note]": "",
    # 应收费用（抓包形态：单条目多费用名，全挂 shou[0]；money 实际金额，
    # price_id/price_type/is_profit/dai_dian/note 恒空）
    "shou[0][运费][money]": "2100.0",
    "shou[0][运费][price_id]": "",
    "shou[0][运费][price_type]": "",
    "shou[0][运费][is_profit]": "",
    "shou[0][运费][dai_dian]": "",
    "shou[0][运费][note]": "",
    "shou[0][其它费][money]": "400.0",
    "shou[0][其它费][price_id]": "",
    "shou[0][其它费][price_type]": "",
    "shou[0][其它费][is_profit]": "",
    "shou[0][其它费][dai_dian]": "",
    "shou[0][其它费][note]": "",
}


def make_order(order_data: dict | None = None) -> BillOrder:
    return BillOrder(
        order_num1=(order_data or ORDER_DATA).get("order_num1"),
        c_title=(order_data or ORDER_DATA).get("c_title"),
        order_data=order_data if order_data is not None else dict(ORDER_DATA),
    )


@pytest.fixture()
def urls(monkeypatch):
    """把四个下游 URL 指到测试域名（下单地址与 /orders 同源 order_api_url）。"""
    monkeypatch.setattr(client_module.settings, "jxt_getwebkey_url", TEST_URLS["getwebkey"])
    monkeypatch.setattr(client_module.settings, "jxt_login_url", TEST_URLS["login"])
    monkeypatch.setattr(client_module.settings, "jxt_addwork_url", TEST_URLS["addwork"])
    monkeypatch.setattr(client_module.settings, "order_api_url", TEST_URLS["order_api"])


class TestFlatten:
    """表单展平（纯函数，零 mock）：§5.3 示例逐键比对。"""

    def test_section53_example_keys(self):
        flat = flatten_order(ORDER_DATA)
        assert flat == EXPECTED_FLAT

    def test_fixed_fields_and_excluded_keys(self):
        """固定值字段齐备（含抓包键）；不发送字段（§2.2/§5.3 清单）不出现。"""
        flat = flatten_order(ORDER_DATA)
        for key in (
            "user_name",
            "car_name",
            "section_name",
            "box_type_text",
            "box_type",
            "b_date",
            "b_date_pick",
            # 本期不发送应付/双拖/成本费用条目与状态类键（只发合计键）
            "pay[0]",
            "duo_get[0][duo_get_hj_zj]",  # 注意：合计键应存在，费用条目才不发
            "cost[0][supplier_hj_zj]",
            "audit_status",
            "b_lock",
        ):
            if key in ("duo_get[0][duo_get_hj_zj]", "cost[0][supplier_hj_zj]"):
                continue  # 合计键恒发（见下方断言），此处仅验证费用条目键不发送
            assert key not in flat, f"不应发送 {key}"
        assert flat["type"] == "1"
        assert flat["appendCost"] == "true"
        assert flat["o_id"] == ""
        assert flat["img_data"] == "[]"
        assert flat["img_data_id"] == "[]"
        assert flat["multiple_tare"] == "[]"
        assert flat["duo_get[0][duo_get_hj_zj]"] == "0.00"
        assert flat["cost[0][supplier_hj_zj]"] == "0.00"

    def test_null_to_empty_string_fixed_keys_always_sent(self):
        """超集键集：必填项缺失（null）→ 空字符串；缺失键也发送（空串）；
        空 box 不产生键；c_id/note 恒发空串（2026-08-12 实测缺键即拒单）。"""
        order_data = {
            "order_num1": None,
            "type": 1,
            "c_title": "某客户",
            "data": [{"b_order_num": None}],
            "box": [],
            "driver": [{"pay_yf_zj": 0.0}],
        }
        flat = flatten_order(order_data)
        assert flat["order_num1"] == ""
        assert flat["data[0][b_order_num]"] == ""
        assert "box" not in flat and "box[0][b_type]" not in flat
        assert flat["driver[0][pay_yf_zj]"] == "0.0"
        assert flat["c_id"] == ""  # 恒发空串
        assert flat["note"] == ""  # 订单级备注，恒发空串
        # 超集键集：order_data 没有的键也发送（空串），不再跳过
        for key in ("c_sn", "factory_name", "factory_bei", "c_note", "month", "b_ship_name"):
            assert flat[key] == ""
        # data[N] 7 子键齐备（j/m/t/hh/mt/note 恒空）
        for key in ("j", "m", "t", "hh", "mt", "note"):
            assert flat[f"data[0][{key}]"] == ""
        # driver 14 固定键齐备
        for key in (
            "d_id",
            "b_date",
            "b_date_time_start",
            "b_get_address",
            "b_back_address",
            "d_name",
            "d_num",
            "d_phone",
            "distance",
            "you_hao",
            "driver_note",
            "get_ys_zj",
            "note",
        ):
            assert flat[f"driver[0][{key}]"] == ""

    def test_box_accumulated_and_shou_chinese_keys(self):
        """box 同型累加（box_num=2）；shou 单条目多费用名（抓包形态：全挂 shou[0]）。"""
        order_data = {
            "order_num1": "OOLU12345678",
            "type": 1,
            "c_title": "客户A",
            "data": [{"b_order_num": "OOLU12345678"}, {"b_order_num": "OOLU12345678"}],
            "box": [{"b_type": "40HQ", "box_num": 2}],
            "driver": [{"pay_yf_zj": 0.0}],
            "shou": [
                {"运费": {"money": 100.0}},
                {"待时费": {"money": 50.0}},
            ],
        }
        flat = flatten_order(order_data)
        assert flat["box[0][b_type]"] == "40HQ"
        assert flat["box[0][box_num]"] == "2"
        assert flat["shou[0][运费][money]"] == "100.0"
        assert flat["shou[0][待时费][money]"] == "50.0"

    def test_shou_attribute_keys(self):
        """shou[0] 单条目属性键：money 实际金额，price_id/price_type/is_profit/
        dai_dian/note 恒空（抓包 2026-08-11 属性清单）。"""
        order_data = {
            "order_num1": "OOLU12345678",
            "type": 1,
            "c_title": "客户A",
            "data": [{"b_order_num": "OOLU12345678"}],
            "box": [{"b_type": "40GP", "box_num": 1}],
            "driver": [{"pay_yf_zj": 0.0}],
            "shou": [{"运费": {"money": 100.0}}],
        }
        flat = flatten_order(order_data)
        assert flat["shou[0][运费][money]"] == "100.0"
        for key in ("price_id", "price_type", "is_profit", "dai_dian", "note"):
            assert flat[f"shou[0][运费][{key}]"] == "", f"shou 属性 {key} 应恒空"

    def test_driver_fixed_keys(self):
        """driver 固定键集：13 键全发（无值空串），未知键不发送。"""
        order_data = {
            "order_num1": "OOLU12345678",
            "type": 1,
            "c_title": "客户A",
            "data": [{"b_order_num": "OOLU12345678"}],
            "box": [{"b_type": "40GP", "box_num": 1}],
            "driver": [
                {
                    "pay_yf_zj": 0.0,
                    "d_name": "张三",
                    "d_phone": "13800000000",
                    "d_num": "沪A12345",
                    "b_date": "2018-01-27",
                    "secret_key": "不应发送",
                }
            ],
        }
        flat = flatten_order(order_data)
        assert flat["driver[0][d_name]"] == "张三"
        assert flat["driver[0][d_phone]"] == "13800000000"
        assert flat["driver[0][d_num]"] == "沪A12345"
        assert flat["driver[0][b_date]"] == "2018-01-27"
        assert flat["driver[0][pay_yf_zj]"] == "0.0"
        # 固定键集：未提供值的 driver 键也发送（空串）
        for key in ("d_id", "b_date_time_start", "distance", "you_hao", "driver_note"):
            assert flat[f"driver[0][{key}]"] == ""
        assert "driver[0][secret_key]" not in flat


class TestCredentials:
    """GetWebKey / login：正常取 token、失败抛 UpstreamError（502）。"""

    def test_get_web_key_ok(self, urls, monkeypatch):
        captured = {}

        def fake_post(url, *, json, timeout):
            captured.update(url=url, payload=json, timeout=timeout)
            return FakeResponse({"code": 200, "msg": "操作成功", "web_key": "wk-1347bdcc"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        assert client_module.get_web_key() == "wk-1347bdcc"
        assert captured["url"] == TEST_URLS["getwebkey"]
        assert captured["payload"] == {
            "ext_app_id": client_module.settings.jxt_ext_app_id,
            "ext_user_id": client_module.settings.jxt_ext_user_id,
            "jxt_open_id": client_module.settings.jxt_jxt_open_id,
            "order_info": [{}],
        }

    def test_get_web_key_business_failure(self, urls, monkeypatch):
        """code 非数字 200 → UpstreamError，details 含上游原文。"""
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse({"code": 500, "msg": "凭据无效"}),
        )
        with pytest.raises(UpstreamError) as caught:
            client_module.get_web_key()
        assert caught.value.http_status == 502
        assert caught.value.code == "order_upstream_error"
        assert caught.value.details["upstream_code"] == 500
        assert caught.value.details["upstream_message"] == "凭据无效"
        assert "凭据无效" in caught.value.details["upstream_response"]

    def test_get_web_key_network_error(self, urls, monkeypatch):
        def fake_post(*_a, **_k):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with pytest.raises(UpstreamError) as caught:
            client_module.get_web_key()
        assert caught.value.details["error_type"] == "ConnectError"

    def test_get_web_key_http_error(self, urls, monkeypatch):
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse({"code": 200}, status_code=500),
        )
        with pytest.raises(UpstreamError) as caught:
            client_module.get_web_key()
        assert caught.value.details["status_code"] == 500

    def test_get_web_key_non_json(self, urls, monkeypatch):
        class NonJson:
            status_code = 200
            text = "<html>gateway</html>"
            headers = {}

            def json(self, **kwargs):
                raise ValueError("not JSON")

        monkeypatch.setattr(client_module.httpx, "post", lambda *_a, **_k: NonJson())
        with pytest.raises(UpstreamError) as caught:
            client_module.get_web_key()
        assert "non-JSON" in caught.value.message

    def test_get_web_key_missing_field(self, urls, monkeypatch):
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse({"code": 200, "msg": "操作成功"}),
        )
        with pytest.raises(UpstreamError) as caught:
            client_module.get_web_key()
        assert "web_key" in caught.value.message

    def test_login_ok(self, urls, monkeypatch):
        """#2 实测结构（2026-08-12）：{"code":200,"data":{"token":...},"msg":...}。"""
        captured = {}

        def fake_post(url, *, json, timeout):
            captured.update(url=url, payload=json, timeout=timeout)
            return FakeResponse({"code": 200, "data": {"token": "tk-实测值"}, "msg": "操作成功"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        assert client_module.login("wk-1") == "tk-实测值"
        assert captured["url"] == TEST_URLS["login"]
        assert captured["payload"] == {"port": "tmsWebKey", "web_key": "wk-1"}

    def test_login_token_candidate_order(self, urls, monkeypatch):
        """候选顺序：data.token → sk → data.sk → token，取第一个非空（单点可调）。"""
        cases = [
            ({"code": 200, "data": {"token": "a"}, "sk": "b"}, "a"),  # data.token 优先
            ({"code": 200, "sk": "b"}, "b"),  # sk 兜底
            ({"code": 200, "data": {"sk": "c"}}, "c"),  # data.sk
            ({"code": 200, "token": "d"}, "d"),  # 顶层 token
            ({"code": 200, "data": {"token": "", "sk": "c"}, "sk": "b"}, "b"),  # 空值跳过→下一候选
        ]
        for payload, expected in cases:
            monkeypatch.setattr(
                client_module.httpx,
                "post",
                lambda *_a, payload=payload, **_k: FakeResponse(payload),
            )
            assert client_module.login("wk-1") == expected, f"payload={payload}"

    def test_login_business_failure(self, urls, monkeypatch):
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse({"code": 401, "msg": "web_key 过期"}),
        )
        with pytest.raises(UpstreamError) as caught:
            client_module.login("wk-bad")
        assert caught.value.details["upstream_code"] == 401
        assert caught.value.details["upstream_message"] == "web_key 过期"

    def test_login_missing_token_field(self, urls, monkeypatch):
        """所有候选路径都取不到 → 清晰报错，details 附响应键清单便于排查。"""
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse({"code": 200, "msg": "操作成功"}),
        )
        with pytest.raises(UpstreamError) as caught:
            client_module.login("wk-1")
        assert "no token field" in caught.value.message
        assert caught.value.details["response_keys"] == ["code", "msg"]


class TestAddWork:
    """AddWork：表单形态、成功取 sn、失败 error、不自动重试。"""

    def test_empty_value_keys_present_in_body(self):
        """防空值过滤回归：urlencode 后的最终 body 必须含 note=/c_id= 等空值键
        （2026-08-12 live 报 Undefined index: note，锁定为缺键而非空值）；
        note 下沉到全部嵌套条目（data/box/driver/shou）；抓包键 multiple_tare
        与 duo_get/cost 合计键恒发。"""
        form = build_add_work_form(ORDER_DATA)
        body = urlencode(form)
        for probe in (
            "note=",
            "c_id=",
            "b_operator=",
            "b_tare=",
            "driver%5B0%5D%5Bdistance%5D=",
            "multiple_tare=",
            "duo_get%5B0%5D%5Bduo_get_hj_zj%5D=",
            "cost%5B0%5D%5Bsupplier_hj_zj%5D=",
            # 嵌套 note 下沉键（live 实证：控制器读嵌套结构）
            "data%5B0%5D%5Bnote%5D=",
            "box%5B0%5D%5Bnote%5D=",
            "driver%5B0%5D%5Bnote%5D=",
            "shou%5B0%5D%5B%E8%BF%90%E8%B4%B9%5D%5Bnote%5D=",
            # shou 单条目属性键（抓包 2026-08-11）
            "shou%5B0%5D%5B%E8%BF%90%E8%B4%B9%5D%5Bprice_id%5D=",
            "shou%5B0%5D%5B%E8%BF%90%E8%B4%B9%5D%5Bdai_dian%5D=",
        ):
            assert probe in body, f"body 缺少空值键 {probe}"
        # b 参数 JSON 与顶层同内容，且含顶层与嵌套 note
        b_json = json.loads(form["b"])
        assert b_json == flatten_order(ORDER_DATA)
        assert b_json["note"] == ""
        assert b_json["data[0][note]"] == ""
        assert b_json["driver[0][note]"] == ""
        assert b_json["shou[0][运费][note]"] == ""
        assert b_json["multiple_tare"] == "[]"

    def test_form_shape_and_headers(self, urls, monkeypatch):
        """表单四组参数（a/c/b+顶层展平）、header sk、URL。"""
        captured = {}

        def fake_post(url, *, data, headers, timeout):
            captured.update(url=url, data=data, headers=headers, timeout=timeout)
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080042"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = add_work("sk-1", ORDER_DATA)

        assert captured["url"] == TEST_URLS["addwork"]
        assert captured["headers"]["sk"] == "sk-1"
        assert captured["data"]["a"] == "{}"
        assert captured["data"]["c"] == "{}"
        # b：URL 编码 JSON，键为展平写法、与顶层同内容
        assert json.loads(captured["data"]["b"]) == EXPECTED_FLAT
        # 顶层展平字段：与 b 的 JSON 同内容、逐键存在
        top = {k: v for k, v in captured["data"].items() if k not in ("a", "b", "c")}
        assert top == EXPECTED_FLAT
        assert result == {"success": True, "sn": "EX26080042", "error": None, "upstream": {"sn": "EX26080042"}}

    def test_success_sn_missing_ok(self, urls, monkeypatch):
        """code 200 但 data[0] 无 sn → 仍成功，sn=None。"""
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse(
                {"code": "200", "msg": "添加成功", "data": [{"o_id": 1}]}
            ),
        )
        assert add_work("sk", ORDER_DATA) == {"success": True, "sn": None, "error": None, "upstream": {"o_id": 1}}

    def test_rejected_204(self, urls, monkeypatch):
        """code '204'（字符串）→ 失败 error，含 upstream 三元组。"""
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse({"code": "204", "msg": "添加失败"}),
        )
        result = add_work("sk", ORDER_DATA)
        assert result["success"] is False and result["sn"] is None
        error = result["error"]
        assert error["code"] == "order_upstream_error"
        assert error["description"] == "订单系统拒绝了请求或不可达，请稍后重试"
        assert error["details"]["upstream_code"] == "204"
        assert error["details"]["upstream_message"] == "添加失败"
        assert "添加失败" in error["details"]["upstream_response"]

    def test_http_error_and_non_json(self, urls, monkeypatch):
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse({"code": "200"}, status_code=500),
        )
        result = add_work("sk", ORDER_DATA)
        assert result["success"] is False and result["error"]["details"]["status_code"] == 500

        class NonJson:
            status_code = 200
            text = "not json"
            headers = {}

            def json(self, **kwargs):
                raise ValueError("not JSON")

        monkeypatch.setattr(client_module.httpx, "post", lambda *_a, **_k: NonJson())
        result = add_work("sk", ORDER_DATA)
        assert result["success"] is False
        assert "non-JSON" in result["error"]["message"]

    def test_timeout_no_retry_single_call(self, urls, monkeypatch):
        """超时 → 该单 error（含异常摘要）；AddWork 调用次数 == 1（不自动重试）。"""
        calls = {"n": 0}

        def fake_post(*_a, **_k):
            calls["n"] += 1
            raise httpx.TimeoutException("timed out after 30s")

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = add_work("sk", ORDER_DATA)
        assert result["success"] is False
        assert result["error"]["details"]["error_type"] == "TimeoutException"
        assert calls["n"] == 1


class TestAddOrderJson:
    """JSON 通道（默认）：body=order_data 嵌套原样 + sk 头、成功取 sn、失败 error 透传、不重试。"""

    def test_body_structure_and_headers(self, urls, monkeypatch):
        """body = order_data 嵌套 JSON 原样（含 shou 费用），无 userId/roomId 包裹层；
        header sk；URL 与 /orders 同一下游地址（order_api_url）。"""
        captured = {}

        def fake_post(url, *, json, headers, timeout):
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080042"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = add_order_json("sk-1", ORDER_DATA)

        assert captured["url"] == TEST_URLS["order_api"]
        assert captured["headers"]["sk"] == "sk-1"
        assert captured["timeout"] == client_module.settings.jxt_timeout_seconds
        # 嵌套 JSON 原样（含 shou 费用），不带 roomId/userId、无 data 包裹层
        assert captured["json"] == ORDER_DATA
        assert captured["json"]["shou"] == [
            {"运费": {"money": 2100.0}},
            {"其它费": {"money": 400.0}},
        ]
        assert "userId" not in captured["json"]
        assert "roomId" not in captured["json"]
        assert {"order_num1", "type", "c_title", "data", "box", "driver"} <= set(captured["json"])
        assert result == {"success": True, "sn": "EX26080042", "error": None, "upstream": {"sn": "EX26080042"}}

    def test_success_sn_missing_ok(self, urls, monkeypatch):
        """code 200 但 data[0] 无 sn → 仍成功，sn=None。"""
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse(
                {"code": "200", "msg": "添加成功", "data": [{"o_id": 1}]}
            ),
        )
        assert add_order_json("sk", ORDER_DATA) == {"success": True, "sn": None, "error": None, "upstream": {"o_id": 1}}

    def test_rejected_error_passthrough(self, urls, monkeypatch):
        """code '204' → 失败 error，含 upstream 三元组（透传下游原文）。"""
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse({"code": "204", "msg": "添加失败"}),
        )
        result = add_order_json("sk", ORDER_DATA)
        assert result["success"] is False and result["sn"] is None
        error = result["error"]
        assert error["code"] == "order_upstream_error"
        assert error["description"] == "订单系统拒绝了请求或不可达，请稍后重试"
        assert error["details"]["upstream_code"] == "204"
        assert error["details"]["upstream_message"] == "添加失败"
        assert "添加失败" in error["details"]["upstream_response"]

    def test_http_error_and_non_json(self, urls, monkeypatch):
        monkeypatch.setattr(
            client_module.httpx,
            "post",
            lambda *_a, **_k: FakeResponse({"code": "200"}, status_code=500),
        )
        result = add_order_json("sk", ORDER_DATA)
        assert result["success"] is False and result["error"]["details"]["status_code"] == 500

        class NonJson:
            status_code = 200
            text = "not json"
            headers = {}

            def json(self, **kwargs):
                raise ValueError("not JSON")

        monkeypatch.setattr(client_module.httpx, "post", lambda *_a, **_k: NonJson())
        result = add_order_json("sk", ORDER_DATA)
        assert result["success"] is False
        assert "non-JSON" in result["error"]["message"]

    def test_timeout_no_retry_single_call(self, urls, monkeypatch):
        """超时 → 该单 error；调用次数 == 1（不自动重试，防重复下单）。"""
        calls = {"n": 0}

        def fake_post(*_a, **_k):
            calls["n"] += 1
            raise httpx.TimeoutException("timed out after 30s")

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = add_order_json("sk", ORDER_DATA)
        assert result["success"] is False
        assert result["error"]["details"]["error_type"] == "TimeoutException"
        assert calls["n"] == 1


class TestCreateOrders:
    """create_orders 编排（form 通道语义）：凭证一次、串行逐单、失败隔离、凭证失败不逐单。"""

    @pytest.fixture(autouse=True)
    def _form_channel(self, monkeypatch):
        """既有用例固定 form 通道（AddWork 降级链路），不随默认值变化漂移。"""
        monkeypatch.setattr(client_module.settings, "jxt_create_channel", "form")

    def _patch_chain(self, monkeypatch, addwork_payloads: list):
        """GetWebKey/login 固定成功；AddWork 按调用序返回 payload 列表。"""
        calls = {"addwork": 0, "all": 0}

        def fake_post(url, **kwargs):
            calls["all"] += 1
            if url == TEST_URLS["getwebkey"]:
                return FakeResponse({"code": 200, "msg": "操作成功", "web_key": "wk-1"})
            if url == TEST_URLS["login"]:
                return FakeResponse({"code": 200, "data": {"token": "sk-1"}, "msg": "操作成功"})
            payload = addwork_payloads[calls["addwork"]]
            calls["addwork"] += 1
            return FakeResponse(payload)

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        return calls

    def test_all_success(self, urls, monkeypatch):
        """两单全成功：GetWebKey/login 各一次 + 每单一 AddWork；create_result 原地填充。"""
        calls = self._patch_chain(
            monkeypatch,
            [
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080001"}]},
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080002"}]},
            ],
        )
        orders = [make_order(), make_order({**ORDER_DATA, "order_num1": "OOLU4044379501"})]
        create_orders(orders)
        assert calls["all"] == 4  # GetWebKey + login + 2×AddWork
        assert orders[0].create_result == {"success": True, "sn": "EX26080001", "error": None, "upstream": {"sn": "EX26080001"}}
        assert orders[1].create_result == {"success": True, "sn": "EX26080002", "error": None, "upstream": {"sn": "EX26080002"}}

    def test_partial_failure_isolated(self, urls, monkeypatch):
        """第一单 204 失败不影响第二单；两单均被调用。"""
        calls = self._patch_chain(
            monkeypatch,
            [
                {"code": "204", "msg": "添加失败"},
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080002"}]},
            ],
        )
        orders = [make_order(), make_order()]
        create_orders(orders)
        assert calls["addwork"] == 2
        assert orders[0].create_result["success"] is False
        assert orders[0].create_result["error"]["details"]["upstream_code"] == "204"
        assert orders[1].create_result == {"success": True, "sn": "EX26080002", "error": None, "upstream": {"sn": "EX26080002"}}

    def test_credential_failure_no_addwork(self, urls, monkeypatch):
        """GetWebKey 业务失败 → UpstreamError（502），AddWork 调用次数 0。"""
        calls = {"addwork": 0}

        def fake_post(url, **kwargs):
            if url == TEST_URLS["getwebkey"]:
                return FakeResponse({"code": 500, "msg": "凭据无效"})
            calls["addwork"] += 1
            return FakeResponse({"code": "200", "msg": "ok"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with pytest.raises(UpstreamError) as caught:
            create_orders([make_order(), make_order()])
        assert caught.value.http_status == 502
        assert caught.value.code == "order_upstream_error"
        assert calls["addwork"] == 0

    def test_login_failure_no_addwork(self, urls, monkeypatch):
        """login 失败 → UpstreamError，AddWork 调用次数 0（不逐单执行）。"""
        calls = {"addwork": 0}

        def fake_post(url, **kwargs):
            if url == TEST_URLS["getwebkey"]:
                return FakeResponse({"code": 200, "msg": "操作成功", "web_key": "wk-1"})
            if url == TEST_URLS["login"]:
                return FakeResponse({"code": 401, "msg": "web_key 过期"})
            calls["addwork"] += 1
            return FakeResponse({"code": "200", "msg": "ok"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with pytest.raises(UpstreamError):
            create_orders([make_order()])
        assert calls["addwork"] == 0

    def test_timeout_one_order_continues_next(self, urls, monkeypatch):
        """第一单超时 → error 不中断；第二单照常提交；各调用一次（不重试）。"""
        calls = {"n": 0}

        def fake_post(url, **kwargs):
            if url == TEST_URLS["getwebkey"]:
                return FakeResponse({"code": 200, "msg": "ok", "web_key": "wk"})
            if url == TEST_URLS["login"]:
                return FakeResponse({"code": 200, "data": {"token": "sk"}, "msg": "ok"})
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.TimeoutException("timed out")
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080002"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        orders = [make_order(), make_order()]
        create_orders(orders)
        assert calls["n"] == 2  # 每单一发，超时不重试
        assert orders[0].create_result["success"] is False
        assert orders[0].create_result["error"]["details"]["error_type"] == "TimeoutException"
        assert orders[1].create_result == {"success": True, "sn": "EX26080002", "error": None, "upstream": {"sn": "EX26080002"}}

    def test_missing_fields_still_submitted(self, urls, monkeypatch):
        """missing_fields 非空（如 box 缺失）→ 照常提交（本服务不拦截）。"""
        calls = {"addwork": 0}

        def fake_post(url, **kwargs):
            if url == TEST_URLS["getwebkey"]:
                return FakeResponse({"code": 200, "msg": "ok", "web_key": "wk"})
            if url == TEST_URLS["login"]:
                return FakeResponse({"code": 200, "data": {"token": "sk"}, "msg": "ok"})
            calls["addwork"] += 1
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        order = make_order(
            {
                "order_num1": None,
                "type": 1,
                "c_title": "客户A",
                "data": [{"b_order_num": None}],
                "box": [],
                "driver": [{"pay_yf_zj": 0.0}],
            }
        )
        order.missing_fields = ["order_num1", "box"]
        create_orders([order])
        assert calls["addwork"] == 1
        assert order.create_result["success"] is True

    def test_empty_orders_no_downstream_calls(self, urls, monkeypatch):
        """空订单列表：不取凭证、不调下游。"""
        calls = {"n": 0}

        def fake_post(*_a, **_k):
            calls["n"] += 1
            return FakeResponse({"code": "200"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        create_orders([])
        assert calls["n"] == 0

    def test_upstream_error_is_skill_api_error(self):
        """UpstreamError 走既有错误体系（SkillAPIError 子类）。"""
        assert issubclass(UpstreamError, SkillAPIError)
        err = UpstreamError("boom")
        assert err.http_status == 502
        assert err.code == "order_upstream_error"
        assert err.description == "订单系统拒绝了请求或不可达，请稍后重试"

    # ---- 重复上传去重（方案一：成功单注册表）----

    def test_dedup_second_upload_skipped(self, urls, monkeypatch):
        """成功单登记后重导：同提单号再次 create → skipped，下游 0 次新增调用。"""
        calls = self._patch_chain(
            monkeypatch,
            [{"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080001"}]}],
        )
        first = [make_order()]
        create_orders(first)
        assert first[0].create_result["success"] is True
        assert calls["addwork"] == 1
        # 再次上传同一文件（同提单号）：跳过且 sn 回显首次创建
        second = [make_order()]
        create_orders(second)
        assert calls["addwork"] == 1
        assert second[0].create_result == {
            "success": True,
            "skipped": True,
            "sn": "EX26080001",
            "error": None,
        }

    def test_dedup_failed_not_registered_retry_submits(self, urls, monkeypatch):
        """失败单不登记：修正后重导照常再次提交（不被误拦）。"""
        calls = self._patch_chain(
            monkeypatch,
            [
                {"code": "204", "msg": "添加失败"},
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080001"}]},
            ],
        )
        first = [make_order()]
        create_orders(first)
        assert first[0].create_result["success"] is False
        assert calls["addwork"] == 1
        second = [make_order()]
        create_orders(second)
        assert second[0].create_result["success"] is True
        assert calls["addwork"] == 2  # 失败单重导不受去重影响

    def test_dedup_no_bl_not_registered(self, urls, monkeypatch):
        """无提单号单：不查不登，照常提交（且不写入注册表）。"""
        calls = self._patch_chain(
            monkeypatch, [{"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]}]
        )
        order = make_order(
            {
                "order_num1": None,
                "type": 1,
                "c_title": "客户A",
                "data": [{"b_order_num": None}],
                "box": [],
                "driver": [{"pay_yf_zj": 0.0}],
            }
        )
        create_orders([order])
        assert order.create_result["success"] is True
        assert calls["addwork"] == 1
        assert client_module.get_imported_registry().snapshot() == {}

    def test_dedup_premarked_skipped_skips_downstream(self, urls, monkeypatch):
        """service 层预判已标记 skipped（create_result 非 None）→ 编排直接跳过。"""
        calls = self._patch_chain(monkeypatch, [{"code": "200", "msg": "ok"}])
        order = make_order()
        order.create_result = {"success": True, "skipped": True, "sn": "EX1", "error": None}
        create_orders([order])
        assert calls["addwork"] == 0

    def test_dedup_register_failure_keeps_success(self, urls, monkeypatch):
        """登记异常（磁盘满/权限）→ 仅日志不冒泡，响应仍成功（单已真实创建）。"""
        calls = self._patch_chain(
            monkeypatch, [{"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]}]
        )
        registry = client_module.get_imported_registry()

        def _fail(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(registry, "register", _fail)
        order = make_order()
        create_orders([order])
        assert order.create_result["success"] is True
        assert calls["addwork"] == 1

    def test_dedup_concurrent_same_bl_single_submit(self, urls, monkeypatch):
        """并发同提单号：两个请求同时 create → 下游恰好 1 次（per-bl_no 锁原子化）。"""
        calls = {"addwork": 0}
        gate = threading.Event()

        def fake_post(url, **kwargs):
            if url == TEST_URLS["getwebkey"]:
                return FakeResponse({"code": 200, "msg": "ok", "web_key": "wk"})
            if url == TEST_URLS["login"]:
                return FakeResponse({"code": 200, "data": {"token": "sk"}, "msg": "ok"})
            calls["addwork"] += 1
            gate.wait(timeout=5)  # 拉长临界区，放大竞态窗口
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        orders_box: list = []
        errors: list = []

        def worker():
            orders = [make_order()]
            orders_box.append(orders)
            try:
                create_orders(orders)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=worker)
        t1.start()
        while calls["addwork"] < 1:
            time.sleep(0.01)  # 等第一单进入 AddWork（持有 per-bl_no 锁）
        t2 = threading.Thread(target=worker)
        t2.start()
        time.sleep(0.1)
        gate.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not errors
        assert calls["addwork"] == 1  # 若无锁，第二请求会再次提交
        results = [o.create_result for box in orders_box for o in box]
        assert sum(1 for r in results if r["success"]) == 2  # 1 新建 + 1 skipped
        assert sum(1 for r in results if r.get("skipped")) == 1


class TestCreateCanonicalOrdersDedup:
    """create_canonical_orders 去重（TMS 通道，bl_no 键）。"""

    def _patch_chain(self, monkeypatch, payloads: list):
        calls = {"submit": 0}

        def fake_post(url, **kwargs):
            if "GetWebKey" in url:
                return FakeResponse({"code": 200, "msg": "ok", "web_key": "wk"})
            if "login" in url:
                return FakeResponse({"code": 200, "data": {"token": "sk"}, "msg": "ok"})
            payload = payloads[calls["submit"]]
            calls["submit"] += 1
            return FakeResponse(payload)

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        return calls

    def _make_canonical(self, bl_no: str) -> CanonicalOrder:
        return CanonicalOrder(
            bl_no=bl_no, box_groups=[BoxGroup(b_type="40HQ", box_num=1)]
        )

    def test_second_upload_skipped(self, urls, monkeypatch):
        """TMS 通道：成功单登记后重导 → skipped（下游 0 次新增调用）。"""
        calls = self._patch_chain(
            monkeypatch, [{"code": "200", "data": [{"sn": "EX1", "o_id": "2101"}]}]
        )
        first = [self._make_canonical("OOLU12345678")]
        create_canonical_orders(first)
        assert first[0].create_result["success"] is True
        assert calls["submit"] == 1
        second = [self._make_canonical("OOLU12345678")]
        create_canonical_orders(second)
        assert calls["submit"] == 1
        assert second[0].create_result == {
            "success": True,
            "skipped": True,
            "sn": "EX1",
            "error": None,
        }

    def test_failed_not_registered_retry_submits(self, urls, monkeypatch):
        """TMS 通道：失败单不登记，重导照常提交。"""
        calls = self._patch_chain(
            monkeypatch,
            [
                {"code": "204", "msg": "添加失败"},
                {"code": "200", "data": [{"sn": "EX2"}]},
            ],
        )
        first = [self._make_canonical("OOLU12345678")]
        create_canonical_orders(first)
        assert first[0].create_result["success"] is False
        assert calls["submit"] == 1
        second = [self._make_canonical("OOLU12345678")]
        create_canonical_orders(second)
        assert second[0].create_result["success"] is True
        assert calls["submit"] == 2

    def test_no_bl_submitted_not_registered(self, urls, monkeypatch):
        """TMS 通道：无提单号单照常提交，不查不登。"""
        calls = self._patch_chain(
            monkeypatch, [{"code": "200", "data": [{"sn": "EX1"}]}]
        )
        order = CanonicalOrder(bl_no=None, box_groups=[BoxGroup(b_type="40HQ", box_num=1)])
        create_canonical_orders([order])
        assert order.create_result["success"] is True
        assert calls["submit"] == 1
        assert client_module.get_imported_registry().snapshot() == {}


class TestCreateOrdersJson:
    """create_orders 默认 json 通道：下单走 order_api_url + sk 头 + 嵌套 body（无包裹层）。"""

    @pytest.fixture(autouse=True)
    def _json_channel(self, monkeypatch):
        """显式固定 json 通道（与默认值一致，防止默认值变更后语义漂移）。"""
        monkeypatch.setattr(client_module.settings, "jxt_create_channel", "json")

    def _patch_chain(self, monkeypatch, payloads: list):
        """GetWebKey/login 固定成功；下单按调用序返回 payload 列表。"""
        calls = {"submit": 0, "all": 0}

        def fake_post(url, **kwargs):
            calls["all"] += 1
            if url == TEST_URLS["getwebkey"]:
                return FakeResponse({"code": 200, "msg": "操作成功", "web_key": "wk-1"})
            if url == TEST_URLS["login"]:
                return FakeResponse({"code": 200, "data": {"token": "sk-1"}, "msg": "操作成功"})
            payload = payloads[calls["submit"]]
            calls["submit"] += 1
            return FakeResponse(payload)

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        return calls

    def test_all_success_json_channel(self, urls, monkeypatch):
        """两单全成功：GetWebKey/login 各一次 + 每单一 POST；create_result 原地填充。"""
        calls = self._patch_chain(
            monkeypatch,
            [
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080001"}]},
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080002"}]},
            ],
        )
        orders = [make_order(), make_order({**ORDER_DATA, "order_num1": "OOLU4044379501"})]
        create_orders(orders)
        assert calls["all"] == 4  # GetWebKey + login + 2×下单
        assert orders[0].create_result == {"success": True, "sn": "EX26080001", "error": None, "upstream": {"sn": "EX26080001"}}
        assert orders[1].create_result == {"success": True, "sn": "EX26080002", "error": None, "upstream": {"sn": "EX26080002"}}

    def test_json_body_no_wrapper(self, urls, monkeypatch):
        """json 通道请求：URL=order_api、header sk、body=order_data 嵌套原样（无 data 包裹层、
        无 userId/roomId）。"""
        captured = {}

        def fake_post(url, **kwargs):
            if url == TEST_URLS["getwebkey"]:
                return FakeResponse({"code": 200, "msg": "ok", "web_key": "wk"})
            if url == TEST_URLS["login"]:
                return FakeResponse({"code": 200, "data": {"token": "sk"}, "msg": "ok"})
            captured.update(url=url, kwargs=kwargs)
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        create_orders([make_order()])
        assert captured["url"] == TEST_URLS["order_api"]
        assert captured["kwargs"]["headers"]["sk"] == "sk"
        assert captured["kwargs"]["json"] == ORDER_DATA
        assert "userId" not in captured["kwargs"]["json"]
        assert "roomId" not in captured["kwargs"]["json"]

    def test_partial_failure_isolated_json(self, urls, monkeypatch):
        """第一单 204 失败不影响第二单（json 通道同样失败隔离）。"""
        calls = self._patch_chain(
            monkeypatch,
            [
                {"code": "204", "msg": "添加失败"},
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080002"}]},
            ],
        )
        orders = [make_order(), make_order()]
        create_orders(orders)
        assert calls["submit"] == 2
        assert orders[0].create_result["success"] is False
        assert orders[0].create_result["error"]["details"]["upstream_code"] == "204"
        assert orders[1].create_result == {"success": True, "sn": "EX26080002", "error": None, "upstream": {"sn": "EX26080002"}}
