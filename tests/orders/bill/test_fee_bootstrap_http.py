"""第 2 批：费用栏目（费用管理新增）httpx 层 mock 测试。

与 test_fee_bootstrap.py（create_archives 层 mock，覆盖四级解析/幂等/降级编排）
互补：本文件恢复真实 create_archives 调用链，mock httpx.post 与凭证，验证
- 费用栏目新增请求体字段完整性（name/sn={prefix}_{CODE}/create_defaults 合并/
  布尔 on 归一/is_other 特判）
- 响应成功解析（price_id 回传 → registry 登记 → 当批回填）与失败解析
  （HTTP 错误/非 JSON/拒单/无主键/已存在）
- service 集成：自举失败不阻断订单（费用降级 excluded 仅对账）

- 建档调用一律 mock（零网络）；费目映射表/自举配置/建档端点全走注入测试值；
- sk 由调用方透传（run_fee_bootstrap 显式传 sk，聚焦建档端点）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

import app.orders.bill.fee_bootstrap as fb_module
import app.orders.bill.fee_price_map as fp_module
import app.orders.bill.master_data as md_module
import app.orders.bill.master_data_client as md_client_module
from app.orders.bill import BoxGroup, CanonicalOrder, FeeItem, build_result
from app.orders.bill.fee_bootstrap import build_price_form, run_fee_bootstrap
from app.orders.bill.fee_price_map import apply_price_map
from app.orders.bill.fee_registry import get_registry
from helpers import FakeResponse, build_bill_bytes

# 注入的费目映射表（测试值）：freight 已有 id；waiting/other 待自举
_FEE_MAP = {
    "freight": {"tms_name": "运费", "price_id": 820},
    "waiting": {"tms_name": "待时费", "price_id": None},
    "other": {"tms_name": "其它费", "price_id": None},
}

_BS_CFG = {
    "enabled": True,
    "endpoint_key": "price_create",
    "create_defaults": {
        "sn_prefix": "AUTO",
        "inout": "3",
        "is_get": "on",
        "is_pay": "on",
        "price_type": "1",
        "is_profit": "1",
        "status": "1",
        "dai_dian": "2",
        "bao_zhang": "1",
        "od": "0",
        "use_imprest": "0",
        "expense_rate": "5",
        "class_id": "4612",
        "classification_name": "运费",
    },
}


def _fee_map_yaml() -> str:
    body = {code: dict(entry) for code, entry in _FEE_MAP.items()}
    body["fee_bootstrap"] = _BS_CFG
    return yaml.safe_dump(body, allow_unicode=True, sort_keys=False)


@pytest.fixture()
def price_cfg(tmp_path, monkeypatch):
    """注入 fee_price_map 配置（临时文件）；teardown 恢复真实路径与缓存。"""
    real_path = fp_module.price_map_path
    path = tmp_path / "fee_price_map.test.yaml"
    path.write_text(_fee_map_yaml(), encoding="utf-8")
    monkeypatch.setattr(fp_module, "price_map_path", lambda: path)
    fp_module.reload_price_map()
    fb_module.reload_bootstrap_config()
    yield
    fp_module.price_map_path = real_path
    fp_module.reload_price_map()
    fb_module.reload_bootstrap_config()


@pytest.fixture()
def md_endpoint(tmp_path, monkeypatch):
    """注入 master_data endpoints（price_create 测试端点）；teardown 恢复。"""
    real_path = md_module._CONFIG_PATH
    path = tmp_path / "master_data.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "master_data": {
                    "enabled": True,
                    "threshold": 5,
                    "endpoints": {
                        "client_create": "http://jxt.test/Create/Client",
                        "factory_create": "http://jxt.test/Create/Factory",
                        "bailor_create": "TODO",
                        "truck_create": "http://jxt.test/Create/Truck",
                        "driver_create": "http://jxt.test/Create/Driver",
                        "price_create": "http://jxt.test/Create/Price",
                    },
                    "defaults": {},
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(md_module, "_CONFIG_PATH", path)
    md_module.reload_config()
    yield
    md_module._CONFIG_PATH = real_path
    md_module.reload_config()


@pytest.fixture()
def real_archives(monkeypatch, _no_real_archive_calls):
    """恢复真实 create_archives（conftest 全局 mock 是零网络兜底）。"""
    monkeypatch.setattr(md_client_module, "create_archives", _no_real_archive_calls)
    return _no_real_archive_calls


@pytest.fixture()
def fake_http(monkeypatch):
    """httpx.post mock：记录每次请求（url/data），按 responder 返回。"""
    captured: list[dict] = []

    def _install(responder) -> None:
        def fake_post(url, *, data=None, headers=None, timeout=None, **kwargs):
            captured.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
            return responder(url, data=data)

        monkeypatch.setattr(md_client_module.httpx, "post", fake_post)

    _install.captured = captured
    return _install


def _make_order(*codes: str) -> CanonicalOrder:
    """构造含指定费目码的订单（price_id 待回填）。"""
    fees = [
        FeeItem(channel="shou", code=code, money=Decimal(f"{i + 1}0.00"))
        for i, code in enumerate(codes)
    ]
    return CanonicalOrder(
        bl_no="BL12345678",
        box_groups=[BoxGroup(b_type="40HQ", box_num=1)],
        fees=fees,
    )


class TestPriceForm:
    """费用栏目新增请求体：build_price_form 字段完整性。"""

    def test_form_fields_with_defaults(self, price_cfg, md_endpoint):
        """create_defaults 打底 + 生成值覆盖（name/sn）；布尔 on 归一。"""
        form = build_price_form("waiting", "待时费")
        assert form["name"] == "待时费"
        assert form["sn"] == "AUTO_WAITING"  # {sn_prefix}_{CODE 大写}
        assert form["inout"] == "3"
        assert form["is_get"] == "on" and form["is_pay"] == "on"  # 布尔 True → "on"
        assert form["class_id"] == "4612" and form["classification_name"] == "运费"
        assert form["price_type"] == "1" and form["dai_dian"] == "2"
        assert "is_other" not in form  # 非其它费不带 is_other

    def test_other_code_sends_is_other(self, price_cfg, md_endpoint):
        """其它费特判：other 码额外发 is_other=1（逆推规范 §8.2）。"""
        form = build_price_form("other", "其它费")
        assert form["is_other"] == "1"
        assert form["sn"] == "AUTO_OTHER"

    def test_success_parses_price_id_and_registers(self, price_cfg, md_endpoint, fake_http, real_archives):
        """建档成功 → price_id 解析 → registry 登记 → 当批 apply_price_map 回填。"""
        fake_http(lambda url, **kw: FakeResponse({"code": "200", "msg": "添加成功", "data": {"price_id": 90001}}))
        report = run_fee_bootstrap([_make_order("waiting")], create_order=True, sk="sk-token")
        assert report["mode"] == "create"
        assert report["created"] == [{"code": "waiting", "tms_name": "待时费", "price_id": 90001}]
        assert report["failed"] == [] and report["exists_external"] == []
        # 请求体（与 build_price_form 同构）
        req = fake_http.captured[0]
        assert req["url"] == "http://jxt.test/Create/Price"
        assert req["headers"] == {"sk": "sk-token"}
        assert req["data"]["name"] == "待时费" and req["data"]["sn"] == "AUTO_WAITING"
        # registry 登记 + 当批回填（apply_price_map 经 registry 命中）
        assert get_registry().lookup("waiting")["price_id"] == 90001
        order = _make_order("waiting")
        apply_price_map(order.fees)
        assert order.fees[0].price_id == 90001 and order.fees[0].tms_name == "待时费"

    def test_multi_code_one_call_each(self, price_cfg, md_endpoint, fake_http, real_archives):
        """同批多码逐码建档（一次调用一码），互不干扰。"""
        price_ids = iter([90001, 90002])
        fake_http(lambda url, **kw: FakeResponse({"code": "200", "msg": "ok", "data": {"price_id": next(price_ids)}}))
        report = run_fee_bootstrap([_make_order("waiting", "other")], create_order=True, sk="sk-token")
        assert [c["code"] for c in report["created"]] == ["waiting", "other"]
        assert [r["data"]["sn"] for r in fake_http.captured] == ["AUTO_WAITING", "AUTO_OTHER"]
        assert get_registry().lookup("other")["price_id"] == 90002


class TestPriceFailures:
    """费用栏目新增失败解析：HTTP 错误 / 非 JSON / 拒单 / 无主键 / 已存在。"""

    def test_failure_http_500(self, price_cfg, md_endpoint, fake_http, real_archives):
        fake_http(lambda url, **kw: FakeResponse({"msg": "boom"}, status_code=500))
        report = run_fee_bootstrap([_make_order("waiting")], create_order=True, sk="sk-token")
        assert report["created"] == [] and report["exists_external"] == []
        assert report["failed"] and "HTTP error: 500" in report["failed"][0]["reason"]
        assert get_registry().lookup("waiting") is None  # 失败不登记 → 下批重试

    def test_failure_non_json(self, price_cfg, md_endpoint, fake_http, real_archives):
        fake_http(lambda url, **kw: FakeResponse("html page", status_code=200))
        report = run_fee_bootstrap([_make_order("waiting")], create_order=True, sk="sk-token")
        assert report["failed"] and "not a JSON object" in report["failed"][0]["reason"]

    def test_failure_rejected_code(self, price_cfg, md_endpoint, fake_http, real_archives):
        """code 非 "200"（拒单，msg 不命中已存在）→ failed。"""
        fake_http(lambda url, **kw: FakeResponse({"code": "500", "msg": "费类不存在"}))
        report = run_fee_bootstrap([_make_order("waiting")], create_order=True, sk="sk-token")
        assert report["failed"] and "费类不存在" in report["failed"][0]["reason"]

    def test_failure_duplicate_marks_external(self, price_cfg, md_endpoint, fake_http, real_archives):
        """「已存在」拒单 → exists_external 登记（不再重试自举）。"""
        fake_http(lambda url, **kw: FakeResponse({"code": "204", "msg": "费用名称已存在"}))
        report = run_fee_bootstrap([_make_order("waiting")], create_order=True, sk="sk-token")
        assert report["failed"] == []
        ext = report["exists_external"]
        assert ext and ext[0]["code"] == "waiting" and "已存在" in ext[0]["message"]
        assert get_registry().lookup("waiting") is None
        assert get_registry().exists_external("waiting") is True
        # 下批不再重试（exists_external 终态；无缺失码 → 不产生报告段）
        assert run_fee_bootstrap([_make_order("waiting")], create_order=True, sk="sk-token") is None

    def test_failure_no_primary_key_marks_external(self, price_cfg, md_endpoint, fake_http, real_archives):
        """成功但无主键 → 自举路径按 failed 处理（registry 不登记，下批重试）；
        注：与 master_data 建档（exists_external 终态防重复建档）语义不一致，
        已报告待实现侧决策（当前价格创建端点实测均回 price_id，未触发此路径）。"""
        fake_http(lambda url, **kw: FakeResponse({"code": "200", "msg": "添加成功", "data": []}))
        report = run_fee_bootstrap([_make_order("waiting")], create_order=True, sk="sk-token")
        assert report["failed"] and "未返回主键" in report["failed"][0]["reason"]
        assert report["exists_external"] == [] and report["created"] == []
        assert get_registry().lookup("waiting") is None


class TestServiceIntegration:
    """service 集成：自举失败不阻断订单；费用降级 excluded 仅对账。"""

    def test_bootstrap_failure_keeps_order_flow(self, price_cfg, md_endpoint, monkeypatch, real_archives):
        import app.orders.bill.client as client_module

        def fake_post(url, data=None, **_kwargs):
            if "/Create/Price" in url:
                return FakeResponse({"code": "500", "msg": "费目建档失败"})
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        # junyu 家族表头（L2 命中；含应收费用列「运费」→ waiting 码待自举）
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
            "K": "待时费",
        }
        result = build_result(
            filename="junyu.xlsx",
            file_bytes=build_bill_bytes(
                headers,
                [{"A": 1, "B": "客户甲", "E": "OOLU12345678", "D": "40HQ", "K": 50}],
            ),
            create_order=True, sk="sk-token",
        )
        # 订单照常创建（自举失败不使订单丢失）
        assert result.summary["success"] == 1 and result.summary["failed"] == 0
        rec = result.meta.get("reconciliation") or {}
        reports = rec.get("reports") or {}
        bootstrap = reports.get("fee_bootstrap") or {}
        # 自举失败明细进报告（当批降级语义）
        assert bootstrap["failed"] and "费目建档失败" in bootstrap["failed"][0]["reason"]
        assert bootstrap["created"] == []
        # 费用降级：price_id null → excluded 不录入仅对账（dropped 清单可见）
        dropped = reports.get("price_null_dropped") or []
        assert any(d["code"] == "waiting" for d in dropped)


class TestBillrowServiceIntegration:
    """jinxin（BillRow 直传名）链模板外费用建档 service 集成（2026-09-04 拍板）：
    订单费用可直传但 TMS 费用管理缺档案 → create 自动 AddCarPrice 建档同名档案；
    preview 只出 planned；建档失败不阻塞下单；模板 6 名已建档不重复建。"""

    _EXTRA_HEADERS = [
        "加班费",
        "报关费",
        "查验费",
    ]

    def _bill_bytes(self) -> bytes:
        from io import BytesIO

        from openpyxl import Workbook
        from test_route import _JINXIN_HEADERS

        headers = _JINXIN_HEADERS[:29] + self._EXTRA_HEADERS + _JINXIN_HEADERS[29:]
        wb = Workbook()
        ws = wb.active
        ws.append(headers)
        r = [""] * len(headers)
        r[0] = 1
        r[1] = "1-27"
        r[3] = "测试客户甲"
        r[5] = "TST01"
        r[7] = "OOLU90000001A"
        r[8] = "门点"
        r[9] = "40HQ"
        r[12] = "地址"
        r[14] = "C1"
        r[16] = "洋山"
        r[23:29] = [100, 20, 0, 10, 5, 3]  # 运费..其它费
        r[29:32] = [8, 6, 0]  # 加班费/报关费/查验费
        ws.append(r)
        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_create_archives_unknown_names_and_registers(
        self, price_cfg, md_endpoint, monkeypatch
    ):
        import app.orders.bill.client as client_module

        def fake_addwork_post(url, *, data=None, headers=None, timeout=None, **kwargs):
            return FakeResponse(
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EXBILL1"}]}
            )

        monkeypatch.setattr(client_module.httpx, "post", fake_addwork_post)
        # 建档走 conftest 全局 mock（create_archives 层零网络，递增主键）
        result = build_result(
            filename="dyn.xlsx",
            file_bytes=self._bill_bytes(),
            create_order=True,
            sk="sk-token",
        )
        # 订单照常创建成功
        assert result.summary["success"] == 1 and result.summary["created"] == 1
        fb = (result.meta or {}).get("fee_bootstrap") or {}
        assert fb["mode"] == "create" and fb["failed"] == []
        created = {c["tms_name"]: c for c in fb["created"]}
        # 模板外 3 名建档（动态码）；已建档名（运费→freight 注入已有 price_id）不重复建
        assert {"加班费", "报关费", "查验费"} <= set(created)
        assert "运费" not in created
        assert all(isinstance(c["price_id"], int) for c in created.values())
        # registry 已登记（幂等：下次同文件不再建档）
        reg = get_registry()
        assert all(reg.lookup(c["code"]) is not None for c in created.values())

    def test_preview_planned_only_zero_side_effect(self, price_cfg, md_endpoint):

        result = build_result(
            filename="dyn.xlsx",
            file_bytes=self._bill_bytes(),
            create_order=False,
        )
        fb = (result.meta or {}).get("fee_bootstrap") or {}
        assert fb["mode"] == "preview"
        planned = {p["tms_name"] for p in fb["planned"]}
        # 3 模板外名进 planned；已建档名（运费）不在计划内
        assert {"加班费", "报关费", "查验费"} <= planned
        assert "运费" not in planned
        assert fb["created"] == [] and fb["failed"] == []
