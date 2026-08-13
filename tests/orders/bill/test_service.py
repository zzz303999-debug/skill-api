"""service（build_result）测试：总览字段、meta 溯源、JSON 契约、create 模式。"""

from __future__ import annotations

import hashlib

import pytest

import app.orders.bill.client as client_module
from app.errors import ConvertError
from app.orders.bill import BillParseResult, build_result
from app.orders.bill.client import UpstreamError
from helpers import (
    REAL_ORDER_COUNT,
    REAL_RAW_ROWS,
    REAL_TOTAL_ROWS,
    REAL_XLS,
    FakeResponse,
    build_bill_bytes,
)


def build_real_result() -> BillParseResult:
    return build_result(filename=REAL_XLS.name, file_bytes=REAL_XLS.read_bytes())


@pytest.mark.skipif(
    not REAL_XLS.exists(), reason="golden 样本未入库（表格文件不入库），本地放置后自动启用"
)
class TestOverview:
    def test_overview_fields(self):
        result = build_real_result()
        assert result.file == REAL_XLS.name
        assert result.bill_period == "2015-01-01~2015-12-31"
        assert result.total_rows == REAL_TOTAL_ROWS
        assert result.order_count == REAL_ORDER_COUNT
        assert result.create_order is False
        assert result.summary is None

    def test_meta(self):
        result = build_real_result()
        meta = result.meta
        assert meta["source_sha256"] == hashlib.sha256(REAL_XLS.read_bytes()).hexdigest()
        assert meta["source_bytes"] == REAL_XLS.stat().st_size
        assert meta["parser"] == "xlrd"
        assert meta["raw_rows"] == REAL_RAW_ROWS
        assert meta["unmatched_headers"] == []
        assert "reconciliation" in meta and meta["reconciliation"]["status"] == "matched"
        assert meta["reconciliation"]["fees_status"] == "matched"  # 合计大写第二锚点吻合
        import re

        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", meta["parsed_at"])

    def test_json_contract(self):
        """model_dump(mode='json') 顶层与 orders[] 键对齐文档 §3.3/§3.4（新增 canonical_orders）。"""
        dumped = build_real_result().model_dump(mode="json")
        assert set(dumped) == {
            "file",
            "bill_period",
            "total_rows",
            "order_count",
            "create_order",
            "orders",
            "canonical_orders",
            "summary",
            "meta",
        }
        assert set(dumped["orders"][0]) == {
            "order_num1",
            "c_title",
            "container_count",
            "row_count",
            "missing_fields",
            "missing_reasons",
            "order_data",
            "create_result",
        }
        assert all(o["create_result"] is None for o in dumped["orders"])
        # TMS 通道：金科信经转换得到标准订单（通寰家族由 jinxin_v1 覆盖）
        assert len(dumped["canonical_orders"]) == REAL_ORDER_COUNT
        first = dumped["canonical_orders"][0]
        assert first["bl_no"] == "SHSB52129400"
        assert first["source_template"] == "jinxin_v1"
        assert first["box_groups"] and first["box_groups"][0]["b_type"]
        assert first["missing_fields"] == []


@pytest.mark.skipif(
    not REAL_XLS.exists(), reason="golden 样本未入库（表格文件不入库），本地放置后自动启用"
)
class TestCreateMode:
    def test_create_fills_create_result_and_summary(self, monkeypatch):
        """create_order=True → 逐单 create_result + summary；preview 路径零变化。"""

        def fake_post(url, **_kwargs):
            if "GetWebKey" in url:
                return FakeResponse({"code": 200, "msg": "操作成功", "web_key": "wk"})
            if "login" in url:
                return FakeResponse({"code": 200, "data": {"token": "sk"}, "msg": "操作成功"})
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080042"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = build_result(
            filename=REAL_XLS.name,
            file_bytes=REAL_XLS.read_bytes(),
            create_order=True,
        )
        assert result.create_order is True
        assert result.summary == {
            "total": REAL_ORDER_COUNT,
            "success": REAL_ORDER_COUNT,
            "failed": 0,
        }
        assert all(
            o.create_result and o.create_result["success"] and o.create_result["sn"] == "EX26080042"
            for o in result.orders
        )

    def test_credential_failure_raises_upstream_error(self, monkeypatch):
        """凭证失败 → UpstreamError（502 order_upstream_error）上抛，不逐单。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            if "GetWebKey" in url:
                return FakeResponse({"code": 500, "msg": "凭据无效"})
            calls["addwork"] += 1
            return FakeResponse({"code": "200", "msg": "添加成功"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        with pytest.raises(UpstreamError) as caught:
            build_result(
                filename=REAL_XLS.name,
                file_bytes=REAL_XLS.read_bytes(),
                create_order=True,
            )
        assert caught.value.http_status == 502
        assert caught.value.code == "order_upstream_error"
        assert calls["addwork"] == 0

    def test_preview_never_calls_downstream(self, monkeypatch):
        """preview 路径（create_order=False）零下游调用（行为零变化）。"""
        calls = {"n": 0}

        def fake_post(*_a, **_k):
            calls["n"] += 1
            return FakeResponse({"code": "200"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = build_result(filename=REAL_XLS.name, file_bytes=REAL_XLS.read_bytes())
        assert result.summary is None
        assert calls["n"] == 0

    def test_canonical_create_mode_uses_addwork(self, monkeypatch):
        """标准字段家族（canonical 语义）create_order=True → create_canonical_orders
        （AddWork 端点 + sk 头 + create_order=true，2026-08-13 实测定论）。"""
        responses = iter(
            [
                FakeResponse({"code": 200, "web_key": "wk"}),
                FakeResponse({"code": 200, "data": {"token": "sk"}}),
                FakeResponse({"code": "200", "data": [{"sn": "EX1", "o_id": "2101"}]}),
            ]
        )
        posts: list[str] = []

        def fake_post(url, **_kwargs):
            posts.append(url)
            return next(responses)

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        # junyu 家族表头（L2 族级近似命中，canonical 语义）
        headers = {
            "A": "序号",
            "B": "客户名称",
            "C": "门点",
            "D": "箱型箱量",
            "E": "提单号",
            "F": "箱号",
            "G": "做箱时间",
            "H": "港区",
            "I": "司机",
            "J": "应收备注",
        }
        result = build_result(
            filename="junyu.xlsx",
            file_bytes=build_bill_bytes(
                headers,
                [{"A": 1, "B": "客户甲", "E": "OOLU12345678", "D": "40HQ", "F": "TCLU1"}],
            ),
            create_order=True,
        )
        assert result.summary == {"total": 1, "success": 1, "failed": 0}
        assert result.canonical_orders[0].create_result["success"] is True
        assert result.canonical_orders[0].create_result["sn"] == "EX1"
        assert result.canonical_orders[0].create_result["o_id"] == "2101"
        assert sum(1 for u in posts if "AddWork" in u) == 1  # TMS 直连走 AddWork 端点


class TestConstructed:
    def test_no_period_bill(self):
        """无结算区间 → bill_period=None，不报错。"""
        result = build_result(
            filename="no-period.xlsx",
            file_bytes=build_bill_bytes(
                {"A": "序号", "B": "客户编号", "C": "提单号", "D": "箱型"},
                [{"A": 1, "B": "C001", "C": "OOLU12345678", "D": "40HQ"}],
            ),
        )
        assert result.bill_period is None
        assert result.order_count == 1

    def test_broken_bytes_convert_error(self):
        """坏文件 bytes → ConvertError 原样上抛。"""
        try:
            build_result(filename="bad.xlsx", file_bytes=b"not a zip")
            raise AssertionError("应抛 ConvertError")
        except ConvertError as exc:
            assert exc.code == "convert_error"
