"""T7 payload 测试：build_order_form 快照（对照《逆推规范》§4 字段名）+ b 双写。

覆盖：必填字段落点（提单号/箱型）、多箱型展开、多箱号首箱 + warning、
order_date→month 派生、biz_no/biz_type 拼入 b_note、type 枚举默认 1、
client 侧响应判定（code 字符串 "200"）与 create_order=true 提交。
"""

from __future__ import annotations

import pytest

import app.orders.bill.client as client_module
import app.orders.http_client as http_client_module
from app.orders.bill.client import create_canonical_orders_async
from app.orders.bill.payload import build_order_form, build_order_payload
from app.orders.bill.schema import BoxGroup, CanonicalOrder, ContainerInfo
from helpers import FakeResponse

pytestmark = pytest.mark.asyncio




def _sample_order(**overrides) -> CanonicalOrder:
    """标准订单样例：一票两箱（同箱型两箱号）+ 全字段。"""
    data = dict(
        bl_no="OOLU4044379500",
        box_groups=[BoxGroup(b_type="40HQ", box_num=2), BoxGroup(b_type="20GP", box_num=1)],
        customer_name="德清华凯",
        customer_contact="张经理",
        customer_no="SED161509",
        door_point="德清和普",
        work_date="2016-07-26",
        pickup_point="提箱点A",
        return_point="还箱点B",
        vessel="MAERSK SARNIA",
        voyage="752E",
        port_area="外二期",
        pieces=10,
        gross_weight=12.5,
        cargo_name="化工品",
        biz_no="16070002-1",
        biz_type="出口",
        order_date="2016-07-26",
        remark="备注原文",
        containers=[
            ContainerInfo(container_no="TCLU1234567", box_type="40HQ", seal_no="SN888"),
            ContainerInfo(container_no="TCLU7654321", box_type="40HQ"),
        ],
        source_template="qiuyi_v1",
        row_count=2,
    )
    data.update(overrides)
    return CanonicalOrder(**data)


class TestBuildOrderForm:
    """build_order_form 字段落点快照（《逆推规范》§4 映射表）。"""

    async def test_required_fields_landing(self):
        form, warnings = build_order_form(_sample_order())
        # 必填：提单号 → data[0][b_order_num]；箱型 → box[N][b_type/box_num]
        assert form["data[0][b_order_num]"] == "OOLU4044379500"
        assert form["box[0][b_type]"] == "40HQ" and form["box[0][box_num]"] == "2"
        assert form["box[1][b_type]"] == "20GP" and form["box[1][box_num]"] == "1"
        # 常量：order_num1=1、create_order=true、o_id 空（新增）、费用四通道省略
        assert form["order_num1"] == "1"
        assert form["create_order"] == "true"
        assert form["o_id"] == ""
        assert not any(k.startswith(("shou[", "pay[", "duo_get[", "cost[")) for k in form)

    async def test_field_mapping_snapshot(self):
        """标准字段 → TMS 表单字段逐项落点（§4 映射表）。"""
        form, _ = build_order_form(_sample_order())
        # 客户：c_title=客户名称（2026-08-13 实测响应回显确认）；c_name=客户联系人
        # （实证 c_name 渲染为 UI「联系人」）；c_id 恒发（控制器硬读，缺键 204 拒单）
        assert form["c_title"] == "德清华凯"  # customer_name
        assert form["c_name"] == "张经理"  # customer_contact
        assert form["c_sn"] == "SED161509"  # customer_no
        assert form["c_id"] == ""  # 控制器硬读键恒发（未建档恒空）
        assert form["factory_name"] == "德清和普"  # door_point
        assert form["driver[0][b_date]"] == "2016-07-26"  # work_date
        assert form["driver[0][b_get_address]"] == "提箱点A"  # pickup_point
        assert form["driver[0][b_back_address]"] == "还箱点B"  # return_point
        assert form["data[0][j]"] == "10"  # pieces
        assert form["data[0][m]"] == "12.5"  # gross_weight
        assert form["data[0][hh]"] == "化工品"  # cargo_name
        assert form["b_ship_name"] == "MAERSK SARNIA"  # vessel
        assert form["b_ship_num"] == "752E"  # voyage
        assert form["b_wharf"] == "外二期"  # port_area
        assert form["month"] == "2016-07"  # order_date → 账期
        assert form["type"] == "1"  # biz_type 出口 → 1
        assert form["b_lock"] == "SN888"  # 首箱封号
        assert "业务编号：16070002-1" in form["b_note"]  # biz_no 拼入备查
        assert "业务类型：出口" in form["b_note"]

    async def test_note_dual_send_b_note_and_c_note(self):
        """备注双发（2026-08-31 用户拍板）：c_note 与 b_note 同内容。

        背景：TMS 界面「业务备注」落点未实证（b_note/c_note 哪个被界面消费），
        双发零风险（PHP 控制器硬读键名，多余键无害）；remark/biz_no/biz_type
        全部拼入两键。
        """
        form, _ = build_order_form(_sample_order())
        assert form["c_note"] == form["b_note"]
        assert "备注原文" in form["c_note"]
        assert "业务编号：16070002-1" in form["c_note"]
        # 无备注无业务编号时双键同步空串
        empty = build_order_form(
            _sample_order(remark=None, biz_no=None, biz_type=None)
        )[0]
        assert empty["c_note"] == "" and empty["b_note"] == ""

    async def test_multi_container_takes_first_and_warns(self):
        """多箱号：b_num 取首箱 + warning（split_per_container 预留，默认 false）。"""
        form, warnings = build_order_form(_sample_order())
        assert form["b_num"] == "TCLU1234567"
        assert any("一票多箱" in w and "split_per_container" in w for w in warnings)
        # 单箱无多箱 warning（unmapped 报告 warning 仍在：样例含客户名称）
        single = _sample_order(containers=[ContainerInfo(container_no="TCLU1234567")])
        _, warnings2 = build_order_form(single)
        assert not any("一票多箱" in w for w in warnings2)

    async def test_junyu_customer_fields(self):
        """军羽 r73 实证票（《修复prompt》F4）：客户名称「锦煦」曾误入 c_name。

        修复后：c_title=客户名称（旧链路实证），c_name 由 customer_contact 供给，
        军羽无该列 → 留空（联系人栏不再出现错误客户名称）。
        """
        junyu = _sample_order(
            bl_no="ZIMUSNH3849269",
            customer_name="锦煦",
            customer_contact=None,
        )
        form, _ = build_order_form(junyu)
        assert form["c_title"] == "锦煦"  # 客户名称 → c_title（实测回显确认）
        assert form["c_name"] == ""  # 军羽无客户联系人列 → 留空
        assert form["c_id"] == ""  # 控制器硬读键恒发，避免 204 拒单
        # 有客户联系人的家族：c_name = 联系人值（通寰/秋怡/志驿/亚灏/1111）
        form2, _ = build_order_form(_sample_order(customer_contact="朱经理"))
        assert form2["c_name"] == "朱经理"

    async def test_missing_optional_omitted_as_empty(self):
        """选填缺失 → 空串（PHP 控制器直接索引读取的语义，与既有链路一致）。"""
        order = _sample_order(
            customer_no=None,
            customer_contact=None,
            door_point=None,
            work_date=None,
            vessel=None,
            voyage=None,
            biz_no=None,
            biz_type=None,
            remark=None,
            pieces=None,
            gross_weight=None,
            cargo_name=None,
        )
        form, _ = build_order_form(order)
        assert form["c_sn"] == "" and form["factory_name"] == ""
        assert form["driver[0][b_date]"] == ""
        assert form["c_name"] == ""  # customer_contact 缺失 → 空串
        assert form["b_ship_name"] == "" and form["b_ship_num"] == ""
        assert form["data[0][j]"] == "" and form["data[0][m]"] == "" and form["data[0][hh]"] == ""
        assert form["b_note"] == "" and form["c_note"] == ""  # 无备注双键同步空
        # month 回退 order_date
        assert form["month"] == "2016-07"

    async def test_type_defaults_to_export(self):
        """type 枚举：仅确认 1=出口；进口等其他值留 TODO 默认 1。"""
        assert build_order_form(_sample_order(biz_type="进口"))[0]["type"] == "1"
        assert build_order_form(_sample_order(biz_type=None, io_type=None))[0]["type"] == "1"

    async def test_payload_flat_form_only(self):
        """TMS 直连表单：扁平字段 + create_order=true；b 双写实测非必需（2026-08-13），不再发送。"""
        payload, _ = build_order_payload(_sample_order())
        assert "a" not in payload and "b" not in payload and "c" not in payload
        assert payload["create_order"] == "true"
        assert payload["data[0][b_order_num]"] == "OOLU4044379500"


class TestSubmitCanonical:
    """client 侧：响应 code 字符串 "200" 判定 + form 字段与 create_order=true 提交。"""

    async def test_success_parses_sn_and_o_id(self, monkeypatch):
        captured: dict = {}

        async def fake_post(url, *, payload=None, headers=None, name=None, payload_kind=None, **_kwargs):
            captured["url"] = url
            captured["data"] = payload
            captured["headers"] = headers
            return FakeResponse(
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080355", "o_id": "21034692"}]}
            )

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)
        order = _sample_order()
        result = await client_module.submit_canonical_async("sk-token", order)
        assert result == {
            "success": True,
            "sn": "EX26080355",
            "o_id": "21034692",
            "error": None,
            "upstream": {"sn": "EX26080355", "o_id": "21034692"},  # 原始回显原样保留
        }
        # form-data 提交：create_order=true + 提单号 + 鉴权头
        assert captured["data"]["create_order"] == "true"
        assert captured["data"]["data[0][b_order_num]"] == "OOLU4044379500"
        assert captured["headers"] == {"sk": "sk-token"}

    async def test_code_as_int_200_also_success(self, monkeypatch):
        """响应 code 兼容数字 200（PHP 接口历史形态）。"""

        async def fake_post(*_a, **_k):
            return FakeResponse({"code": 200, "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)
        result = await client_module.submit_canonical_async("sk", _sample_order())
        assert result["success"] is True and result["sn"] == "EX1"

    async def test_rejected_code_not_200(self, monkeypatch):
        """code 非 "200" → 该单 error（不抛异常，调用方按单处理）。"""

        async def fake_post(*_a, **_k):
            return FakeResponse({"code": "500", "msg": "箱型不存在"})

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)
        result = await client_module.submit_canonical_async("sk", _sample_order())
        assert result["success"] is False
        assert result["error"]["code"] == "order_upstream_error"
        assert result["error"]["details"]["upstream_message"] == "箱型不存在"

    async def test_create_canonical_orders_sk_passthrough(self, monkeypatch):
        """sk 由调用方透传 + 逐单提交；单失败隔离不中断（2026-08-19 起无凭证链路）。"""
        responses = iter(
            [
                FakeResponse({"code": "200", "data": [{"sn": "EX1"}]}),
                FakeResponse({"code": "500", "msg": "拒单"}),
                FakeResponse({"code": "200", "data": [{"sn": "EX3"}]}),
            ]
        )
        posts: list[str] = []
        sk_seen: list[str | None] = []

        async def fake_post(url, **_kwargs):
            posts.append(url)
            sk_seen.append((_kwargs.get("headers") or {}).get("sk"))
            return next(responses)

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)
        orders = [_sample_order(), _sample_order(bl_no="B2"), _sample_order(bl_no="B3")]
        await create_canonical_orders_async(orders, "tk-caller")
        assert sum(1 for u in posts if "CreateOrder" in u or "AddWork" in u) == 3
        assert sk_seen and all(s == "tk-caller" for s in sk_seen)  # sk 原样透传
        assert orders[0].create_result["success"] and orders[0].create_result["sn"] == "EX1"
        assert orders[1].create_result["success"] is False  # 单失败隔离
        assert orders[2].create_result["sn"] == "EX3"  # 后续单不受影响
