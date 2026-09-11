"""T24-T26 费目自举测试：四级解析顺序/懒创建/幂等/失败降级重试/环境开关/
is_other 特判/建档 form 构造/registry 持久化/service 管线集成/golden 集成。

- 四级解析顺序：YAML 显式 id → registry → 自举创建 → 降级 skip_report（YAML 优先）；
- 全部断言用配置注入（临时 yaml + 测试端点 + mock 建档），无费目名/price_id/
  class_id 硬编码（测试配置值只在本文件定义，非 TMS 真实值）；
- 建档调用一律 mock（零网络）；registry 由 conftest 全局隔离到临时目录。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

import app.core.http_client as http_client_module
import app.orders.bill.fees.fee_bootstrap as fb_module
import app.orders.bill.fees.fee_price_map as fp_module
import app.orders.bill.master_data.client as md_client_module
import app.orders.bill.master_data.config as md_cfg_module
from app.orders.bill import BoxGroup, CanonicalOrder, FeeItem
from app.orders.bill.fees.fee_bootstrap import (
    bootstrap_endpoint,
    build_price_form,
    run_fee_bootstrap_async,
)
from app.orders.bill.fees.fee_price_map import apply_price_map
from app.orders.bill.fees.fee_registry import get_fee_registry
from app.orders.bill.master_data.config import KIND_PRICE
from app.orders.bill.schema import BillOrder
from helpers import TEST_SK_OWNER, inject_price_map

pytestmark = pytest.mark.asyncio

# 旧版（补值前）真实表中无 id 的费目码全集：注入 None 锁定自举场景
_LEGACY_NULL_CODES = [
    "amend", "damage_box", "deduction", "drop_box", "inspect", "lift",
    "move", "other", "overdue", "overweight", "port_misc", "pre_inport",
    "tally", "waiting", "weigh", "yangshan",
]

FAMILIES_DIR = (
    Path(__file__).resolve().parent.parent.parent / "golden" / "bill" / "families"
)

# 注入的测试配置（值全部为测试值，非 TMS 真实值；断言只引用本文件常量）
BS_CFG = {
    "enabled": True,
    "endpoint_key": "price_create",
    "create_defaults": {
        "sn_prefix": "TST",
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

# 费目映射表测试体：已实证码 freight（owner_price_ids default 槽显式 id）+
# 待建码 waiting/yangshan + 其它费 other + 税金 tax。条目级 price_id 已废弃
# （owner 隔离）。
_FEE_MAP = {
    "freight": {"tms_name": "运费"},
    "waiting": {"tms_name": "待时费"},
    "yangshan": {"tms_name": "洋山费"},
    "other": {"tms_name": "其它费"},
    "tax": {"tms_name": "税金", "import": False},
}


def _fee_map_yaml(bootstrap: dict | None) -> str:
    """构造注入用映射表 YAML（费目条目 + 可选 fee_bootstrap 段）。

    测试直调无 sk → DEFAULT_OWNER 槽；freight 显式 id 同时配 default 与
    TEST_SK_OWNER（带 sk 用例同槽命中，保「已建档名跳过」语义）。
    """
    body = {code: dict(entry) for code, entry in _FEE_MAP.items()}
    body["owner_price_ids"] = {
        "default": {"freight": 820},
        TEST_SK_OWNER: {"freight": 820},
    }
    if bootstrap is not None:
        body["fee_bootstrap"] = bootstrap
    return yaml.safe_dump(body, allow_unicode=True, sort_keys=False)


@pytest.fixture()
def price_cfg(tmp_path, monkeypatch):
    """注入 fee_price_map 配置（临时文件）+ registry 初始内容；teardown 恢复真实缓存。"""
    real_path = fp_module.price_map_path

    def _set(body: str, *, registry: dict | None = None):
        path = tmp_path / "fee_price_map.test.yaml"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(fp_module, "price_map_path", lambda: path)
        fp_module.reload_price_map()
        fb_module.reload_bootstrap_config()
        for code, rec in (registry or {}).items():
            get_fee_registry().register(code, rec["price_id"], rec.get("tms_name"))

    yield _set
    # 先还原路径再重载（monkeypatch 撤销在 fixture teardown 之后，需手动还原防残留缓存）
    fp_module.price_map_path = real_path
    fp_module.reload_price_map()
    fb_module.reload_bootstrap_config()


@pytest.fixture()
def md_endpoint(tmp_path, monkeypatch):
    """注入 master_data endpoints（price_create 测试端点）；teardown 恢复真实配置缓存。"""
    real_path = md_cfg_module._CONFIG_PATH

    def _set(endpoint: str = "http://jxt.test/Create/Price"):
        path = tmp_path / "master_data.yaml"
        cfg = {"master_data": {"enabled": False, "endpoints": {"price_create": endpoint}}}
        path.write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(md_cfg_module, "_CONFIG_PATH", path)
        md_cfg_module.reload_config()

    yield _set
    md_cfg_module._CONFIG_PATH = real_path
    md_cfg_module.reload_config()


@pytest.fixture()
def fake_create(monkeypatch):
    """建档调用 mock：记录每次调用（forms），默认全部成功返回递增数字 price_id。"""
    calls: list[dict] = []

    def _install(*, fail_codes: set[str] | None = None):
        async def _fake(forms_by_kind: dict[str, dict[str, dict[str, str]]], sk: str = ""):
            calls.append(forms_by_kind)
            results: dict = {}
            for kind, forms in forms_by_kind.items():
                results[kind] = {}
                for i, key in enumerate(forms):
                    if fail_codes and key in fail_codes:
                        results[kind][key] = {
                            "success": False,
                            "archive_id": None,
                            "error": {
                                "code": "master_data_create_error",
                                "message": "boom",
                            },
                        }
                    else:
                        results[kind][key] = {
                            "success": True,
                            "archive_id": f"{9000 + i}",
                            "error": None,
                        }
            return results

        monkeypatch.setattr(md_client_module, "create_archives_async", _fake)

    _install.calls = calls
    return _install


def _fee(
    code: str = "freight",
    money: str = "100.00",
    *,
    channel: str = "shou",
    excluded: bool = False,
    note: str | None = None,
) -> FeeItem:
    return FeeItem(
        channel=channel,
        code=code,
        money=Decimal(money),
        note=note,
        excluded=excluded,
    )


def _make_order(fees: list[FeeItem], bl_no: str = "BL00000001") -> CanonicalOrder:
    return CanonicalOrder(
        bl_no=bl_no,
        box_groups=[BoxGroup(b_type="40HQ", box_num=1)],
        fees=fees,
    )


class TestResolutionOrder:
    """T24 四级解析前两级：YAML 显式 id → registry（YAML 优先：人工修正压过自动产物）。"""

    async def test_yaml_beats_registry(self, price_cfg):
        """YAML 已实证 id 优先于 registry（registry 999 被忽略）。"""
        price_cfg(
            _fee_map_yaml(BS_CFG),
            registry={"freight": {"price_id": 999, "tms_name": "运费"}},
        )
        fee = _fee(code="freight")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].price_id == 820  # YAML 实证 id，非 registry 999
        assert dropped == []

    async def test_registry_fallback_when_yaml_null(self, price_cfg):
        """YAML null → registry 兜底（自举产物），正常回填不降级。"""
        price_cfg(
            _fee_map_yaml(BS_CFG),
            registry={"yangshan": {"price_id": 124900, "tms_name": "洋山费"}},
        )
        fee = _fee(code="yangshan")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].price_id == 124900
        assert updated[0].tms_name == "洋山费"
        assert dropped == []


class TestLazyCreate:
    """T25 懒创建：只建「真实导入中命中且解析为 null」的码；无缺失 → 零调用。"""

    async def test_only_missing_codes_bootstrapped(self, price_cfg, md_endpoint, fake_create):
        """已实证码（freight）不建档；缺失码逐码建档（同批同码一次、凭证一次）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [
            _make_order([_fee(code="freight"), _fee(code="waiting", money="50.00")]),
            _make_order([_fee(code="yangshan", money="10.00")], bl_no="BL00000002"),
        ]
        report = await run_fee_bootstrap_async(orders, create_order=True)
        assert [c["code"] for c in report["created"]] == ["waiting", "yangshan"]
        assert report["failed"] == []
        assert len(fake_create.calls) == 1  # 凭证一次
        forms = fake_create.calls[0][KIND_PRICE]
        assert set(forms) == {"waiting", "yangshan"}  # freight 有 id 不建档
        assert forms["waiting"]["name"] == "待时费"
        assert forms["yangshan"]["sn"] == "TST_YANGSHAN"
        # 建档即登记 registry（当批回填由 apply_price_map 命中）
        assert get_fee_registry().lookup("waiting")["price_id"] == 9000

    async def test_no_missing_returns_none(self, price_cfg, md_endpoint, fake_create):
        """全码有 id → 不建档、无报告段。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="freight")])]
        assert await run_fee_bootstrap_async(orders, create_order=True) is None
        assert fake_create.calls == []

    async def test_tax_excluded_not_bootstrapped(self, price_cfg, md_endpoint, fake_create):
        """import:false（税金）不建档（仅对账，price_id 非必需）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="tax", money="4.00", excluded=True)])]
        assert await run_fee_bootstrap_async(orders, create_order=True) is None
        assert fake_create.calls == []


class TestIdempotent:
    """T25 幂等：registry 命中即复用；二次导入零建档调用；同批同码只调一次。"""

    async def test_second_batch_no_create_calls(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        report = await run_fee_bootstrap_async(orders, create_order=True)
        assert report["created"][0]["price_id"] == 9000
        # 二次导入同码：registry 命中 → 零建档调用、无报告段
        assert await run_fee_bootstrap_async(orders, create_order=True) is None
        assert len(fake_create.calls) == 1
        # 当批回填：apply_price_map 经 registry 命中正常录入（不降级）
        fee = _fee(code="waiting", money="50.00")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].price_id == 9000 and updated[0].excluded is False
        assert dropped == []

    async def test_same_code_once_per_batch(self, price_cfg, md_endpoint, fake_create):
        """同批多单同码 → 建档调用只一次。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="waiting", money="10.00")]) for _ in range(3)]
        report = await run_fee_bootstrap_async(orders, create_order=True)
        assert len(report["created"]) == 1
        assert len(fake_create.calls[0][KIND_PRICE]) == 1

    async def test_registry_persists_across_reload(self, price_cfg, md_endpoint, fake_create, tmp_path):
        """registry 持久化：进程重启（新实例）登记不丢（幂等跨批次/跨进程）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        await run_fee_bootstrap_async([_make_order([_fee(code="waiting")])], create_order=True)
        from app.orders.bill.fees.fee_registry import FeeRegistry

        restarted = FeeRegistry(tmp_path / "fee_registry.json")
        rec = restarted.lookup("waiting")
        assert rec is not None and rec["price_id"] == 9000
        assert rec["tms_name"] == "待时费"


class TestFailureRetry:
    """T25 失败降级 + 下批重试：registry 不记失败；当批该码全部降级（现状语义）。"""

    async def test_failed_downgrades_and_retries_next_batch(
        self, price_cfg, md_endpoint, fake_create
    ):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create(fail_codes={"waiting"})
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        report = await run_fee_bootstrap_async(orders, create_order=True)
        assert report["created"] == []
        assert report["failed"] == [
            {
                "code": "waiting",
                "tms_name": "待时费",
                "reason": "master_data_create_error：boom",
            }
        ]
        # registry 不记失败 → 下批重试
        assert get_fee_registry().lookup("waiting") is None
        # 当批降级：apply_price_map 仍走现状语义（excluded + dropped，不抛断）
        fee = _fee(code="waiting", money="50.00")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].excluded is True
        assert dropped == [
            {
                "channel": "shou",
                "code": "waiting",
                "tms_name": "待时费",
                "money": "50.00",
                "note": None,
                "reason": "price_id null",
            }
        ]
        # 下批：建档成功 → 正常录入
        fake_create()
        report2 = await run_fee_bootstrap_async(orders, create_order=True)
        assert [c["code"] for c in report2["created"]] == ["waiting"]
        assert len(fake_create.calls) == 2


class TestDisabled:
    """T24 环境开关：enabled=false → 不自举（全降级），零建档调用。"""

    async def test_disabled_no_bootstrap(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml({**BS_CFG, "enabled": False}))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        assert await run_fee_bootstrap_async(orders, create_order=True) is None
        assert fake_create.calls == []
        fee = _fee(code="waiting", money="50.00")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].excluded is True
        assert dropped[0]["reason"] == "price_id null"


class TestEndpointMissing:
    """端点未配（TODO）→ 不自举（全降级），不 fail fast。"""

    async def test_todo_endpoint_no_bootstrap(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint(endpoint="TODO")
        fake_create()
        assert bootstrap_endpoint() is None
        assert await run_fee_bootstrap_async([_make_order([_fee(code="waiting")])], create_order=True) is None
        assert fake_create.calls == []


class TestPreviewReadOnly:
    """T25 preview 零副作用（与阶段三 preview 只读语义一致）：create_order=false
    只输出 planned 计划创建清单——不发建档请求、不查端点、不写 registry；
    费用仍走现状降级语义（preview 不建档则 dropped 非空）。"""

    async def test_preview_planned_only_no_requests(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        report = await run_fee_bootstrap_async(orders, create_order=False)
        assert report["mode"] == "preview"
        assert report["planned"] == [{"code": "waiting", "tms_name": "待时费"}]
        assert report["created"] == [] and report["failed"] == []
        assert fake_create.calls == []  # 零建档请求
        assert get_fee_registry().snapshot() == {}  # 零 registry 写入

    async def test_preview_without_sk_ignores_default_slot(
        self, price_cfg, md_endpoint, fake_create
    ):
        """preview 无 sk → 无归属：default 测试槽的显式 id 不再命中（保守全列）。

        2026-09-10 修复回归：此前无 sk preview 落 DEFAULT_OWNER 槽（注入表显式段
        有 freight 820）→ 输出「无需建档」的乐观假象，与带 sk 创建不一致；
        现与 master_data preview 无 sk 口径统一（不判定账号归属）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="freight", money="100.00")])]
        report = await run_fee_bootstrap_async(orders, create_order=False)
        assert report["mode"] == "preview"
        assert report["planned"] == [{"code": "freight", "tms_name": "运费"}]
        assert report["created"] == [] and report["failed"] == []
        assert fake_create.calls == []

    async def test_preview_keeps_downgrade_semantics(self, price_cfg, md_endpoint, fake_create):
        """preview 不建档 → apply_price_map 仍按现状降级（dropped 非空）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        report = await run_fee_bootstrap_async(
            [_make_order([_fee(code="waiting", money="50.00")])],
            create_order=False,
        )
        assert report["planned"]
        fee = _fee(code="waiting", money="50.00")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].excluded is True
        assert dropped[0]["reason"] == "price_id null"

    async def test_preview_does_not_require_endpoint(self, price_cfg, md_endpoint, fake_create):
        """preview 不查端点：endpoint TODO 也输出 planned（提示待建码，零副作用）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint(endpoint="TODO")
        fake_create()
        report = await run_fee_bootstrap_async(
            [_make_order([_fee(code="waiting")])], create_order=False
        )
        assert report is not None and report["planned"]
        assert fake_create.calls == []


class TestDuplicateExternal:
    """T27b 费目自举 204「已存在」：registry 登记 exists_external 不再重试。"""

    async def test_duplicate_marks_external_and_skips_retry(
        self, price_cfg, md_endpoint, monkeypatch
    ):
        """204+已存在 → exists_external 单列 + registry 标记；下批不再建档。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        calls: list[dict] = []

        async def _fake(forms_by_kind, sk: str = ""):
            calls.append(forms_by_kind)
            results: dict = {}
            for kind, forms in forms_by_kind.items():
                results[kind] = {}
                for key in forms:
                    results[kind][key] = {
                        "success": False,
                        "archive_id": None,
                        "duplicate": True,
                        "error": {"code": "master_data_duplicate", "message": "价格已存在,无法继续添加。"},
                    }
            return results

        monkeypatch.setattr(md_client_module, "create_archives_async", _fake)
        orders = [_make_order([_fee(code="waiting")])]
        report = await run_fee_bootstrap_async(orders, create_order=True)
        assert report["exists_external"] and report["exists_external"][0]["code"] == "waiting"
        assert report["failed"] == [] and report["created"] == []
        assert len(calls) == 1
        assert get_fee_registry().exists_external("waiting") is True
        assert get_fee_registry().lookup("waiting") is None  # price_id 保持 null（无查询接口）
        # 下批：不再重试建档
        report2 = await run_fee_bootstrap_async(orders, create_order=True)
        assert len(calls) == 1
        assert report2 is None or report2.get("exists_external") == []

    async def test_duplicate_invalid_price_id_falls_back_external(
        self, price_cfg, md_endpoint, monkeypatch
    ):
        """204 回传主键但非数字 → 不抛异常，退回 exists_external 终态（防异常穿透整请求）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()

        async def _fake(forms_by_kind, sk: str = ""):
            return {
                kind: {
                    key: {
                        "success": False,
                        "archive_id": "not-a-number",
                        "duplicate": True,
                        "error": {
                            "code": "master_data_duplicate",
                            "message": "价格已存在,无法继续添加。",
                        },
                    }
                    for key in forms
                }
                for kind, forms in forms_by_kind.items()
            }

        monkeypatch.setattr(md_client_module, "create_archives_async", _fake)
        report = await run_fee_bootstrap_async(
            [_make_order([_fee(code="waiting")])], create_order=True
        )
        # 非法主键不入 registry，维持 exists_external 终态（处理未抛异常）
        assert report["exists_external"] and report["exists_external"][0]["code"] == "waiting"
        assert "price_id" not in report["exists_external"][0]
        assert get_fee_registry().lookup("waiting") is None
        assert get_fee_registry().exists_external("waiting") is True

    async def test_duplicate_non_marker_keeps_failed(self, price_cfg, md_endpoint, monkeypatch):
        """非「已存在」语义拒单 → 维持 failed + registry 不记 → 下批重试。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        calls: list[dict] = []

        async def _fake(forms_by_kind, sk: str = ""):
            calls.append(forms_by_kind)
            results: dict = {}
            for kind, forms in forms_by_kind.items():
                results[kind] = {}
                for key in forms:
                    results[kind][key] = {
                        "success": False,
                        "archive_id": None,
                        "error": {"code": "master_data_create_error", "message": "参数错误"},
                    }
            return results

        monkeypatch.setattr(md_client_module, "create_archives_async", _fake)
        orders = [_make_order([_fee(code="waiting")])]
        report = await run_fee_bootstrap_async(orders, create_order=True)
        assert report["failed"] and report["exists_external"] == []
        assert get_fee_registry().exists_external("waiting") is False
        assert len(calls) == 1
        await run_fee_bootstrap_async(orders, create_order=True)
        assert len(calls) == 2  # 下批重试


class TestIsOther:
    """T25 「其它费」特判：other 码建档额外发 is_other=1；失败进报告不抛断。"""

    async def test_other_form_carries_is_other(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="other", money="6.00", note="高速费")])]
        report = await run_fee_bootstrap_async(orders, create_order=True)
        assert [c["code"] for c in report["created"]] == ["other"]
        form = fake_create.calls[0][KIND_PRICE]["other"]
        assert form["is_other"] == "1"
        assert form["name"] == "其它费"
        assert form["sn"] == "TST_OTHER"
        # create_defaults 全量发射（配置注入值，非硬编码）
        assert form["class_id"] == "4612" and form["classification_name"] == "运费"

    async def test_other_failure_reported(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create(fail_codes={"other"})
        orders = [_make_order([_fee(code="other", money="6.00", note="高速费")])]
        report = await run_fee_bootstrap_async(orders, create_order=True)
        assert report["created"] == []
        assert [f["code"] for f in report["failed"]] == ["other"]


class TestForm:
    """建档 form 构造：YAML 布尔（on/off）按 TMS checkbox 语义归一。"""

    async def test_boolean_on_normalized(self, price_cfg):
        """真实配置文件形态：is_get: on 会被 YAML 1.1 解析为布尔 True → 发射 "on"。"""
        body = _fee_map_yaml(
            {
                "enabled": True,
                "endpoint_key": "price_create",
                "create_defaults": {"sn_prefix": "TST", "is_get": "on", "is_pay": "on"},
            }
        ).replace("'on'", "on")
        price_cfg(body)
        form = build_price_form("yangshan", "洋山费")
        assert form["is_get"] == "on" and form["is_pay"] == "on"
        assert form["sn"] == "TST_YANGSHAN" and form["name"] == "洋山费"
        assert "class_id" not in form  # 未配置键不发送

    async def test_false_value_omitted(self, price_cfg):
        """False（off）→ 省略键（不发送）。"""
        body = _fee_map_yaml(
            {
                **BS_CFG,
                "create_defaults": {**BS_CFG["create_defaults"], "is_pay": False},
            }
        )
        price_cfg(body)
        form = build_price_form("yangshan", "洋山费")
        assert "is_pay" not in form
        assert form["is_get"] == "on"

    async def test_sn_prefix_default(self, price_cfg):
        """sn_prefix 未配置 → 默认 AUTO（配置缺省，非费目硬编码）。"""
        body = _fee_map_yaml(
            {"enabled": True, "endpoint_key": "price_create", "create_defaults": {}}
        )
        price_cfg(body)
        form = build_price_form("yangshan", "洋山费")
        assert form["sn"] == "AUTO_YANGSHAN"


class TestRegistryStore:
    """T24 registry 存储：损坏文件容错 / 缺 price_id 不算登记。"""

    async def test_corrupted_file_ignored(self, tmp_path):
        from app.orders.bill.fees.fee_registry import FeeRegistry

        path = tmp_path / "fee_registry.json"
        path.write_text("{broken json", encoding="utf-8")
        store = FeeRegistry(path)
        assert store.lookup("waiting") is None
        assert store.snapshot() == {}

    async def test_lookup_requires_price_id(self, tmp_path):
        from app.orders.bill.fees.fee_registry import FeeRegistry

        path = tmp_path / "fee_registry.json"
        path.write_text(
            '{"waiting": {"price_id": null, "tms_name": "待时费"}}',
            encoding="utf-8",
        )
        store = FeeRegistry(path)
        assert store.lookup("waiting") is None  # 无 price_id 不算登记


class TestServicePipeline:
    """service 管线集成：自举 → registry 登记 → apply 回填 → payload 发射（不降级）。"""

    async def test_pipeline_backfills_and_emits(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        from app.orders.bill.aggregation.canonical_aggregator import group_canonical
        from app.orders.bill.submission.payload import build_order_payload

        rows = [
            {
                "bl_no": "BL12345678",
                "box_type_qty": [{"type": "40HQ", "qty": 1}],
                "_fees": [
                    {
                        "section": "应收",
                        "name": "洋山费",
                        "channel": "shou",
                        "code": "yangshan",
                        "import": True,
                        "reconcile": True,
                        "money": 100.0,
                    }
                ],
                "_anchors": {"应收": {"小计": 100.0}},
            }
        ]
        template = {
            "fees": {
                "channels": {"应收": "shou"},
                "mapping": {"应收.洋山费": "yangshan"},
                "unmapped_fee": "to_other",
            }
        }
        orders = group_canonical(rows, template, None)
        report = await run_fee_bootstrap_async(orders, create_order=True)
        assert [c["code"] for c in report["created"]] == ["yangshan"]
        # 当批回填 + payload 发射（自举产物 price_id 正常录入）
        fee = orders[0].fees[0]
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].price_id == 9000 and dropped == []
        form, _ = build_order_payload(orders[0])
        assert form["shou[0][洋山费][price_id]"] == "9000"
        assert form["driver[0][get_ys_zj]"] == "100.00"


class TestGoldenBootstrap:
    """golden 集成：志驿 2020 全量——preview 零副作用（planned 清单）/ create 真实
    建档后 dropped 归零（mock 建档固定 id）。

    用志驿 2020 样本（无缺提单号行、无非法箱型——提单号缺失文件级
    连坐拍板后，自举/建档集成改用干净样本；缺行家族样本（秋怡 2017 等）已整批
    拒绝语义，不承载自举场景）。
    使用仓库真实配置（test 环境 enabled=true + 全量码映射）；建档调用由 conftest
    全局 mock（零网络，成功返回递增 id）；create 用例另 mock 下单链路（零网络） 。
    """

    @pytest.mark.skipif(
        not (FAMILIES_DIR / "zhiyi").exists(), reason="样本未入库（表格文件不入 库）"
    )
    async def test_zhiyi_preview_planned_only(self, monkeypatch):
        """preview：只输出 planned 清单（零副作用）——dropped 保持现状（非零）。

        注入旧版 null 费目（真实表已全量补 id）以触发自举场景。
        """
        inject_price_map(monkeypatch, {code: None for code in _LEGACY_NULL_CODES})
        path = FAMILIES_DIR / "zhiyi" / "志驿2020对账单.xls"
        if not path.exists():
            pytest.skip("志驿 2020 样本缺失")
        from app.orders.bill import build_result_async

        result = await build_result_async(filename=path.name, file_bytes=path.read_bytes())
        reports = result.meta["reconciliation"]["reports"]
        bootstrap = reports.get("fee_bootstrap")
        assert bootstrap is not None
        assert bootstrap["mode"] == "preview"
        assert bootstrap["planned"] and bootstrap["created"] == []
        assert reports.get("price_null_dropped")  # 现状：未建档 → 降级清单非空
        assert get_fee_registry().snapshot() == {}  # 零 registry 写入

    @pytest.mark.skipif(
        not (FAMILIES_DIR / "zhiyi").exists(), reason="样本未入库（表格文件不入 库）"
    )
    async def test_zhiyi_create_dropped_to_zero(self, monkeypatch):
        """create（真实导入）：建档成功 → dropped 归零 + 费用全部回填。

        志驿 2020 无缺提单号行（连坐口径下全批可录）；注入旧版 null
        费目（真实表已全量补 id）以触发自举场景。
        """
        inject_price_map(monkeypatch, {code: None for code in _LEGACY_NULL_CODES})
        path = FAMILIES_DIR / "zhiyi" / "志驿2020对账单.xls"
        if not path.exists():
            pytest.skip("志驿 2020 样本缺失")
        from helpers import FakeResponse

        async def fake_post(url, **_kwargs):
            return FakeResponse(
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080042"}]}
            )

        monkeypatch.setattr(http_client_module, "_post_async", fake_post)
        from app.orders.bill import build_result_async

        result = await build_result_async(
            filename=path.name, file_bytes=path.read_bytes(), create_order=True, sk="sk"
        )
        reports = result.meta["reconciliation"]["reports"]
        bootstrap = reports.get("fee_bootstrap")
        assert bootstrap is not None and bootstrap["mode"] == "create"
        assert bootstrap["failed"] == []
        assert bootstrap["created"]  # 志驿 mapping 缺码（other/waiting/yangshan 等）自举建档
        assert reports.get("price_null_dropped") == []  # 注入 null 费目降级 → 0
        # 全量费用回填：非 excluded 项 price_id 均非空；样本无缺提单号行（全批可录）
        null_prices = [
            (o.bl_no, f.code)
            for o in result.canonical_orders
            if o.bl_no
            for f in o.fees
            if not f.excluded and f.price_id is None
        ]
        assert null_prices == []
        assert not [o for o in result.canonical_orders if not o.bl_no]  # 干净样本断言
        # registry 与建档清单一致（登记即命中；带 sk 建档登记在 sk 槽）
        registered = {
            c["code"]: c["price_id"]
            for c in bootstrap["created"]
            if get_fee_registry().lookup(c["code"], TEST_SK_OWNER) is not None
        }
        assert registered == {c["code"]: c["price_id"] for c in bootstrap["created"]}


def _make_billrow_order(order_num1: str, fee_names: list[str]) -> BillOrder:
    """构造 BillRow 链订单（order_data.shou 中文名直传形态）。"""
    return BillOrder(
        order_num1=order_num1,
        c_title="测试客户",
        order_data={
            "shou": [{name: {"money": 100.0}} for name in fee_names],
            "box": [{"b_type": "40HQ", "box_num": 1}],
        },
    )


class TestBillrowNamedBootstrap:
    """BillRow 链（jinxin 直传名）模板外费用建档（用户拍板）：

    - 候选 = shou 名中无已建档档案者（运费等别名命中且 registry/YAML 有 id → 跳过）；
    - 模板外新名（加班费等）→ 动态码（x+sha1 前 8）+ tms_name=原名建档；
    - preview 零副作用（planned 清单，不发请求）；建档失败不阻塞（仅报告）；
    - 幂等：registry 登记后同批/跨批不再建档。
    """

    async def test_preview_planned_only_no_requests(self, price_cfg, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        fake_create()
        orders = [
            _make_billrow_order("BL001", ["运费", "加班费", "报关费", "运费"]),
            _make_billrow_order("BL002", ["报关费", "查验费"]),
        ]
        report = await fb_module.run_billrow_fee_bootstrap_async(
            orders, create_order=False, sk="sk"
        )
        assert report is not None and report["mode"] == "preview"
        # 运费别名命中且已有 price_id(820) → 跳过；3 新名 → planned（动态码 + 原名）
        planned = {p["tms_name"]: p["code"] for p in report["planned"]}
        assert set(planned) == {"加班费", "报关费", "查验费"}
        for code in planned.values():
            assert code.startswith("x") and len(code) == 9
        assert fake_create.calls == []  # 零请求
        assert report["created"] == [] and report["failed"] == []

    async def test_create_archives_dynamic_names_and_registers(
        self, price_cfg, md_endpoint, fake_create
    ):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_billrow_order("BL001", ["加班费", "报关费"])]
        report = await fb_module.run_billrow_fee_bootstrap_async(
            orders, create_order=True, sk="sk"
        )
        assert report is not None and report["mode"] == "create"
        created = {c["tms_name"]: c for c in report["created"]}
        assert set(created) == {"加班费", "报关费"}
        # 表单：name=原名 + sn 含动态码大写；registry 已登记（幂等命中，sk 槽）
        form_sent = fake_create.calls[0]["price"][created["加班费"]["code"]]
        assert form_sent["name"] == "加班费"
        assert created["加班费"]["code"].upper() in form_sent["sn"]
        assert get_fee_registry().lookup(created["加班费"]["code"], TEST_SK_OWNER)["tms_name"] == "加班费"
        assert get_fee_registry().lookup(created["报关费"]["code"], TEST_SK_OWNER)["tms_name"] == "报关费"

    async def test_second_batch_no_create_calls(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        await fb_module.run_billrow_fee_bootstrap_async(
            [_make_billrow_order("BL001", ["加班费"])], create_order=True, sk="sk"
        )
        assert len(fake_create.calls) == 1
        # 同批重复名一次；跨批已登记（registry 幂等）→ 无缺失（None 不产生报告段）
        again = await fb_module.run_billrow_fee_bootstrap_async(
            [_make_billrow_order("BL002", ["加班费"])],
            create_order=True,
            sk="sk",
        )
        assert again is None  # 全部已建档 → 无候选
        assert len(fake_create.calls) == 1  # 未再发建档

    async def test_duplicate_marks_external(self, price_cfg, md_endpoint, monkeypatch):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()

        async def _dup(forms_by_kind, sk=""):
            return {
                kind: {
                    key: {
                        "success": False,
                        "archive_id": None,
                        "error": {"code": "master_data_duplicate", "message": "已存在"},
                        "duplicate": True,
                    }
                    for key in forms
                }
                for kind, forms in forms_by_kind.items()
            }

        monkeypatch.setattr(md_client_module, "create_archives_async", _dup)
        report = await fb_module.run_billrow_fee_bootstrap_async(
            [_make_billrow_order("BL001", ["加班费"])], create_order=True, sk="sk"
        )
        assert report["exists_external"] and report["exists_external"][0]["tms_name"] == "加班费"
        assert report["created"] == [] and report["failed"] == []
        # 登记 exists_external → 下批不再重试
        assert await fb_module.run_billrow_fee_bootstrap_async(
            [_make_billrow_order("BL002", ["加班费"])], create_order=True, sk="sk"
        ) is None

    async def test_failed_does_not_block_and_retries_next_batch(
        self, price_cfg, md_endpoint, fake_create
    ):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create(fail_codes={fb_module._dynamic_fee_code("加班费")})
        report = await fb_module.run_billrow_fee_bootstrap_async(
            [_make_billrow_order("BL001", ["加班费", "报关费"])],
            create_order=True,
            sk="sk",
        )
        assert {f["tms_name"] for f in report["failed"]} == {"加班费"}
        assert {c["tms_name"] for c in report["created"]} == {"报关费"}
        # 失败不登记 → 下批重试仍建档（mock 修复后成功）
        fake_create(fail_codes=None)
        again = await fb_module.run_billrow_fee_bootstrap_async(
            [_make_billrow_order("BL002", ["加班费"])], create_order=True, sk="sk"
        )
        assert again["created"] and again["created"][0]["tms_name"] == "加班费"


class TestDynamicFallbackToOther:
    """模板外动态码建档失败/未触发的降级保底（拍板：不丢费不
    excluded，归并本通道其它费以其它费名义录入——与旧版 to_other 等价；
    registry 建档成功后下次上传自动转独立发射）。"""

    async def test_no_other_item_reroutes_to_other(self, price_cfg):
        """本通道无其它费条目 → 动态码条目直接改道其它费发射。"""
        from decimal import Decimal

        from app.orders.bill.fees.fee_name_map import _dynamic_fee_code
        from app.orders.bill.schema import FeeItem

        price_cfg(_fee_map_yaml(None))
        get_fee_registry().register("other", 90002, "其它费")  # 其它费档案已建档（保底归并前提）
        fee = FeeItem(channel="shou", code=_dynamic_fee_code("高速费"), money=Decimal("6"), note="高速费")
        apply_price_map([fee])
        # 未建档（registry 空、YAML 无此码）→ 改道其它费：码/名/id 就位、不 excluded、金额不丢
        assert fee.code == "other"
        assert fee.price_id == 90002 and fee.note == "高速费"
        assert fee.excluded is False

    async def test_with_other_item_merges_money_and_note(self, price_cfg):
        """同通道已有其它费条目 → 金额/原名并入（动态条目自身不发射）。"""
        from decimal import Decimal

        from app.orders.bill.fees.fee_name_map import _dynamic_fee_code
        from app.orders.bill.schema import FeeItem

        price_cfg(_fee_map_yaml(None))
        get_fee_registry().register("other", 90002, "其它费")
        other = FeeItem(channel="shou", code="other", money=Decimal("10"), note=None)
        dyn = FeeItem(channel="shou", code=_dynamic_fee_code("高速费"), money=Decimal("6"), note="高速费")
        apply_price_map([other, dyn])
        assert other.price_id == 90002
        assert other.money == Decimal("16") and other.note == "高速费"
        assert dyn.excluded is True  # 金额已并入 other → 自身不重复发射

    async def test_merge_dropped_report_and_no_double_count(self, price_cfg):
        """归并保底只报告不丢费：dropped 带 merged_to_other 原金额与原名；桶条目
        金额=两者之和，动态条目 excluded+金额置零——发射/响应求和口径不双算
        （审查 S6 锁死）。"""
        from app.orders.bill.fees.fee_name_map import _dynamic_fee_code
        from app.orders.bill.schema import FeeItem

        price_cfg(_fee_map_yaml(None))
        get_fee_registry().register("other", 90002, "其它费")
        other = FeeItem(channel="shou", code="other", money=Decimal("10"), note=None)
        dyn = FeeItem(
            channel="shou", code=_dynamic_fee_code("高速费"),
            money=Decimal("6"), note="高速费",
        )
        _, dropped = apply_price_map([other, dyn])
        assert other.money == Decimal("16") and other.note == "高速费"
        assert dyn.excluded is True and dyn.money == Decimal("0")
        assert dropped == [
            {
                "channel": "shou",
                "code": _dynamic_fee_code("高速费"),
                "tms_name": None,
                "money": "6",
                "note": "高速费",
                "reason": "merged_to_other",
            }
        ]
        # 发射口径不双算：Σ 未 excluded 条目金额 == 合并前 Σ（10+6）
        assert sum(f.money for f in (other, dyn) if not f.excluded) == Decimal("16")

    async def test_reconcile_identity_kept_after_merge(self, price_cfg):
        """service 对账口径（S6 锁死）：merged_to_other 不调整 recorded→excluded
        ——金额已并入桶条目正常发射仍在 recorded 内，恒等 diff=0 不破。"""
        from app.orders.bill.fees.fee_name_map import _dynamic_fee_code
        from app.orders.bill.fees.fee_registry import DEFAULT_OWNER
        from app.orders.bill.fees.reconcile import reconcile_order_fees
        from app.orders.bill.schema import FeeReconcile

        price_cfg(_fee_map_yaml(None))
        get_fee_registry().register("other", 90002, "其它费")
        other = FeeItem(channel="shou", code="other", money=Decimal("10"), note=None)
        dyn = FeeItem(
            channel="shou", code=_dynamic_fee_code("高速费"),
            money=Decimal("6"), note="高速费",
        )
        order = _make_order([other, dyn])
        order.fee_reconcile = {
            "shou": FeeReconcile(
                bill_total=Decimal("16"), recorded_total=Decimal("16"),
                excluded_total=Decimal("0"),
            )
        }
        dropped, mismatch, _, stats = reconcile_order_fees([order], DEFAULT_OWNER)
        assert dropped and dropped[0]["reason"] == "merged_to_other"
        rec = order.fee_reconcile["shou"]
        assert rec.recorded_total == Decimal("16") and rec.excluded_total == Decimal("0")
        assert rec.ok is True and mismatch == []
        assert stats["shou"]["recorded_total"] == Decimal("16")

    async def test_other_missing_warning_covers_dynamic_codes(
        self, price_cfg, monkeypatch
    ):
        """其它费缺档显著告警（W2 修复）：纯动态码账单（无真其它费列）整批
        excluded 也触发——原实现按「未 excluded」事后过滤恒为空（死代码）。"""
        from app.orders.bill.fees.fee_name_map import _dynamic_fee_code
        from app.orders.bill.schema import FeeItem

        price_cfg(_fee_map_yaml(None))  # 无 other 显式 id、registry 空 → 其它费无档
        warnings = []
        monkeypatch.setattr(
            fp_module.log, "warning",
            lambda event, extra=None: warnings.append((event, extra)),
        )
        dyn = FeeItem(
            channel="shou", code=_dynamic_fee_code("高速费"),
            money=Decimal("6"), note="高速费",
        )
        _, dropped = apply_price_map([dyn])
        assert dyn.excluded is True
        assert dropped and dropped[0]["reason"] == "price_id null"
        assert warnings and warnings[0][0] == "fee_price_map_other_missing"
        assert _dynamic_fee_code("高速费") in warnings[0][1]["codes"]

    async def test_other_missing_silent_when_other_has_id(self, price_cfg, monkeypatch):
        """其它费有档（显式段/registry）→ 归并保底走通，不误报缺档告警。"""
        from app.orders.bill.fees.fee_name_map import _dynamic_fee_code
        from app.orders.bill.schema import FeeItem

        price_cfg(_fee_map_yaml(None))
        get_fee_registry().register("other", 90002, "其它费")
        warnings = []
        monkeypatch.setattr(
            fp_module.log, "warning",
            lambda event, extra=None: warnings.append((event, extra)),
        )
        dyn = FeeItem(
            channel="shou", code=_dynamic_fee_code("高速费"),
            money=Decimal("6"), note="高速费",
        )
        _, dropped = apply_price_map([dyn])
        assert dyn.code == "other" and dyn.price_id == 90002  # 无桶改道发射
        assert dropped == []
        assert warnings == []

    async def test_standard_code_without_yaml_not_merged(self, price_cfg):
        """别名字典标准码（如 crane 吊机费，YAML 刻意无条目）→ 维持旧语义：
        不并入其它费、不建档案，excluded+dropped 进对账报告（审查
        修复：归并保底仅限 is_dynamic_code 动态码）。"""
        from decimal import Decimal

        from app.orders.bill.schema import FeeItem

        price_cfg(_fee_map_yaml(None))
        get_fee_registry().register("other", 90002, "其它费")
        fee = FeeItem(channel="shou", code="crane", money=Decimal("30"), note=None)
        updated, dropped = apply_price_map([fee])
        assert fee.code == "crane"  # 不改道
        assert fee.excluded is True  # 不录入仅对账
        assert dropped and dropped[0]["reason"] == "price_id null"
        assert updated[0].note is None  # 标准码 note 恒 None

    async def test_collect_missing_skips_standard_without_yaml(self, price_cfg, md_endpoint):
        """crane 类标准码（YAML 无条目）不构成建档候选——避免 TMS 档案意外复活
        （_collect_missing 的 note 命名回退仅限动态码，审查修复）。"""

        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        report = await run_fee_bootstrap_async(
            [_make_order([_fee(code="crane", money="30.00")])],
            create_order=True,
            sk="sk",
        )
        assert report is None  # 无建档候选（resolve null 但非动态码 → 不收集）

    async def test_preview_planned_lists_dynamic_codes(self, price_cfg):
        """preview：动态码缺档进 planned 计划清单（零副作用，与建档后独立发射对应）。"""
        from app.orders.bill.fees.fee_name_map import _dynamic_fee_code

        price_cfg(_fee_map_yaml(BS_CFG))  # BS_CFG 开启自举（preview 只出计划零请求）
        report = await run_fee_bootstrap_async(
            [_make_order([_fee(code=_dynamic_fee_code("高速费"), money="6.00", note="高速费")])],
            create_order=False,
            sk="sk",
        )
        assert report is not None and report["mode"] == "preview"
        assert report["planned"] == [
            {"code": _dynamic_fee_code("高速费"), "tms_name": "高速费"}
        ]
        assert report["created"] == []


class TestNamedBootstrapExtraNames:
    """BillRow 链模板外费名建档 + extra_names 直传（后 canonical
    动态码建档并入 run_fee_bootstrap 提前闭环；本类锁定直传链建档/幂等/preview
    零副作用）。"""

    async def test_preview_planned_zero_side_effect(self, price_cfg, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        fake_create()
        report = await fb_module.run_billrow_fee_bootstrap_async(
            [], create_order=False, sk="sk", extra_names=["加班费", "报关费"]
        )
        assert report is not None and report["mode"] == "preview"
        planned = {p["tms_name"] for p in report["planned"]}
        assert planned == {"加班费", "报关费"}
        assert fake_create.calls == []  # 零请求

    async def test_create_archives_and_registry_idempotent(
        self, price_cfg, md_endpoint, fake_create
    ):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        report = await fb_module.run_billrow_fee_bootstrap_async(
            [], create_order=True, sk="sk", extra_names=["加班费", "报关费"]
        )
        assert report is not None and report["mode"] == "create"
        created = {c["tms_name"] for c in report["created"]}
        assert created == {"加班费", "报关费"}
        assert len(fake_create.calls) == 1  # 同批一次
        # 幂等：registry 登记后跨批不再建档（报告段不产生）
        again = await fb_module.run_billrow_fee_bootstrap_async(
            [], create_order=True, sk="sk", extra_names=["加班费", "报关费"]
        )
        assert again is None and len(fake_create.calls) == 1

    async def test_billrow_candidates_priority_on_same_name(
        self, price_cfg, md_endpoint, fake_create
    ):
        """双链同名去重：BillRow 候选优先（保序），extra_names 同名不重复建。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        report = await fb_module.run_billrow_fee_bootstrap_async(
            [_make_billrow_order("BL001", ["加班费"])],
            create_order=True,
            sk="sk",
            extra_names=["加班费", "查验费"],
        )
        planned_all = [c["tms_name"] for c in report["created"]]
        assert planned_all == ["加班费", "查验费"]  # 加班费只出现一次
        assert len(fake_create.calls) == 1


class TestOwnerIsolation:
    """owner 隔离（用户拍板：费目全链按 sk，废除首个触发者全局共享）。

    生产实证驱动：test1/test2 首个触发者建档后全局复用 price_id，其他账号
    订单费用挂别人名下档案（15599 订单挂 15478 档案）。锁定编排层三要素：
    双 sk 同码各自建档、A 撞名终态不拦 B、apply 回填不跨槽。"""

    async def test_same_code_both_sk_bootstrap_independently(
        self, price_cfg, md_endpoint, fake_create
    ):
        """同码双 sk 各自建档：B 不命中 A 的登记（各自 price_id、各自槽）。"""
        from app.orders.bill.submission.imported_registry import owner_key

        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        first = await run_fee_bootstrap_async(orders, create_order=True, sk="sk-a")
        second = await run_fee_bootstrap_async(orders, create_order=True, sk="sk-b")
        id_a = first["created"][0]["price_id"]
        id_b = second["created"][0]["price_id"]
        # B 槽空 → 不命中 A 的登记 → 二次建档（mock 固定回值，比对行为非值：
        # 两次建档调用 + 两槽各自登记；真实链路 price_id 由 TMS 递增分配）
        assert len(fake_create.calls) == 2
        reg = get_fee_registry()
        assert reg.lookup("waiting", owner_key("sk-a"))["price_id"] == id_a
        assert reg.lookup("waiting", owner_key("sk-b"))["price_id"] == id_b
        assert reg.lookup("waiting", owner_key("sk-a")) is not None
        assert id_a == 9000 and id_b == 9000  # mock 固定回值；隔离语义由两槽独立登记佐证

    async def test_exists_external_of_a_not_block_b(
        self, price_cfg, md_endpoint, fake_create, monkeypatch
    ):
        """A 撞名（204 已存在）终态只落 A 槽：B 上传同码照常自举建档。"""
        from app.orders.bill.submission.imported_registry import owner_key

        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()

        async def _dup(forms_by_kind, sk=""):
            return {
                kind: {
                    key: {
                        "success": False,
                        "archive_id": None,
                        "duplicate": True,
                        "error": {"code": "master_data_duplicate", "message": "已存在"},
                    }
                    for key in forms
                }
                for kind, forms in forms_by_kind.items()
            }

        monkeypatch.setattr(md_client_module, "create_archives_async", _dup)
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        blocked = await run_fee_bootstrap_async(orders, create_order=True, sk="sk-a")
        assert blocked["created"] == [] and blocked["exists_external"]
        # B 槽不受 A 的 exists_external 影响（无缺失判定按各自槽）
        fake_create()
        report_b = await run_fee_bootstrap_async(orders, create_order=True, sk="sk-b")
        assert report_b["created"] and report_b["created"][0]["code"] == "waiting"
        assert get_fee_registry().exists_external("waiting", owner_key("sk-a")) is True
        assert get_fee_registry().exists_external("waiting", owner_key("sk-b")) is False

    async def test_apply_price_map_scoped_to_owner(self, price_cfg):
        """apply 回填只查当前 owner 槽：A 单不挂 B 的 price_id。"""
        from app.orders.bill.submission.imported_registry import owner_key

        price_cfg(_fee_map_yaml(None))
        get_fee_registry().register("waiting", 90001, "待时费", owner_key("sk-a"))
        get_fee_registry().register("waiting", 91001, "待时费", owner_key("sk-b"))
        fee_a = _fee(code="waiting", money="50.00")
        apply_price_map([fee_a], owner_key("sk-a"))
        fee_b = _fee(code="waiting", money="50.00")
        apply_price_map([fee_b], owner_key("sk-b"))
        assert fee_a.price_id == 90001 and fee_b.price_id == 91001

    async def test_explicit_ids_scoped_to_owner(self, price_cfg):
        """显式段只对当前 owner 槽生效：A 槽配 waiting、B/default 槽不配 → 零跨槽
        零全局兜底（resolve_price_id 核心语义直测，防误写成全局兜底）。"""
        from app.orders.bill.submission.imported_registry import owner_key

        body = {code: dict(entry) for code, entry in _FEE_MAP.items()}
        body["owner_price_ids"] = {owner_key("sk-a"): {"waiting": 90001}}
        price_cfg(yaml.safe_dump(body, allow_unicode=True, sort_keys=False))
        owner_a, owner_b = owner_key("sk-a"), owner_key("sk-b")
        assert fp_module.resolve_price_id("waiting", owner_a) == 90001
        assert fp_module.resolve_price_id("waiting", owner_b) is None
        assert fp_module.resolve_price_id("waiting") is None  # default 槽不配
