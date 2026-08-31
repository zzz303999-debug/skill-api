"""service（build_result）测试：总览字段、meta 溯源、JSON 契约、create 模式。"""

from __future__ import annotations

import hashlib

import pytest

import app.orders.bill.client as client_module
from app.errors import BadRequestError, ConvertError
from app.orders.bill import BillParseResult, build_result
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


class TestSkGuard:
    """create 模式 sk 防御性校验（build_result 公开函数，防第二入口漏传）。"""

    def test_create_without_sk_raises_bad_request(self):
        """create_order=True 且 sk 缺失 → 400 bad_request（与路由层同语义）；
        守卫在解析前触发，坏文件内容不触发 IO/解析。"""
        with pytest.raises(BadRequestError) as caught:
            build_result(filename="x.xlsx", file_bytes=b"x", create_order=True)
        assert caught.value.http_status == 400
        assert caught.value.code == "bad_request"
        assert caught.value.details["upstream"] == {
            "code": "400",
            "msg": "缺少 TMS token（sk 请求头），请先登录 TMS",
            "data": [],
        }

    def test_create_with_blank_sk_raises(self):
        """空白 sk（纯空格）同样拒绝——与路由层 strip 后判空同口径。"""
        with pytest.raises(BadRequestError):
            build_result(filename="x.xlsx", file_bytes=b"x", create_order=True, sk="   ")

    def test_preview_without_sk_not_guarded(self):
        """preview 不要求 sk：守卫只拦 create 模式，预览路径不受影响。"""
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
                headers, [{"A": 1, "B": "客户甲", "E": "OOLU12345678", "D": "40HQ", "I": "王师傅"}]
            ),
            create_order=False,
        )
        assert result.create_order is False
        assert result.summary is None


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
            "upstream",
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
        # 客户字段（2026-08-13 实证）：c_title=客户名称（旧链路 AddWork 实证 dump），
        # c_name=客户联系人；金科信经转换后同样适用（表单键断言见 test_payload）
        assert first["customer_name"]
        assert first["unmapped_note"] is None  # 客户字段已有 c_title 落点，无未映射字段


@pytest.mark.skipif(
    not REAL_XLS.exists(), reason="golden 样本未入库（表格文件不入库），本地放置后自动启用"
)
class TestCreateMode:
    def test_create_fills_create_result_and_summary(self, monkeypatch):
        """create_order=True → 逐单 create_result + summary；preview 路径零变化。"""

        def fake_post(url, **_kwargs):
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080042"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = build_result(
            filename=REAL_XLS.name,
            file_bytes=REAL_XLS.read_bytes(),
            create_order=True,
            sk="sk-token",
        )
        assert result.create_order is True
        assert result.summary == {
            "total": REAL_ORDER_COUNT,
            "success": REAL_ORDER_COUNT,
            "failed": 0,
            "skipped": 0,
            "created": REAL_ORDER_COUNT,
            "success_sns": ["EX26080042"] * REAL_ORDER_COUNT,
            "failed_details": [],
        }
        assert result.upstream == {
            "code": "200",
            "msg": "添加成功",
            "data": [{"sn": "EX26080042"}] * REAL_ORDER_COUNT,
        }
        assert all(
            o.create_result and o.create_result["success"] and o.create_result["sn"] == "EX26080042"
            for o in result.orders
        )

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
            sk="sk-token",
        )
        assert result.summary == {
            "total": 1,
            "success": 1,
            "failed": 0,
            "skipped": 0,
            "created": 1,
            "success_sns": ["EX1"],
            "failed_details": [],
        }
        assert result.upstream == {
            "code": "200",
            "msg": "添加成功",
            "data": [{"sn": "EX1", "o_id": "2101"}],
        }
        assert result.canonical_orders[0].create_result["success"] is True
        assert result.canonical_orders[0].create_result["sn"] == "EX1"
        assert result.canonical_orders[0].create_result["o_id"] == "2101"
        assert result.canonical_orders[0].create_result["upstream"] == {"sn": "EX1", "o_id": "2101"}
        assert sum(1 for u in posts if "AddWork" in u) == 1  # TMS 直连走 AddWork 端点
        # 客户字段：c_title=客户名称（旧链路实证，表单键断言见 test_payload）；
        # 军羽无联系人列 → customer_contact 空
        order = result.canonical_orders[0]
        assert order.customer_name == "客户甲"
        assert order.customer_contact is None

    # ---- 重复上传去重（方案一：成功单注册表）----

    @staticmethod
    def _junyu_file() -> bytes:
        """构造 junyu 家族账单（canonical 语义，2 单不同提单号）。"""
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
        return build_bill_bytes(
            headers,
            [
                {"A": 1, "B": "客户甲", "E": "OOLU10000001", "D": "40HQ", "F": "TCLU1"},
                {"A": 2, "B": "客户甲", "E": "OOLU10000002", "D": "40HQ", "F": "TCLU2"},
            ],
        )

    @staticmethod
    def _fake_ok_chain(monkeypatch, calls: dict):
        """下单按调用序计数并返回成功（sk 由调用方透传，无凭证链路）。"""

        def fake_post(url, **_kwargs):
            calls["addwork"] += 1
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)

    def test_dedup_second_upload_skipped(self, monkeypatch):
        """同文件重导（create_order=True）：第二次全部 skipped（下游 0 次新增调用）。"""
        calls = {"addwork": 0}
        self._fake_ok_chain(monkeypatch, calls)
        file_bytes = self._junyu_file()
        first = build_result(filename="junyu.xlsx", file_bytes=file_bytes, create_order=True, sk="sk")
        assert first.summary == {
            "total": 2,
            "success": 2,
            "failed": 0,
            "skipped": 0,
            "created": 2,
            "success_sns": ["EX1", "EX1"],
            "failed_details": [],
        }
        assert calls["addwork"] == 2
        second = build_result(filename="junyu.xlsx", file_bytes=file_bytes, create_order=True, sk="sk")
        assert second.summary == {
            "total": 2,
            "success": 2,
            "failed": 0,
            "skipped": 2,
            "created": 0,
            "success_sns": ["EX1", "EX1"],
            "failed_details": [],
        }
        assert calls["addwork"] == 2  # 不重复下单
        assert all(o.create_result.get("skipped") for o in second.canonical_orders)
        # 全部 skipped（无新建动作）→ upstream 保持 None（与「无单可创建」同语义，
        # 路由层转 409；不得误报 204 添加失败）
        assert second.upstream is None

    def test_dedup_different_sk_not_skipped(self, monkeypatch):
        """异 sk（不同操作员）导入同一账单：不命中注册表，照常创建（2026-08-31 起）。

        去重维度从提单号全局改为 (提单号, sk)：A 创建后 B 导入不再被误拦，
        各自真实下单、各自登记。
        """
        calls = {"addwork": 0}
        self._fake_ok_chain(monkeypatch, calls)
        file_bytes = self._junyu_file()
        build_result(filename="junyu.xlsx", file_bytes=file_bytes, create_order=True, sk="sk-a")
        assert calls["addwork"] == 2
        other = build_result(
            filename="junyu.xlsx", file_bytes=file_bytes, create_order=True, sk="sk-b"
        )
        assert other.summary["skipped"] == 0
        assert other.summary["created"] == 2
        assert calls["addwork"] == 4  # 异 sk 各自真实下单
        assert all(not o.create_result.get("skipped") for o in other.canonical_orders)

    def test_dedup_failed_not_registered_retry_creates(self, monkeypatch):
        """失败单不登记：重导时失败单正常创建（修正后重导不被误拦）。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            calls["addwork"] += 1
            if calls["addwork"] == 1:
                return FakeResponse({"code": "204", "msg": "添加失败"})
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX2"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        file_bytes = self._junyu_file()
        first = build_result(filename="junyu.xlsx", file_bytes=file_bytes, create_order=True, sk="sk")
        assert first.summary["failed"] == 1
        assert first.summary["success"] == 1
        second = build_result(filename="junyu.xlsx", file_bytes=file_bytes, create_order=True, sk="sk")
        assert second.summary == {
            "total": 2,
            "success": 2,
            "failed": 0,
            "skipped": 1,
            "created": 1,
            "success_sns": ["EX2", "EX2"],
            "failed_details": [],
        }

    def test_all_failed_upstream_204(self, monkeypatch):
        """build_result 直测：新建全失败 → upstream 204 结构（锁死契约）。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            calls["addwork"] += 1
            return FakeResponse({"code": "204", "msg": "添加失败"})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = build_result(
            filename="junyu.xlsx", file_bytes=self._junyu_file(), create_order=True, sk="sk"
        )
        assert result.summary["success"] == 0 and result.summary["failed"] == 2
        assert result.upstream == {"code": "204", "msg": "添加失败", "data": []}
        assert calls["addwork"] == 2

    def test_success_without_upstream_echo_is_200(self, monkeypatch):
        """下游 code 200 但无 data 回显 → 仍按新建成功返回 200（不得误报 204）。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            calls["addwork"] += 1
            return FakeResponse({"code": "200", "msg": "添加成功"})  # 无 data[0]

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = build_result(
            filename="junyu.xlsx", file_bytes=self._junyu_file(), create_order=True, sk="sk"
        )
        assert result.summary["success"] == 2 and result.summary["created"] == 2
        # 成功但无原始回显：业务码仍为 200（data 为空），与 summary 不矛盾
        assert result.upstream == {"code": "200", "msg": "添加成功", "data": []}
        assert calls["addwork"] == 2

    def test_too_many_rows_raises_bad_request(self, monkeypatch):
        """build_result 直测：行数超限 → BadRequestError too_many_rows（双管线口径）。"""
        from types import SimpleNamespace

        import app.orders.bill.service as service_module
        from app.config import settings
        from app.errors import BadRequestError

        monkeypatch.setattr(settings, "bill_import_max_rows", 2)
        monkeypatch.setattr(
            service_module,
            "parse_bill",
            lambda _path: SimpleNamespace(
                rows=[None] * 3, canonical_rows=[None] * 3, period=None
            ),
        )
        with pytest.raises(BadRequestError) as caught:
            build_result(filename="many.xlsx", file_bytes=b"x")
        assert caught.value.http_status == 400
        assert caught.value.code == "too_many_rows"
        assert caught.value.details == {
            "total_rows": 6,  # rows + canonical_rows 同口径合计
            "max_rows": 2,
            "upstream": {"code": "400", "msg": "数据量过大，联系人工客服", "data": []},
        }

    def test_nan_echo_sanitized_to_null(self, monkeypatch):
        """下游回显含 NaN/Infinity 字面量 → 归一为 None（JSON null），不污染响应体。"""
        calls = {"addwork": 0}

        def fake_post(url, **_kwargs):
            calls["addwork"] += 1
            return FakeResponse(
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX1", "fee": float("nan")}]}
            )

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        result = build_result(
            filename="junyu.xlsx", file_bytes=self._junyu_file(), create_order=True, sk="sk"
        )
        assert calls["addwork"] == 2
        assert result.upstream == {
            "code": "200",
            "msg": "添加成功",
            "data": [{"sn": "EX1", "fee": None}] * 2,
        }
        # 透传进 create_result 的原始回显同样已清洗（route 层序列化不再 500）
        assert all(
            o.create_result["upstream"]["fee"] is None for o in result.canonical_orders
        )

    def test_preview_never_touches_imported_registry(self, monkeypatch):
        """preview 模式零注册表读写（去重零副作用，锁死回归）。"""
        from app.orders.bill import imported_registry

        registry = imported_registry.get_imported_registry()
        calls = {"lookup": 0, "register": 0}

        def _counting(fn, key):
            def wrapped(*a, **k):
                calls[key] += 1
                return fn(*a, **k)

            return wrapped

        monkeypatch.setattr(registry, "lookup", _counting(registry.lookup, "lookup"))
        monkeypatch.setattr(registry, "register", _counting(registry.register, "register"))
        result = build_result(filename="junyu.xlsx", file_bytes=self._junyu_file())
        assert result.create_order is False
        assert calls == {"lookup": 0, "register": 0}

    def test_dedup_skipped_not_counted_in_master_data(self, monkeypatch):
        """重导跳过单不再计数（建档阈值统计不被重复上传推高）。"""
        from app.orders.bill import master_data_store

        calls = {"addwork": 0}
        self._fake_ok_chain(monkeypatch, calls)
        file_bytes = self._junyu_file()
        store = master_data_store.get_store()
        build_result(filename="junyu.xlsx", file_bytes=file_bytes, create_order=True, sk="sk")
        after_first = dict(store.snapshot())
        assert after_first  # 第一次导入产生计数
        build_result(filename="junyu.xlsx", file_bytes=file_bytes, create_order=True, sk="sk")
        assert store.snapshot() == after_first  # 第二次（全 skipped）计数不变


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
