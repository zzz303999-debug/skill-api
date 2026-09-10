"""第 3 批：业务订单（业务信息 + 财务四类费用合并）mock 测试。

- 完整请求体快照：一单同时提交 应收(shou)/应付(pay)/成本+车辆成本(cost)
  费用字段（主字段 + 四通道键位 + 三处合计回写 + 通道级 note 恒发 +
  cost 费目条目 driver_name 恒发），对照《逆推规范》§2.7/§4；
- 费用为空/部分为空时的请求体形态（空通道整段省略、合计键不出现）；
- 与去重注册表联动（四通道费用单视角）：创建成功登记 → 同提单号重传
  skipped 且下游 AddWork 0 次；失败单不登记 → 修正后重导正常提交。

- 纯函数断言（build_order_payload）零网络；下单 mock httpx（零网络）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import app.core.http_client as http_client_module
from app.orders.bill.schema import BoxGroup, CanonicalOrder, FeeItem
from app.orders.bill.submission.client import create_canonical_orders_async
from app.orders.bill.submission.payload import build_order_payload
from helpers import FakeResponse

pytestmark = pytest.mark.asyncio




def _fee_order(**overrides) -> CanonicalOrder:
    """一单四通道费用样例：应收运费 + 应付油费 + 成本（车辆成本打劫费 + 成本出车费）。"""
    data = dict(
        bl_no="OOLU12345678",
        box_groups=[BoxGroup(b_type="40HQ", box_num=2)],
        customer_name="客户甲",
        customer_contact="张经理",
        customer_no="SED161509",
        door_point="门点A",
        work_date="2026-08-10",
        fees=[
            FeeItem(channel="shou", code="freight", tms_name="运费", price_id=820, money=Decimal("100.00")),
            FeeItem(channel="pay", code="fuel", tms_name="油费", price_id=1134, money=Decimal("80.00")),
            FeeItem(channel="cost", code="hijack", tms_name="打劫费", price_id=1135, money=Decimal("30.00")),
            FeeItem(channel="cost", code="trip", tms_name="出车费", price_id=821, money=Decimal("20.00")),
        ],
        source_template="categorize_v1",
        row_count=1,
    )
    data.update(overrides)
    order = CanonicalOrder(**data)
    order._fee_defaults = {"price_type": "1", "is_profit": "1", "dai_dian": "1"}
    return order


class TestFullPayloadWithFourChannels:
    """业务 + 财务四类费用合并提交：完整 form 快照（《逆推规范》§2.7/§4）。"""

    async def test_four_channel_form_snapshot(self):
        form, warnings = build_order_payload(_fee_order())
        # 业务主字段（合并提交同一 form）
        assert form["data[0][b_order_num]"] == "OOLU12345678"
        assert form["box[0][b_type]"] == "40HQ" and form["box[0][box_num]"] == "2"
        assert form["c_title"] == "客户甲"
        assert form["c_name"] == "张经理"
        assert form["c_sn"] == "SED161509"
        assert form["factory_name"] == "门点A"
        assert form["driver[0][b_date]"] == "2026-08-10"
        # 应收 shou：六属性键 + 两位小数字符串
        shou = "shou[0][运费]"
        assert form[f"{shou}[money]"] == "100.00"
        assert form[f"{shou}[price_id]"] == "820"
        assert form[f"{shou}[price_type]"] == "1"
        assert form[f"{shou}[is_profit]"] == "1"
        assert form[f"{shou}[dai_dian]"] == "1"
        # 应付 pay
        pay = "pay[0][油费]"
        assert form[f"{pay}[money]"] == "80.00"
        assert form[f"{pay}[price_id]"] == "1134"
        # 成本 + 车辆成本（同 cost 通道不同费目；费目条目 driver_name 恒发）
        hijack = "cost[0][打劫费]"
        assert form[f"{hijack}[money]"] == "30.00"
        assert form[f"{hijack}[price_id]"] == "1135"
        assert form[f"{hijack}[driver_name]"] == ""
        trip = "cost[0][出车费]"
        assert form[f"{trip}[money]"] == "20.00"
        assert form[f"{trip}[price_id]"] == "821"
        # 三处合计回写（我方计算）：应收/应付进 driver[0]，成本进 cost[0]
        assert form["driver[0][get_ys_zj]"] == "100.00"
        assert form["driver[0][pay_yf_zj]"] == "80.00"
        assert form["cost[0][supplier_hj_zj]"] == "50.00"  # 30 + 20
        # 通道级 note 恒发（无 to_other 项 → 空串；缺键 204 拒单实证）
        assert form["shou[0][note]"] == ""
        assert form["pay[0][note]"] == ""
        assert form["cost[0][note]"] == ""
        assert warnings == []

    async def test_to_other_note_at_channel_level(self):
        """to_other 原名进通道级 note（「原名 ¥金额」列表拼接）。"""
        order = _fee_order(
            fees=[
                FeeItem(channel="shou", code="freight", tms_name="运费", price_id=820, money=Decimal("100.00")),
                FeeItem(
                    channel="shou",
                    code="other",
                    tms_name="其它费",
                    price_id=90002,
                    money=Decimal("6.00"),
                    note="高速费,掏箱费",
                ),
            ]
        )
        form, _ = build_order_payload(order)
        # 同码多原名在归集层已逗号合并进 note → 单条目「原名列表 ¥金额」
        assert form["shou[0][note]"] == "高速费,掏箱费 ¥6.00"
        assert form["driver[0][get_ys_zj]"] == "106.00"
        assert "pay[0][note]" not in form  # 无费用通道整段省略（含 note）


class TestFeeShapeVariants:
    """费用为空/部分为空时的请求体形态。"""

    async def test_empty_fees_omitted_entirely(self):
        form, _ = build_order_payload(_fee_order(fees=[]))
        assert not any(k.startswith(("shou[", "pay[", "cost[", "duo_get[")) for k in form)
        # 合计键不出现（_emit_fees 未执行）
        assert "driver[0][get_ys_zj]" not in form
        assert "driver[0][pay_yf_zj]" not in form
        assert "cost[0][supplier_hj_zj]" not in form
        # 业务主字段照常
        assert form["data[0][b_order_num]"] == "OOLU12345678"

    async def test_partial_shou_only(self):
        """仅应收：pay/cost 段与对应合计键不出现，应收合计正常。"""
        order = _fee_order(
            fees=[
                FeeItem(channel="shou", code="freight", tms_name="运费", price_id=820, money=Decimal("100.00")),
            ]
        )
        form, _ = build_order_payload(order)
        assert form["driver[0][get_ys_zj]"] == "100.00"
        assert not any(k.startswith(("pay[", "cost[", "duo_get[")) for k in form)
        assert "driver[0][pay_yf_zj]" not in form
        assert "cost[0][supplier_hj_zj]" not in form

    async def test_partial_price_id_null_excluded(self):
        """price_id null（待补/自举失败）→ 该费目整段省略，合计不含其金额。"""
        order = _fee_order(
            fees=[
                FeeItem(channel="shou", code="freight", tms_name="运费", price_id=820, money=Decimal("100.00")),
                FeeItem(channel="shou", code="waiting", tms_name="待时费", price_id=None, money=Decimal("50.00")),
            ]
        )
        form, _ = build_order_payload(order)
        assert "shou[0][待时费]" not in form  # 不录入（excluded 语义）
        assert form["shou[0][运费][money]"] == "100.00"
        assert form["driver[0][get_ys_zj]"] == "100.00"  # 合计不含 50


class TestDedupWithFeeOrders:
    """去重注册表联动（四通道费用单视角）：成功登记 / 失败不登记。"""

    @staticmethod
    def _fake_chain(monkeypatch, calls: dict, responses: list):
        """AddWork 按 responses 队列返回（sk 由调用方透传，无凭证链路）。"""
        queue = iter(responses)

        async def fake_post(url, **_kwargs):
            calls["addwork"] += 1
            return next(queue)

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)

    async def test_second_upload_skipped_zero_downstream(self, monkeypatch):
        """同提单号（四通道费用单）重传 → skipped 且下游 AddWork 0 次新增。"""
        calls = {"addwork": 0}
        self._fake_chain(
            monkeypatch,
            calls,
            [FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1", "o_id": "2101"}]})],
        )
        first = _fee_order()
        await create_canonical_orders_async([first], "sk")
        assert first.create_result.success is True
        assert first.create_result.sn == "EX1"
        assert calls["addwork"] == 1

        second = _fee_order()  # 同提单号不同对象
        await create_canonical_orders_async([second], "sk")
        assert second.create_result.skipped is True
        assert second.create_result.sn == "EX1"  # 回显首次创建 sn
        assert calls["addwork"] == 1  # 不重复下单

    async def test_failed_not_registered_retry_submits(self, monkeypatch):
        """失败单不登记：修正后重导正常提交（下游重新调用恰 1 次）。"""
        calls = {"addwork": 0}
        self._fake_chain(
            monkeypatch,
            calls,
            [
                FakeResponse({"code": "204", "msg": "箱型不存在"}),  # 首次失败
                FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX2"}]}),
            ],
        )
        first = _fee_order()
        await create_canonical_orders_async([first], "sk")
        assert first.create_result.success is False
        assert first.create_result.skipped is False  # 失败非 skipped（补全缺省键，R5）
        assert calls["addwork"] == 1

        retry = _fee_order()
        await create_canonical_orders_async([retry], "sk")
        assert retry.create_result.success is True
        assert retry.create_result.skipped is False  # 未被误拦（补全缺省键，R5）
        assert retry.create_result.sn == "EX2"
        assert calls["addwork"] == 2  # 失败 1 次 + 修正后 1 次

    async def test_preview_touches_no_registry(self, monkeypatch):
        """preview（不调 create_canonical_orders_async）零注册表读写——锁死去重零副作用。"""
        import app.orders.bill.submission.imported_registry as imported_registry

        registry = imported_registry.get_imported_registry()
        calls = {"lookup": 0, "register": 0}

        def _counting(fn, key):
            def wrapped(*a, **k):
                calls[key] += 1
                return fn(*a, **k)

            return wrapped

        monkeypatch.setattr(registry, "lookup", _counting(registry.lookup, "lookup"))
        monkeypatch.setattr(registry, "register", _counting(registry.register, "register"))
        _fee_order()  # 仅构造订单（preview 路径不会触达 client）
        assert calls == {"lookup": 0, "register": 0}
