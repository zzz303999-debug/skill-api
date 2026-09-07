"""第 1 批：基础信息四接口（客户/工厂/车辆/司机）httpx 层 mock 测试。

与 test_master_data.py（create_archives 层 mock，覆盖阈值/依赖序/回填编排）
互补：本文件恢复真实 create_archives 调用链，mock httpx.post 与凭证，
逐接口验证请求体字段完整性 + 响应成功/失败解析（HTTP 错误/非 JSON/
拒单/无主键/已存在）+ 四接口并行失败隔离 + service 集成不阻断。

- 建档调用一律 mock（零网络）；端点/默认值全走注入配置（测试值）；
- sk 由调用方透传（run_master_data 显式传 sk，聚焦建档端点）。
"""

from __future__ import annotations

import pytest
import yaml

import app.core.http_client as http_client_module
import app.orders.bill.master_data.client as md_client_module
import app.orders.bill.master_data.config as md_cfg_module
from app.orders.bill import BoxGroup, CanonicalOrder, build_result_async
from app.orders.bill.master_data.keys import client_key, driver_key, factory_key
from app.orders.bill.master_data.orchestrator import (
    KIND_CLIENT,
    KIND_DRIVER,
    KIND_FACTORY,
    KIND_TRUCK,
    run_master_data_async,
)
from app.orders.bill.master_data.store import get_master_data_store
from app.orders.bill.submission.imported_registry import owner_key
from helpers import FakeResponse

pytestmark = pytest.mark.asyncio

# 本文件用例统一 sk（建档维度 owner = owner_key(sk)，2026-09 owner 化）
_TEST_SK = "sk-token"
_TEST_OWNER = owner_key(_TEST_SK)

# 建档端点测试值（避开真实域名；与 test_master_data.TEST_ENDPOINTS 同口径）
TEST_ENDPOINTS = {
    "client_create": "http://jxt.test/Create/Client",
    "factory_create": "http://jxt.test/Create/Factory",
    "bailor_create": "TODO",
    "truck_create": "http://jxt.test/Create/Truck",
    "driver_create": "http://jxt.test/Create/Driver",
    "price_create": "http://jxt.test/Create/Price",
}

_DEFAULTS = {
    "client": {"cg_id": "4", "cg_name": "同行", "sys_type": "1", "su_id": "15478"},
    "truck": {"section_id": "6987", "remind_id": "15478", "sys_type": "1", "type": "bill"},
    "driver": {"sinout": "自做", "sys_type": "1"},
    "factory": {"sys_type": "1"},
}


def _md_cfg(threshold: int = 5) -> dict:
    return {
        "enabled": True,
        "threshold": threshold,
        "sn_prefix": {"client": "CLT", "factory": "FAC", "truck": "TRK", "driver": "DRV"},
        "endpoints": dict(TEST_ENDPOINTS),
        "defaults": dict(_DEFAULTS),
    }


@pytest.fixture()
def md_config(tmp_path, monkeypatch):
    """注入 master_data 配置（临时文件 + 缓存重置）；teardown 恢复真实配置缓存。"""
    real_path = md_cfg_module._CONFIG_PATH

    def _set(cfg: dict | None = None) -> None:
        path = tmp_path / "master_data.yaml"
        path.write_text(
            yaml.safe_dump({"master_data": cfg or _md_cfg()}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(md_cfg_module, "_CONFIG_PATH", path)
        md_cfg_module.reload_config()

    yield _set
    md_cfg_module._CONFIG_PATH = real_path
    md_cfg_module.reload_config()


@pytest.fixture()
def real_archives(monkeypatch, _no_real_archive_calls):
    """恢复真实 create_archives（conftest 全局 mock 是零网络兜底）——本文件
    验证建档内部调用链（httpx 层），显式依赖本 fixture 拿回真实实现。"""
    monkeypatch.setattr(md_client_module, "create_archives_async", _no_real_archive_calls)
    return _no_real_archive_calls


@pytest.fixture()
def fake_http(monkeypatch):
    """httpx.post mock：按 URL 路由返回预设响应，记录每次请求（url/data/headers）。"""
    captured: list[dict] = []

    def _install(responder) -> None:
        async def fake_post(url, *, payload=None, headers=None, timeout=None, name=None, payload_kind=None, **kwargs):
            captured.append({"url": url, "data": payload, "headers": headers, "timeout": timeout})
            return responder(url, data=payload)

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)

    _install.captured = captured
    return _install


def _make_order(
    *,
    customer: str | None = "锦煦",
    customer_contact: str | None = "张经理",
    contact_phone: str | None = "13900000000",
    door: str | None = "上海仓",
    address: str | None = "浦东新区",
    driver: str | None = "王师傅",
    plate: str | None = "沪A12345",
    phone: str | None = "13800000000",
) -> CanonicalOrder:
    return CanonicalOrder(
        bl_no="BL00000001",
        box_groups=[BoxGroup(b_type="40HQ", box_num=1)],
        customer_name=customer,
        customer_contact=customer_contact,
        contact_phone=contact_phone,
        door_point=door,
        load_address=address,
        driver_name=driver,
        plate_no=plate,
        driver_phone=phone,
    )


def _success(kind: str) -> dict:
    """按档案类返回带主键的成功响应体（主键键名与 _PRIMARY_KEY_MAP 一致）。"""
    return {
        KIND_CLIENT: {"code": "200", "msg": "添加成功", "data": {"client_id": "c-1001"}},
        KIND_FACTORY: {"code": "200", "msg": "添加成功", "data": {"factory_id": "f-1002"}},
        KIND_TRUCK: {"code": "200", "msg": "添加成功", "data": {"truck_id": "t-1003"}},
        KIND_DRIVER: {"code": "200", "msg": "添加成功", "data": {"id": "d-1004"}},
    }[kind]


class TestClientCreate:
    """客户建档接口：请求体字段完整性 + 成功解析 client_id。"""

    async def test_success_fields_and_primary_key(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(lambda url, **kw: FakeResponse(_success(KIND_CLIENT)))
        report = await run_master_data_async([_make_order()], create_order=True, sk="sk-token")
        assert report["archived"][0] == {
            "kind": KIND_CLIENT,
            "key": client_key("锦煦"),
            "display": "锦煦",
            "archive_id": "c-1001",
        }
        req = fake_http.captured[0]
        assert req["url"] == TEST_ENDPOINTS["client_create"]
        assert req["headers"] == {"sk": "sk-token"}
        # 请求体字段完整性：最小字段集 + defaults 合并 + 联系人
        form = req["data"]
        assert form["client_name"] == "锦煦"
        assert form["sn"] == "CLT00001"
        assert form["cg_id"] == "4" and form["cg_name"] == "同行"
        assert form["sys_type"] == "1" and form["su_id"] == "15478"
        assert form["data[0][n]"] == "张经理" and form["data[0][p]"] == "13900000000"
        assert get_master_data_store().get(KIND_CLIENT, client_key("锦煦"), _TEST_OWNER)["archive_id"] == "c-1001"

    async def test_success_without_contact_omits_contact_keys(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(lambda url, **kw: FakeResponse(_success(KIND_CLIENT)))
        await run_master_data_async(
            [_make_order(customer="客户甲", customer_contact=None, contact_phone=None)],
            create_order=True, sk="sk-token",
        )
        form = fake_http.captured[0]["data"]
        assert "data[0][n]" not in form and "data[0][p]" not in form  # 非必填空值省略键

    async def test_failure_http_500(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(lambda url, **kw: FakeResponse({"msg": "boom"}, status_code=500))
        # 仅客户候选（避免工厂/司机链干扰断言）
        report = await run_master_data_async([_make_order(door=None, driver=None)], create_order=True, sk="sk-token")
        failed = [f for f in report["failed"] if f["kind"] == KIND_CLIENT]
        assert failed and "HTTP error: 500" in failed[0]["reason"]
        assert report["archived"] == []
        assert get_master_data_store().get(KIND_CLIENT, client_key("锦煦"), _TEST_OWNER).get("archive_id") is None  # 不登记

    async def test_failure_non_json(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(lambda url, **kw: FakeResponse("html page", status_code=200))
        # 仅客户候选（避免工厂/司机链干扰断言）
        report = await run_master_data_async([_make_order(door=None, driver=None)], create_order=True, sk="sk-token")
        failed = [f for f in report["failed"] if f["kind"] == KIND_CLIENT]
        assert failed and "not a JSON object" in failed[0]["reason"]

    async def test_failure_rejected_code(self, md_config, fake_http, real_archives):
        """code 非 "200"（拒单）→ failed；msg 不命中 duplicate 标记。"""
        md_config(_md_cfg(threshold=1))
        fake_http(lambda url, **kw: FakeResponse({"code": "500", "msg": "分组不存在"}))
        report = await run_master_data_async([_make_order()], create_order=True, sk="sk-token")
        failed = [f for f in report["failed"] if f["kind"] == KIND_CLIENT]
        assert failed and "分组不存在" in failed[0]["reason"]

    async def test_failure_duplicate_marks_external(self, md_config, fake_http, real_archives):
        """「已存在」拒单 → exists_external 登记（不再重试，不记 failed）。"""
        md_config(_md_cfg(threshold=1))

        def responder(url, **kw):
            return FakeResponse({"code": "204", "msg": "客户名已存在,无法继续添加。"})

        fake_http(responder)
        # 仅客户候选（无门点无司机，避免工厂依赖/司机链干扰断言）
        report = await run_master_data_async([_make_order(door=None, driver=None)], create_order=True, sk="sk-token")
        assert report["failed"] == []
        ext = [e for e in report["exists_external"] if e["kind"] == KIND_CLIENT]
        assert ext and "已存在" in ext[0]["message"]
        rec = get_master_data_store().get(KIND_CLIENT, client_key("锦煦"), _TEST_OWNER)
        assert rec and rec.get("exists_external") is True and rec.get("archive_id") is None

    async def test_duplicate_branch_logging_extra_key_safe(
        self, md_config, fake_http, real_archives, monkeypatch
    ):
        """「已存在」分支日志 extra 不得使用 LogRecord 保留键 message（2026-08-31
        生产 500 回归：makeRecord 检查 extra 键直接 KeyError 冒泡到请求）。

        强制 INFO 生效路径：pytest 默认 root WARNING 会短路 INFO 日志（isEnabledFor
        为 False 不进入 makeRecord），不强制则该缺陷测不出来（历史假阴性）。
        """
        md_config(_md_cfg(threshold=1))
        fake_http(
            lambda url, **kw: FakeResponse(
                {"code": "204", "msg": "客户名已存在,无法继续添加。"}
            )
        )
        monkeypatch.setattr(md_client_module.log, "isEnabledFor", lambda level: True)
        report = await run_master_data_async(
            [_make_order(door=None, driver=None)], create_order=True, sk="sk-token"
        )
        ext = [e for e in report["exists_external"] if e["kind"] == KIND_CLIENT]
        assert ext and "已存在" in ext[0]["message"]

    async def test_failure_no_primary_key_marks_external(self, md_config, fake_http, real_archives):
        """成功但无主键（data:[] 实证形态）→ exists_external（防重复建档）。"""
        md_config(_md_cfg(threshold=1))
        fake_http(lambda url, **kw: FakeResponse({"code": "200", "msg": "添加成功", "data": []}))
        # 仅客户候选（避免工厂/司机链干扰断言）
        report = await run_master_data_async([_make_order(door=None, driver=None)], create_order=True, sk="sk-token")
        ext = [e for e in report["exists_external"] if e["kind"] == KIND_CLIENT]
        assert ext and "未返回主键" in ext[0]["message"]
        assert report["failed"] == []


class TestFactoryCreate:
    """工厂建档接口：依赖前置 client_id + 请求体字段完整性 + factory_id。"""

    async def test_success_fields_with_client_dependency(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(
            lambda url, **kw: FakeResponse(
                _success(KIND_CLIENT) if "Client" in url else _success(KIND_FACTORY)
            )
        )
        report = await run_master_data_async([_make_order()], create_order=True, sk="sk-token")
        assert any(a["kind"] == KIND_FACTORY for a in report["archived"])
        factory_req = next(r for r in fake_http.captured if "Factory" in r["url"])
        form = factory_req["data"]
        assert form["client_id"] == "c-1001"  # 前置客户建档 id 回填
        assert form["client_name"] == "锦煦"
        assert form["name"] == "上海仓"
        assert form["sn"] == "FAC00001"
        assert form["address"] == "浦东新区"
        assert form["sys_type"] == "1"
        # 依赖序：客户请求先于工厂请求
        assert fake_http.captured.index(factory_req) > 0

    async def test_factory_archives_with_client_exists_external(
        self, md_config, fake_http, real_archives
    ):
        """客户 TMS 已存在（exists_external、无本地 id）→ 工厂不再本地拦截：
        client_id 发空串照常尝试（2026-09-03 修复）；TMS 接受 → 建档成功。"""
        md_config(_md_cfg(threshold=1))
        fake_http(
            lambda url, **kw: FakeResponse(
                {"code": "204", "msg": "客户名已存在,无法继续添加。"}
                if "Client" in url
                else _success(KIND_FACTORY)
            )
        )
        # 仅客户+工厂候选（无司机，避免车辆/司机链干扰断言）
        report = await run_master_data_async(
            [_make_order(driver=None)], create_order=True, sk="sk-token"
        )
        assert report["failed"] == []
        assert any(a["kind"] == KIND_FACTORY for a in report["archived"])
        factory_req = next(r for r in fake_http.captured if "Factory" in r["url"])
        # 已存在客户无本地 id → client_id 省略键（非必填空值省略，与 payload 语义一致）
        assert "client_id" not in factory_req["data"]
        assert factory_req["data"]["client_name"] == "锦煦"
        rec = get_master_data_store().get(KIND_FACTORY, factory_key("上海仓", "浦东新区"), _TEST_OWNER)
        assert rec and rec.get("archive_id") == "f-1002"

    async def test_factory_rejected_without_client_id_marks_skip_once(
        self, md_config, fake_http, real_archives
    ):
        """空 client_id 被 TMS 拒（非「已存在」）→ skip_archive 终态：本批记一次失败，
        下批不再重发注定被拒的请求（防每批骚扰）。"""
        md_config(_md_cfg(threshold=1))

        def responder(url, **kw):
            if "Client" in url:
                return FakeResponse({"code": "204", "msg": "客户名已存在,无法继续添加。"})
            return FakeResponse({"code": "500", "msg": "缺少客户信息"})  # 工厂拒

        fake_http(responder)
        report = await run_master_data_async(
            [_make_order(driver=None)], create_order=True, sk="sk-token"
        )
        fac_failed = [f for f in report["failed"] if f["kind"] == KIND_FACTORY]
        assert fac_failed  # 本次尝试失败进报告
        rec = get_master_data_store().get(KIND_FACTORY, factory_key("上海仓", "浦东新区"), _TEST_OWNER)
        assert rec and rec.get("skip_archive") is True  # 终态：不再重试
        n_factory_requests = sum(1 for r in fake_http.captured if "Factory" in r["url"])
        assert n_factory_requests == 1
        # 下批：skip 终态 → 不再发工厂请求
        report2 = await run_master_data_async(
            [_make_order(driver=None)], create_order=True, sk="sk-token"
        )
        n2 = sum(1 for r in fake_http.captured if "Factory" in r["url"])
        assert n2 == n_factory_requests
        assert any(f["kind"] == KIND_FACTORY for f in report2["failed"]) is False


class TestTruckCreate:
    """车辆建档接口（司机依赖链内触发）：num/sn + 归属默认值 + truck_id。"""

    async def test_success_fields_and_primary_key(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(
            lambda url, **kw: FakeResponse(
                _success(KIND_TRUCK)
                if "Truck" in url
                else _success(KIND_DRIVER) if "Driver" in url else _success(KIND_CLIENT)
            )
        )
        report = await run_master_data_async([_make_order()], create_order=True, sk="sk-token")
        assert any(a["kind"] == KIND_TRUCK for a in report["archived"])
        truck_req = next(r for r in fake_http.captured if "Truck" in r["url"])
        form = truck_req["data"]
        assert form["num"] == "沪A12345"  # 车牌
        assert form["sn"] == "TRK00001"
        assert form["section_id"] == "6987" and form["remind_id"] == "15478"
        assert form["sys_type"] == "1" and form["type"] == "bill"


class TestDriverCreate:
    """司机建档接口：name/phone/num/sn + sinout 默认值 + truck_id + id 主键。"""

    async def test_success_fields_and_primary_key(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(
            lambda url, **kw: FakeResponse(
                _success(KIND_TRUCK)
                if "Truck" in url
                else _success(KIND_DRIVER) if "Driver" in url else _success(KIND_CLIENT)
            )
        )
        report = await run_master_data_async([_make_order()], create_order=True, sk="sk-token")
        assert any(a["kind"] == KIND_DRIVER for a in report["archived"])
        driver_req = next(r for r in fake_http.captured if "Driver" in r["url"])
        form = driver_req["data"]
        assert form["name"] == "王师傅"
        assert form["phone"] == "13800000000"
        assert form["num"] == "沪A12345"
        assert form["sn"] == "DRV00001"
        assert form["sinout"] == "自做" and form["sys_type"] == "1"
        assert form["truck_id"] == "t-1003"  # 依赖链：车辆先建档拿 truck_id
        # bailor_title/bailor_id 可空（实证）——不发送
        assert "bailor_title" not in form and "bailor_id" not in form


class TestParallelIsolation:
    """四接口并行（同批四类候选全部达阈值）：一类失败不阻断其余三类。"""

    def _responder(self, fail_kind: str | None):
        def responder(url, **kw):
            if fail_kind and fail_kind.lower() in url.lower():
                return FakeResponse({"code": "500", "msg": "服务器繁忙"})
            for kind, url_frag in (
                (KIND_CLIENT, "Client"),
                (KIND_FACTORY, "Factory"),
                (KIND_TRUCK, "Truck"),
                (KIND_DRIVER, "Driver"),
            ):
                if url_frag in url:
                    return FakeResponse(_success(kind))
            return FakeResponse({"code": "500", "msg": "unknown"})

        return responder

    async def test_all_four_succeed_in_one_run(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(self._responder(None))
        report = await run_master_data_async([_make_order()], create_order=True, sk="sk-token")
        assert [a["kind"] for a in report["archived"]] == [
            KIND_CLIENT,
            KIND_FACTORY,
            KIND_TRUCK,
            KIND_DRIVER,
        ]
        assert report["failed"] == []

    async def test_client_failure_blocks_factory_but_not_truck_driver(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(self._responder(KIND_CLIENT))
        report = await run_master_data_async([_make_order()], create_order=True, sk="sk-token")
        # 客户失败 → 工厂因依赖前置跳过（failed 原因）；车辆/司机链路不受影响
        assert any(f["kind"] == KIND_CLIENT for f in report["failed"])
        assert any(f["kind"] == KIND_FACTORY and "所属客户未建档" in f["reason"] for f in report["failed"])
        assert [a["kind"] for a in report["archived"]] == [KIND_TRUCK, KIND_DRIVER]
        # 计数保留（下批重试语义）
        assert get_master_data_store().get(KIND_CLIENT, client_key("锦煦"), _TEST_OWNER)["count"] == 1
        assert get_master_data_store().get(KIND_FACTORY, factory_key("上海仓", "浦东新区"), _TEST_OWNER)["count"] == 1

    async def test_driver_failure_does_not_break_others(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(self._responder(KIND_DRIVER))
        report = await run_master_data_async([_make_order()], create_order=True, sk="sk-token")
        assert any(f["kind"] == KIND_DRIVER for f in report["failed"])
        assert [a["kind"] for a in report["archived"]] == [
            KIND_CLIENT,
            KIND_FACTORY,
            KIND_TRUCK,
        ]
        # 司机计数保留 → 下批重试
        assert get_master_data_store().get(KIND_DRIVER, driver_key("王师傅", "沪A12345"), _TEST_OWNER)["count"] == 1


class TestSkPassthroughAllKinds:
    """sk 透传防回归：客户/工厂/车辆/司机四类建档请求头一律携带调用方 sk。

    既有用例仅客户类断言过 headers（TestClientCreate），工厂/车辆/司机只断言
    请求体字段（2026-08-19 review 发现的覆盖缺口）；漏传 sk 时下游 203 拒单，
    且 create_archives 不校验 sk 非空——靠本断言守住透传链不回归。
    """

    async def test_all_four_kinds_carry_sk_header(self, md_config, fake_http, real_archives):
        md_config(_md_cfg(threshold=1))
        fake_http(
            lambda url, **kw: FakeResponse(
                _success(KIND_TRUCK)
                if "Truck" in url
                else _success(KIND_DRIVER)
                if "Driver" in url
                else _success(KIND_FACTORY)
                if "Factory" in url
                else _success(KIND_CLIENT)
            )
        )
        report = await run_master_data_async([_make_order()], create_order=True, sk="sk-token")
        assert [a["kind"] for a in report["archived"]] == [
            KIND_CLIENT,
            KIND_FACTORY,
            KIND_TRUCK,
            KIND_DRIVER,
        ]
        # 四类建档端点各命中一次（依赖序：客户→工厂、车辆→司机）
        assert {r["url"] for r in fake_http.captured} == {
            TEST_ENDPOINTS["client_create"],
            TEST_ENDPOINTS["factory_create"],
            TEST_ENDPOINTS["truck_create"],
            TEST_ENDPOINTS["driver_create"],
        }
        # 每一笔建档请求头逐一断言：sk 原样透传（不重写/不丢失）
        for req in fake_http.captured:
            assert req["headers"] == {"sk": "sk-token"}, f"{req['url']} 未携带 sk"


class TestServiceIntegration:
    """service 层集成：建档失败进 meta.master_data.failed，订单照常创建（不阻断）。"""

    async def test_archive_failure_keeps_order_flow(self, md_config, monkeypatch, real_archives):
        md_config(_md_cfg(threshold=1))

        async def fake_post(url, *, payload=None, headers=None, name=None, payload_kind=None, timeout=None, **_kwargs):
            if "Client" in url:
                return FakeResponse({"code": "500", "msg": "客户建档失败"})
            if "Factory" in url or "Truck" in url or "Driver" in url:
                return FakeResponse({"code": "500", "msg": "依赖前置未建档"})
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX1"}]})

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)
        from helpers import build_bill_bytes

        # junyu 家族表头（L2 族级近似命中，既有语义 → canonical 转换；订单可建）
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
        result = await build_result_async(
            filename="junyu.xlsx",
            file_bytes=build_bill_bytes(
                headers, [{"A": 1, "B": "客户甲", "E": "OOLU12345678", "D": "40HQ", "I": "王师傅"}]
            ),
            create_order=True, sk="sk-token",
        )
        # 订单照常创建（建档失败不使订单丢失）
        assert result.summary["success"] == 1 and result.summary["failed"] == 0
        md = result.meta.get("master_data") or {}
        assert md and md["mode"] == "create"
        # 建档失败明细进 meta.master_data.failed（客户 + 工厂依赖前置 + 司机无车牌/手机）
        assert any(f["kind"] == KIND_CLIENT for f in md["failed"])
        assert md["archived"] == []  # 全部建档失败
        # 计数照常累计（下批重试语义）
        assert get_master_data_store().get(KIND_CLIENT, client_key("客户甲"), _TEST_OWNER)["count"] == 1
