"""阶段三（T17-T21）测试：归一键/阈值边界/持久化/同批去重/依赖序/失败非阻塞/
payload 回填/endpoints TODO 降级/preview 只读/司机车辆链/golden 集成。

全部断言用配置注入（threshold/endpoints/sn_prefix 测试值），无竞品名/TMS 业务值
硬编码；建档调用一律 mock（零网络）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import app.orders.bill.master_data as md_module
import app.orders.bill.master_data_client as md_client_module
import app.orders.http_client as http_client_module
from app.orders.bill import BoxGroup, CanonicalOrder, build_result_async
from app.orders.bill.master_data import (
    KIND_CLIENT,
    KIND_DRIVER,
    KIND_FACTORY,
    KIND_TRUCK,
    client_key,
    collect_candidates,
    driver_key,
    endpoint_for,
    factory_key,
    normalize_key,
    plate_key,
    run_master_data_async,
    sn_for,
)
from app.orders.bill.master_data_store import MasterDataStore, get_store
from app.orders.bill.payload import build_order_payload

pytestmark = pytest.mark.asyncio

FAMILIES_DIR = Path(__file__).resolve().parent.parent.parent / "golden" / "bill" / "families"

# 建档端点测试值（避开真实域名；触发建档用）
TEST_ENDPOINTS = {
    "client_create": "http://jxt.test/Create/Client",
    "factory_create": "http://jxt.test/Create/Factory",
    "bailor_create": "http://jxt.test/Create/Bailor",
    "truck_create": "http://jxt.test/Create/Truck",
    "driver_create": "http://jxt.test/Create/Driver",
    "price_create": "http://jxt.test/Create/Price",
}


@pytest.fixture()
def md_config(tmp_path, monkeypatch):
    """注入 master_data 配置（临时文件 + 缓存重置）；teardown 恢复真实配置缓存。"""
    real_path = md_module._CONFIG_PATH

    def _set(cfg: dict) -> None:
        path = tmp_path / "master_data.yaml"
        path.write_text(
            yaml.safe_dump({"master_data": cfg}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(md_module, "_CONFIG_PATH", path)
        md_module.reload_config()

    yield _set
    # 先还原路径再重载（pytest 撤销 monkeypatch 在 fixture teardown 之后，
    # 若不先还原，后续用例会读到残留的测试配置缓存）
    md_module._CONFIG_PATH = real_path
    md_module.reload_config()


@pytest.fixture()
def fake_create(md_config, monkeypatch):
    """建档调用 mock：记录每次调用（forms），默认全部成功返回递增 archive_id。"""
    calls: list[dict] = []

    def _install(cfg: dict | None = None, *, fail_kinds: set[str] | None = None):
        md_config(cfg or _default_cfg())

        async def _fake(forms_by_kind: dict[str, dict[str, dict[str, str]]], sk: str = ""):
            calls.append(forms_by_kind)
            results: dict = {}
            for kind, forms in forms_by_kind.items():
                results[kind] = {}
                for i, key in enumerate(forms):
                    if fail_kinds and kind in fail_kinds:
                        results[kind][key] = {
                            "success": False,
                            "archive_id": None,
                            "error": {"code": "master_data_create_error", "message": "boom"},
                        }
                    else:
                        results[kind][key] = {
                            "success": True,
                            "archive_id": f"aid-{kind}-{i}",
                            "error": None,
                        }
            return results

        monkeypatch.setattr(md_client_module, "create_archives_async", _fake)

    _install.calls = calls
    return _install


def _default_cfg(threshold: int = 5) -> dict:
    return {
        "enabled": True,
        "threshold": threshold,
        "sn_prefix": {"client": "CLT", "factory": "FAC", "truck": "TRK", "driver": "DRV"},
        # 司机端点已启用（2026-08-14 实证：/Car/CarDriver/AddCarDriver，与真实配置一致）
        "endpoints": dict(TEST_ENDPOINTS),
        "defaults": {
            "client": {"cg_id": "4", "cg_name": "同行", "sys_type": "1", "su_id": "15478"},
            "truck": {"section_id": "6987", "remind_id": "15478", "sys_type": "1", "type": "bill"},
            "driver": {"sinout": "自做", "sys_type": "1"},
            "factory": {"sys_type": "1"},
        },
    }


def _make_order(
    *,
    customer: str | None = "锦煦",
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
        door_point=door,
        load_address=address,
        driver_name=driver,
        plate_no=plate,
        driver_phone=phone,
    )


class TestNormalizeKeys:
    """T17 归一键：去首尾空格/全半角/连续空白；车牌全大写去空格。"""

    async def test_normalize_fullwidth_halfwidth(self):
        assert normalize_key("ＡＢＣ１２３") == "ABC123"
        assert normalize_key("锦煦") == normalize_key("锦煦")

    async def test_normalize_whitespace(self):
        assert normalize_key("  锦 煦  ") == "锦煦"  # 空白全部删除
        assert normalize_key("锦  煦") == normalize_key("锦 煦")  # 连续空白
        assert normalize_key("锦　煦") == "锦煦"  # 全角空格归一并删除

    async def test_client_key_consistent(self):
        assert client_key("锦煦") == client_key(" 锦煦 ") == client_key("锦　煦")

    async def test_plate_key_case_and_space(self):
        assert plate_key("沪a12345") == plate_key("沪A12345")
        assert plate_key(" 沪A 12345 ") == plate_key("沪A12345")
        assert plate_key(None) == ""

    async def test_factory_key_composite(self):
        assert factory_key("上海仓", "浦东") != factory_key("上海仓", "浦西")
        assert factory_key("上海仓", "浦东") == factory_key(" 上海仓 ", "浦东")

    async def test_driver_key_pair(self):
        assert driver_key("王师傅", "沪A12345") != driver_key("王师傅", "沪B67890")
        assert driver_key("王师傅", "沪A12345") != driver_key("李师傅", "沪A12345")
        assert driver_key("王师傅", "沪a12345") == driver_key("王师傅", "沪A12345")


class TestThreshold:
    """阈值边界：第 4 次不触发、第 5 次触发（口径：>N 次才录入）。"""

    async def test_fourth_not_trigger_fifth_triggers(self, fake_create):
        fake_create()
        for _ in range(4):
            report = await run_master_data_async([_make_order()], create_order=True)
        assert report["archived"] == [] and report["failed"] == []
        assert get_store().get(KIND_CLIENT, client_key("锦煦"))["count"] == 4
        assert fake_create.calls == []  # 未达阈值不建档

        report = await run_master_data_async([_make_order()], create_order=True)
        # 同一订单含客户/工厂/司机三类候选，第 5 次全部达阈值；建档按依赖序
        # （客户 → 工厂 → 车辆 → 司机；司机带车牌先建车拿 truck_id）
        assert [a["kind"] for a in report["archived"]] == [
            KIND_CLIENT,
            KIND_FACTORY,
            KIND_TRUCK,
            KIND_DRIVER,
        ]
        assert get_store().get(KIND_CLIENT, client_key("锦煦"))["count"] == 5
        client_calls = [c for c in fake_create.calls if KIND_CLIENT in c]
        assert len(client_calls) == 1
        assert client_calls[0][KIND_CLIENT][client_key("锦煦")]["client_name"] == "锦煦"

    async def test_sn_generated_from_count(self, fake_create):
        """sn = {sn_prefix}{5 位序号}（序号取该键累计计数，防存量撞名避让）。"""
        fake_create(_default_cfg(threshold=1))
        await run_master_data_async([_make_order()], create_order=True)
        form = fake_create.calls[0][KIND_CLIENT][client_key("锦煦")]
        assert form["sn"] == "CLT00001"
        assert sn_for(KIND_CLIENT, 12) == "CLT00012"


class TestPersistence:
    """跨批次持久化：重启（新实例）计数不丢。"""

    async def test_count_survives_reload(self, tmp_path):
        path = tmp_path / "md.json"
        store = MasterDataStore(path)
        store.record(KIND_CLIENT, client_key("锦煦"))
        store.record(KIND_CLIENT, client_key("锦煦"))

        # 模拟进程重启：新实例读同一文件
        restarted = MasterDataStore(path)
        rec = restarted.get(KIND_CLIENT, client_key("锦煦"))
        assert rec is not None and rec["count"] == 2

    async def test_archive_id_survives_reload(self, tmp_path):
        path = tmp_path / "md.json"
        store = MasterDataStore(path)
        store.record(KIND_CLIENT, client_key("锦煦"))
        store.set_archive(KIND_CLIENT, client_key("锦煦"), 169642)
        restarted = MasterDataStore(path)
        assert restarted.get(KIND_CLIENT, client_key("锦煦"))["archive_id"] == "169642"


class TestBatchDedupe:
    """同批去重：按单计（一单一次），建档调用每键只发一次（T19）。"""

    async def test_same_batch_counts_per_order_but_archives_once(self, fake_create):
        fake_create()
        orders = [_make_order() for _ in range(5)]
        report = await run_master_data_async(orders, create_order=True)
        assert get_store().get(KIND_CLIENT, client_key("锦煦"))["count"] == 5
        # 每档案类同批建档只发一次（依赖序：client/factory 各一次；
        # truck 在司机链内嵌调用一次，driver 收尾一次）
        for kind in (KIND_CLIENT, KIND_FACTORY, KIND_TRUCK, KIND_DRIVER):
            assert len([c for c in fake_create.calls if kind in c]) == 1
        # 同批去重：同一键只出现一个建档条目
        assert [a["kind"] for a in report["archived"]].count(KIND_CLIENT) == 1
        assert [a["kind"] for a in report["archived"]].count(KIND_DRIVER) == 1
        # 司机表单带前置 truck_id（依赖链：车辆 → 司机）
        driver_form = fake_create.calls[-1][KIND_DRIVER][driver_key("王师傅", "沪A12345")]
        assert driver_form["truck_id"] == "aid-truck-0"

    async def test_archived_result_reused_within_batch(self, fake_create):
        fake_create(_default_cfg(threshold=3))
        orders = [_make_order() for _ in range(6)]
        await run_master_data_async(orders, create_order=True)
        # 第 3 单触发建档，后续 3 单直接取用登记结果（调用仍只一次）
        client_calls = [c for c in fake_create.calls if KIND_CLIENT in c]
        assert len(client_calls) == 1
        assert get_store().get(KIND_CLIENT, client_key("锦煦"))["count"] == 6
        assert orders[5]._archive_refs[KIND_CLIENT]["archive_id"] == "aid-client-0"


class TestDependencyOrder:
    """依赖序（逆推规范 §14）：客户 → 工厂；工厂缺客户档案不建档但计数保留。"""

    async def test_factory_skipped_without_client(self, fake_create):
        """工厂达阈值但所属客户未达 → 跳过建档（failed 原因），工厂计数保留。"""
        fake_create(_default_cfg(threshold=3))
        orders = [
            _make_order(customer="客户甲", door="上海仓", address="浦东", driver=None),
            _make_order(customer="客户乙", door="上海仓", address="浦东", driver=None),
            _make_order(customer="客户丙", door="上海仓", address="浦东", driver=None),
        ]
        report = await run_master_data_async(orders, create_order=True)
        failed = [f for f in report["failed"] if f["kind"] == KIND_FACTORY]
        assert failed and "所属客户未建档" in failed[0]["reason"]
        assert get_store().get(KIND_FACTORY, factory_key("上海仓", "浦东"))["count"] == 3
        assert fake_create.calls == []  # 客户未建档 → 未触发任何建档调用

    async def test_factory_archives_after_client_with_client_id(self, fake_create):
        fake_create(_default_cfg(threshold=1))
        report = await run_master_data_async([_make_order()], create_order=True)
        kinds = [a["kind"] for a in report["archived"]]
        # 依赖序：客户先建档，工厂随后（其后为司机链）
        assert kinds[:2] == [KIND_CLIENT, KIND_FACTORY]
        # 工厂建档 form 带前置 client_id（依赖链关键；第二次调用即 factory）
        factory_form = fake_create.calls[1][KIND_FACTORY][factory_key("上海仓", "浦东新区")]
        assert factory_form["client_id"] == "aid-client-0"
        assert factory_form["client_name"] == "锦煦"

    async def test_client_failure_blocks_factory_but_keeps_counts(self, fake_create):
        """客户建档失败 → 工厂跳过（计数保留、下批重试）；不抛断。"""
        fake_create(_default_cfg(threshold=1), fail_kinds={KIND_CLIENT})
        report = await run_master_data_async([_make_order()], create_order=True)
        assert any(f["kind"] == KIND_FACTORY for f in report["failed"])
        assert any(f["kind"] == KIND_CLIENT for f in report["failed"])
        assert get_store().get(KIND_CLIENT, client_key("锦煦"))["count"] == 1
        assert get_store().get(KIND_FACTORY, factory_key("上海仓", "浦东新区"))["count"] == 1


class TestDriverArchived:
    """司机建档（2026-08-14 实证启用：/Car/CarDriver/AddCarDriver——bailor_title/
    bailor_id 可空、响应带主键 data.id、必填 num 车牌，早前 AddDriverGroup 方案 A 作废）。
    依赖链：车辆 → 司机（带车牌先建车拿 truck_id）；无车牌 → skip_archive 终态不建档；
    司机档案 id 不回填订单 payload（driver[0] 走文本 d_name/d_num），只落库。"""

    async def test_driver_archives_with_truck_chain(self, fake_create):
        fake_create(_default_cfg(threshold=1))
        order = _make_order(driver="王师傅", plate="沪A12345", phone="13800000000")
        report = await run_master_data_async([order], create_order=True)
        assert KIND_DRIVER in [a["kind"] for a in report["archived"]]
        assert KIND_TRUCK in [a["kind"] for a in report["archived"]]
        assert KIND_DRIVER not in report["degraded"]
        # 依赖链：truck 先建档（单独调用），driver 表单带 truck_id
        assert any(KIND_TRUCK in c for c in fake_create.calls)
        driver_form = fake_create.calls[-1][KIND_DRIVER][driver_key("王师傅", "沪A12345")]
        assert driver_form["name"] == "王师傅"
        assert driver_form["num"] == "沪A12345"
        assert driver_form["phone"] == "13800000000"
        assert driver_form["truck_id"] == "aid-truck-0"
        assert driver_form["sn"] == "DRV00001"
        assert driver_form["sinout"] == "自做"  # defaults.driver
        # bailor_title/bailor_id 可空（实证）——不发送
        assert "bailor_title" not in driver_form
        assert "bailor_id" not in driver_form
        # 落库 + 订单标注已建档（不再标未建档）
        rec = get_store().get(KIND_DRIVER, driver_key("王师傅", "沪A12345"))
        assert rec and rec["archive_id"] == "aid-driver-0"
        assert order._archive_refs[KIND_DRIVER]["archive_id"] == "aid-driver-0"
        assert "司机「王师傅/沪A12345」未建档" not in (order.unmapped_note or "")

    async def test_driver_without_plate_skips_archive(self, fake_create):
        """无车牌司机：TMS AddCarDriver 必填 num → 跳过建档（skip_archive 终态），
        计数照常、订单保留未建档标注、下批不再重试（不发注定被拒的请求）。"""
        fake_create(_default_cfg(threshold=1))
        order = _make_order(driver="王师傅", plate=None)
        report = await run_master_data_async([order], create_order=True)
        driver_failed = [f for f in report["failed"] if f["kind"] == KIND_DRIVER]
        assert driver_failed and "无车牌" in driver_failed[0]["reason"]
        assert not any(KIND_DRIVER in c for c in fake_create.calls)
        assert not any(KIND_TRUCK in c for c in fake_create.calls)
        rec = get_store().get(KIND_DRIVER, driver_key("王师傅", None))
        assert rec and rec["count"] == 1 and rec["skip_archive"] is True
        assert "司机「王师傅」未建档(1/1)" in (order.unmapped_note or "")
        # 下批不再重试（skip_archive 终态）
        await run_master_data_async([_make_order(driver="王师傅", plate=None)], create_order=True)
        assert not any(KIND_DRIVER in c for c in fake_create.calls)
        assert not any(KIND_TRUCK in c for c in fake_create.calls)

    async def test_driver_without_phone_skips_archive(self, fake_create):
        """有车牌无手机号：TMS AddCarDriver 必填 phone（缺键 500 / 空串 no: phone
        实证）→ 跳过建档（skip_archive 终态），不发注定被拒的请求。"""
        fake_create(_default_cfg(threshold=1))
        order = _make_order(driver="王师傅", plate="沪A12345", phone=None)
        report = await run_master_data_async([order], create_order=True)
        driver_failed = [f for f in report["failed"] if f["kind"] == KIND_DRIVER]
        assert driver_failed and "无手机号" in driver_failed[0]["reason"]
        assert not any(KIND_DRIVER in c for c in fake_create.calls)
        assert not any(KIND_TRUCK in c for c in fake_create.calls)
        rec = get_store().get(KIND_DRIVER, driver_key("王师傅", "沪A12345"))
        assert rec and rec["skip_archive"] is True
        assert "司机「王师傅/沪A12345」未建档(1/1)" in (order.unmapped_note or "")
        # 下批不再重试
        await run_master_data_async([_make_order(driver="王师傅", plate="沪A12345", phone=None)], create_order=True)
        assert not any(KIND_DRIVER in c for c in fake_create.calls)
        assert not any(KIND_TRUCK in c for c in fake_create.calls)

    async def test_driver_counts_across_batches(self, fake_create):
        """跨批计数：达阈值建档一次，后续批次直接取用登记结果不重复建档。"""
        fake_create(_default_cfg(threshold=3))
        for _ in range(3):
            await run_master_data_async([_make_order(driver="王师傅", plate="沪A12345")], create_order=True)
        driver_calls = [c for c in fake_create.calls if KIND_DRIVER in c]
        assert len(driver_calls) == 1
        assert get_store().get(KIND_DRIVER, driver_key("王师傅", "沪A12345"))["count"] == 3
        assert get_store().get(KIND_DRIVER, driver_key("王师傅", "沪A12345"))["archive_id"] == "aid-driver-0"

    async def test_driver_failed_keeps_counts_and_retries(self, fake_create):
        """司机建档失败 → 计数保留、订单标注未建档、下批重试（与全失败语义一致）。"""
        fake_create(_default_cfg(threshold=1), fail_kinds={KIND_DRIVER})
        order = _make_order(driver="王师傅", plate="沪A12345")
        report = await run_master_data_async([order], create_order=True)
        assert any(f["kind"] == KIND_DRIVER for f in report["failed"])
        assert "司机「王师傅/沪A12345」未建档(1/1)" in (order.unmapped_note or "")
        # 下批重试
        fake_create(_default_cfg(threshold=1))
        await run_master_data_async([_make_order(driver="王师傅", plate="沪A12345")], create_order=True)
        assert get_store().get(KIND_DRIVER, driver_key("王师傅", "沪A12345"))["archive_id"] == "aid-driver-0"


class TestFailureNonBlocking:
    """建档全部失败 → 订单照常（不抛断）、计数保留、报告 failed + 未建档标注。"""

    async def test_all_failed_keeps_order_flow(self, fake_create):
        fake_create(
            _default_cfg(threshold=1),
            fail_kinds={KIND_CLIENT, KIND_FACTORY, KIND_DRIVER, KIND_TRUCK},
        )
        order = _make_order()
        report = await run_master_data_async([order], create_order=True)
        assert report["failed"]
        assert order._archive_refs == {}  # 无回填
        # 订单标注「未建档(x/N)」（不阻塞语义可见；threshold 为注入值 1）
        assert "未建档(1/1)" in (order.unmapped_note or "")
        # 计数保留 → 下批继续累计
        assert get_store().get(KIND_CLIENT, client_key("锦煦"))["count"] == 1

    async def test_retry_next_batch(self, fake_create):
        """建档失败 → 下批再试（每批最多一次）。"""
        fake_create(_default_cfg(threshold=1), fail_kinds={KIND_CLIENT})
        await run_master_data_async([_make_order()], create_order=True)
        client_calls = [c for c in fake_create.calls if KIND_CLIENT in c]
        assert len(client_calls) == 1  # 首批尝试一次

        # 下批同一订单：再次尝试
        await run_master_data_async([_make_order()], create_order=True)
        assert len([c for c in fake_create.calls if KIND_CLIENT in c]) == 2


class TestPayloadBackfill:
    """T20 回填：已建档 → c_id/factory_id/b_factory_address_msg；未建档 → 零变化。"""

    async def test_backfill_keys_when_archived(self, fake_create):
        fake_create(_default_cfg(threshold=1))
        order = _make_order()
        await run_master_data_async([order], create_order=True)
        form, _ = build_order_payload(order)
        assert form["c_id"] == "aid-client-0"
        assert form["factory_id"] == "aid-factory-0"
        assert form["b_factory_address_msg"] == "浦东新区"
        assert form["c_title"] == "锦煦"  # 客户名文本不变（F1 语义）

    async def test_no_archive_keeps_text_only(self, fake_create):
        fake_create()  # threshold=5 未达
        order = _make_order()
        await run_master_data_async([order], create_order=True)
        form, _ = build_order_payload(order)
        assert form["c_id"] == ""
        assert form["factory_id"] == ""
        assert form["c_title"] == "锦煦"
        assert form["factory_name"] == "上海仓"


class TestDegradedEndpoints:
    """endpoints TODO（真实配置）→ 只计数不建档、报告 degraded、不 fail fast。"""

    async def test_todo_endpoints_count_only(self, md_config):
        md_config(_default_cfg())
        md_module.reload_config()
        # 把全部端点改回 TODO（模拟真实配置未补给）
        md_config(
            {
                "enabled": True,
                "threshold": 1,
                "sn_prefix": {},
                "endpoints": {k: "TODO" for k in TEST_ENDPOINTS},
                "defaults": {},
            }
        )
        report = await run_master_data_async([_make_order()], create_order=True)
        assert report["archived"] == []
        assert set(report["degraded"]) == {
            "client",
            "factory",
            "truck",
            "driver",
            "bailor",
        }
        # 计数照常（端点 TODO 不影响计数）
        assert get_store().get(KIND_CLIENT, client_key("锦煦"))["count"] == 1
        assert endpoint_for(KIND_CLIENT) is None


class TestClientDirectURL:
    """建档调用跨宿主直发（阶段三收尾 S2，2026-08-14）：endpoints 全量 URL（含
    s3.jxt56.com 不同宿主）原样使用、无 base_url 拼接；sk 鉴权头照带；URL 只从 config 读。"""

    async def test_create_archives_posts_full_url_as_is(
        self, md_config, monkeypatch, _no_real_archive_calls
    ):
        md_config(_default_cfg())
        from helpers import FakeResponse

        captured: dict = {}

        async def fake_post(url, *, payload, headers, timeout, name=None, payload_kind=None):
            captured.update(url=url, data=payload, headers=headers, timeout=timeout)
            return FakeResponse(
                {"code": "200", "msg": "添加成功", "data": {"client_id": "c-1"}}
            )

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)
        # 本用例验证 create_archives 内部调用链：恢复真实实现（conftest 全局
        # mock 是零网络兜底，显式依赖本 fixture 拿回真实函数）
        monkeypatch.setattr(md_client_module, "create_archives_async", _no_real_archive_calls)
        result = await md_client_module.create_archives_async(
            {KIND_CLIENT: {"key": {"client_name": "测试", "sn": "CLT00001"}}}, "sk-token"
        )
        # 全量 URL 原样直发（含 host，无 base_url 拼接）
        assert captured["url"] == TEST_ENDPOINTS["client_create"]
        assert captured["headers"] == {"sk": "sk-token"}
        assert captured["data"] == {"client_name": "测试", "sn": "CLT00001"}
        assert result[KIND_CLIENT]["key"]["archive_id"] == "c-1"


class TestDuplicateExternal:
    """T27b 建档 204「已存在」幂等标记：exists_external 登记后不再重试。"""

    def _install_duplicate_create(self, md_config, monkeypatch, calls):
        """建档 mock：客户建档返回 duplicate（TMS 已存在 204），其余成功。"""
        md_config(_default_cfg())

        async def _fake(forms_by_kind: dict[str, dict[str, dict[str, str]]], sk: str = ""):
            calls.append(forms_by_kind)
            results: dict = {}
            for kind, forms in forms_by_kind.items():
                results[kind] = {}
                for i, key in enumerate(forms):
                    if kind == KIND_CLIENT:
                        results[kind][key] = {
                            "success": False,
                            "archive_id": None,
                            "duplicate": True,
                            "error": {
                                "code": "master_data_duplicate",
                                "message": "客户名已存在,无法继续添加。",
                            },
                        }
                    else:
                        results[kind][key] = {
                            "success": True,
                            "archive_id": f"aid-{kind}-{i}",
                            "error": None,
                        }
            return results

        monkeypatch.setattr(md_client_module, "create_archives_async", _fake)

    async def test_duplicate_marks_external_and_skips_retry(self, md_config, monkeypatch, tmp_path):
        """204+已存在 → exists_external 单列 + store 标记；第二批不再建档。"""
        from app.orders.bill.master_data_store import reload_store

        store = reload_store(tmp_path / "md.json")
        calls: list[dict] = []
        self._install_duplicate_create(md_config, monkeypatch, calls)
        # 仅客户候选（无门点无司机，避免工厂依赖/司机链干扰断言）
        orders = [
            _make_order(customer="锦煦", door=None, address=None, driver=None) for _ in range(5)
        ]
        report = await run_master_data_async(orders, create_order=True)
        assert report["exists_external"] and report["exists_external"][0]["kind"] == KIND_CLIENT
        assert report["failed"] == []  # duplicate 不记 failed
        assert len(calls) == 1
        rec = store.get(KIND_CLIENT, client_key("锦煦"))
        assert rec and rec.get("exists_external") is True
        assert rec.get("archive_id") is None
        # 第二批：不再重试建档（store 已有 exists_external 标记）
        report2 = await run_master_data_async(orders, create_order=True)
        assert len(calls) == 1
        assert report2["exists_external"] == [] and report2["archived"] == []
        # 不列入 pending
        assert all(p["kind"] != KIND_CLIENT for p in (report2.get("pending_top") or []))
        # 订单不标注客户未建档（已存在外部，仅无 id；司机降级标注不影响）
        assert "客户「锦煦」未建档" not in (orders[0].unmapped_note or "")

    async def test_duplicate_requires_marker_match(self, md_config, monkeypatch, tmp_path):
        """非「已存在」语义的 204（其他 msg）→ 维持 failed + 下批重试。"""
        from app.orders.bill.master_data_store import reload_store

        reload_store(tmp_path / "md2.json")
        calls: list[dict] = []

        async def _fake(forms_by_kind: dict[str, dict[str, dict[str, str]]], sk: str = ""):
            calls.append(forms_by_kind)
            results: dict = {}
            for kind, forms in forms_by_kind.items():
                results[kind] = {}
                for key in forms:
                    results[kind][key] = {
                        "success": False,
                        "archive_id": None,
                        "error": {"code": "master_data_create_error", "message": "服务器繁忙"},
                    }
            return results

        monkeypatch.setattr(md_client_module, "create_archives_async", _fake)
        md_config(_default_cfg())
        orders = [_make_order(customer="锦煦", driver=None) for _ in range(5)]
        report = await run_master_data_async(orders, create_order=True)
        assert report["failed"] and report["exists_external"] == []
        assert len(calls) == 1
        # 下批重试（无 exists_external 标记）
        await run_master_data_async(orders, create_order=True)
        assert len(calls) == 2

    async def test_no_primary_key_marks_external(self, md_config, monkeypatch, tmp_path):
        """TMS 成功但无主键（AddCarFactory data:[] 实证，重复提交仍成功）→ 视为已建档
        无 id，登记 exists_external 不再重试（否则每次重试都会再建一条档案）。"""
        from app.orders.bill.master_data_store import reload_store

        reload_store(tmp_path / "md3.json")
        calls: list[dict] = []
        md_config(_default_cfg())

        async def _fake(forms_by_kind: dict[str, dict[str, dict[str, str]]], sk: str = ""):
            calls.append(forms_by_kind)
            results: dict = {}
            for kind, forms in forms_by_kind.items():
                results[kind] = {}
                for key in forms:
                    if kind == KIND_FACTORY:
                        results[kind][key] = {
                            "success": False,
                            "archive_id": None,
                            "no_id_created": True,
                            "error": {
                                "code": "master_data_no_primary_key",
                                "message": "已添加但响应未返回主键",
                            },
                        }
                    else:
                        results[kind][key] = {
                            "success": True,
                            "archive_id": f"aid-{kind}-0",
                            "error": None,
                        }
            return results

        monkeypatch.setattr(md_client_module, "create_archives_async", _fake)
        orders = [_make_order() for _ in range(5)]
        report = await run_master_data_async(orders, create_order=True)
        ext = [e for e in report["exists_external"] if e["kind"] == KIND_FACTORY]
        assert ext and "未返回主键" in ext[0]["message"]
        assert report["failed"] == []  # no_id 不记 failed
        rec = get_store().get(KIND_FACTORY, factory_key("上海仓", "浦东新区"))
        assert rec and rec.get("exists_external") is True and rec.get("archive_id") is None
        # 第二批：全终态（client/driver/truck 已建档、factory exists_external）→ 零新增调用
        calls_before = len(calls)
        await run_master_data_async(orders, create_order=True)
        assert len(calls) == calls_before

    async def test_duplicate_markers_configurable(self, md_config):
        """duplicate_markers 配置化：自定义匹配串生效。"""
        cfg = _default_cfg()
        cfg["duplicate_markers"] = ["已被占用"]
        md_config(cfg)
        assert md_client_module._is_duplicate_message("客户名已被占用") is True
        assert md_client_module._is_duplicate_message("客户名已存在") is False  # 未配置串不命中


class TestPreviewReadOnly:
    """preview（create_order=False）：只读探测——不计数不建档，报告当前计数状态。"""

    async def test_preview_does_not_count_or_archive(self, fake_create):
        fake_create()
        report = await run_master_data_async([_make_order()], create_order=False)
        assert report["mode"] == "preview"
        assert report["candidates"] == {KIND_CLIENT: 1, KIND_FACTORY: 1, KIND_DRIVER: 1}
        assert get_store().snapshot() == {}  # 零写入
        assert fake_create.calls == []  # 零建档调用

    async def test_preview_shows_current_pending(self, fake_create):
        fake_create()
        await run_master_data_async([_make_order()], create_order=True)  # 计数 1
        report = await run_master_data_async([_make_order()], create_order=False)
        pending = [p for p in report["pending_top"] if p["kind"] == KIND_CLIENT]
        assert pending and pending[0]["count"] == 1 and pending[0]["threshold"] == 5


class TestDisabled:
    """enabled: false → 全局关闭：无报告、零计数、零建档。"""

    async def test_disabled_returns_none(self, md_config):
        md_config({"enabled": False, "threshold": 5, "endpoints": {}})
        assert await run_master_data_async([_make_order()], create_order=True) is None
        assert get_store().snapshot() == {}


class TestCandidates:
    """候选收集：缺失字段不构成候选（无门点/无司机名跳过）；车牌缺失退化为按名。"""

    async def test_missing_fields_skipped(self):
        # 无客户/无门点/无司机名 → 三类候选全部不构成
        cands = collect_candidates([_make_order(customer=None, door=None, driver=None)])
        assert cands == []

    async def test_missing_address_or_plate_fallback(self):
        cands = collect_candidates([_make_order(address=None, plate=None)])
        kinds = {c.kind for c in cands}
        assert kinds == {KIND_CLIENT, KIND_FACTORY, KIND_DRIVER}


class TestGoldenIntegration:
    """golden 集成：家族文件走全管线（create，真实配置 5/6 端点已配 → 达阈值建档）
    → meta.master_data 报告段 + 订单级「未建档(x/N)」标注（验收 gate 3 语义）。"""

    @pytest.mark.skipif(
        not (FAMILIES_DIR / "junyu").exists(), reason="样本未入库（表格文件不入库）"
    )
    async def test_junyu_create_report(self, monkeypatch):
        path = FAMILIES_DIR / "junyu" / "2020-10上海军羽应收对账单.xls"
        if not path.exists():
            pytest.skip("junyu 样本缺失")
        # mock 下单通道（零网络）：AddWork 成功回显；建档族按 URL 回主键（sk 由调用方透传）
        from helpers import FakeResponse

        async def fake_post(url, **_kwargs):
            if "/Car/Car" in url:  # 建档族（/Car/Car* 路径；下单 AddWork 也在 s3.jxt56.com/Car/ 下，不能按 /Car/ 或 host 判断）
                pk = (
                    "client_id"
                    if "CarClient" in url
                    else "factory_id"
                    if "CarFactory" in url
                    else "truck_id"
                    if "CarTruck" in url
                    else "id"  # 司机主键是 id（§14）
                )
                return FakeResponse(
                    {"code": "200", "msg": "添加成功", "data": {pk: "aid-mock"}}
                )
            return FakeResponse({"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080042"}]})

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)
        result = await build_result_async(
            filename=path.name, file_bytes=path.read_bytes(), create_order=True, sk="sk"
        )
        report = result.meta.get("master_data")
        assert report is not None
        assert report["mode"] == "create"
        assert report["incremented"]  # 本批出现客户/工厂/司机候选并计数
        assert report["degraded"] == ["bailor"]  # 6/6 端点已配，仅委托人 TODO
        # 建档调用全成功（mock 主键）；仅允许两类非网络失败：工厂依赖前置（所属客户
        # 未达阈值，计数保留下批重试——设计语义，见 _create_one）与司机无车牌 skip
        assert all(
            "所属客户未建档" in f["reason"] or "无车牌" in f["reason"]
            for f in report["failed"]
        )
        # 达阈值候选建档成功并回填主键（junyu 2020-10 实测 13/13 客户/工厂类达阈值）
        assert report["archived"]
        assert all(a["archive_id"] for a in report["archived"])
        # 司机无车牌列（账单无车牌字段）→ 全部 skip（failed「无车牌」），不建档
        assert any("无车牌" in f["reason"] for f in report["failed"])
        # 订单级：未达阈值客户仍标注未建档状态（计数 x/5）
        assert any(
            o.unmapped_note and "未建档" in o.unmapped_note
            for o in result.canonical_orders
        )
