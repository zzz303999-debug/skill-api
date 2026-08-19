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

import app.orders.bill.fee_bootstrap as fb_module
import app.orders.bill.fee_price_map as fp_module
import app.orders.bill.master_data as md_module
import app.orders.bill.master_data_client as md_client_module
from app.orders.bill import BoxGroup, CanonicalOrder, FeeItem
from app.orders.bill.fee_bootstrap import (
    bootstrap_endpoint,
    build_price_form,
    run_fee_bootstrap,
)
from app.orders.bill.fee_price_map import apply_price_map
from app.orders.bill.fee_registry import get_registry
from app.orders.bill.master_data import KIND_PRICE

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

# 费目映射表测试体：已实证码 freight + 待建码 waiting/yangshan + 其它费 other + 税金 tax
_FEE_MAP = {
    "freight": {"tms_name": "运费", "price_id": 820},
    "waiting": {"tms_name": "待时费", "price_id": None},
    "yangshan": {"tms_name": "洋山费", "price_id": None},
    "other": {"tms_name": "其它费", "price_id": None},
    "tax": {"tms_name": "税金", "price_id": None, "import": False},
}


def _fee_map_yaml(bootstrap: dict | None) -> str:
    """构造注入用映射表 YAML（费目条目 + 可选 fee_bootstrap 段）。"""
    body = {code: dict(entry) for code, entry in _FEE_MAP.items()}
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
            get_registry().register(code, rec["price_id"], rec.get("tms_name"))

    yield _set
    # 先还原路径再重载（monkeypatch 撤销在 fixture teardown 之后，需手动还原防残留缓存）
    fp_module.price_map_path = real_path
    fp_module.reload_price_map()
    fb_module.reload_bootstrap_config()


@pytest.fixture()
def md_endpoint(tmp_path, monkeypatch):
    """注入 master_data endpoints（price_create 测试端点）；teardown 恢复真实配置缓存。"""
    real_path = md_module._CONFIG_PATH

    def _set(endpoint: str = "http://jxt.test/Create/Price"):
        path = tmp_path / "master_data.yaml"
        cfg = {"master_data": {"enabled": False, "endpoints": {"price_create": endpoint}}}
        path.write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(md_module, "_CONFIG_PATH", path)
        md_module.reload_config()

    yield _set
    md_module._CONFIG_PATH = real_path
    md_module.reload_config()


@pytest.fixture()
def fake_create(monkeypatch):
    """建档调用 mock：记录每次调用（forms），默认全部成功返回递增数字 price_id。"""
    calls: list[dict] = []

    def _install(*, fail_codes: set[str] | None = None):
        def _fake(forms_by_kind: dict[str, dict[str, dict[str, str]]], sk: str = ""):
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

        monkeypatch.setattr(md_client_module, "create_archives", _fake)

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

    def test_yaml_beats_registry(self, price_cfg):
        """YAML 已实证 id 优先于 registry（registry 999 被忽略）。"""
        price_cfg(
            _fee_map_yaml(BS_CFG),
            registry={"freight": {"price_id": 999, "tms_name": "运费"}},
        )
        fee = _fee(code="freight")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].price_id == 820  # YAML 实证 id，非 registry 999
        assert dropped == []

    def test_registry_fallback_when_yaml_null(self, price_cfg):
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

    def test_only_missing_codes_bootstrapped(self, price_cfg, md_endpoint, fake_create):
        """已实证码（freight）不建档；缺失码逐码建档（同批同码一次、凭证一次）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [
            _make_order([_fee(code="freight"), _fee(code="waiting", money="50.00")]),
            _make_order([_fee(code="yangshan", money="10.00")], bl_no="BL00000002"),
        ]
        report = run_fee_bootstrap(orders, create_order=True)
        assert [c["code"] for c in report["created"]] == ["waiting", "yangshan"]
        assert report["failed"] == []
        assert len(fake_create.calls) == 1  # 凭证一次
        forms = fake_create.calls[0][KIND_PRICE]
        assert set(forms) == {"waiting", "yangshan"}  # freight 有 id 不建档
        assert forms["waiting"]["name"] == "待时费"
        assert forms["yangshan"]["sn"] == "TST_YANGSHAN"
        # 建档即登记 registry（当批回填由 apply_price_map 命中）
        assert get_registry().lookup("waiting")["price_id"] == 9000

    def test_no_missing_returns_none(self, price_cfg, md_endpoint, fake_create):
        """全码有 id → 不建档、无报告段。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="freight")])]
        assert run_fee_bootstrap(orders, create_order=True) is None
        assert fake_create.calls == []

    def test_tax_excluded_not_bootstrapped(self, price_cfg, md_endpoint, fake_create):
        """import:false（税金）不建档（仅对账，price_id 非必需）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="tax", money="4.00", excluded=True)])]
        assert run_fee_bootstrap(orders, create_order=True) is None
        assert fake_create.calls == []


class TestIdempotent:
    """T25 幂等：registry 命中即复用；二次导入零建档调用；同批同码只调一次。"""

    def test_second_batch_no_create_calls(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        report = run_fee_bootstrap(orders, create_order=True)
        assert report["created"][0]["price_id"] == 9000
        # 二次导入同码：registry 命中 → 零建档调用、无报告段
        assert run_fee_bootstrap(orders, create_order=True) is None
        assert len(fake_create.calls) == 1
        # 当批回填：apply_price_map 经 registry 命中正常录入（不降级）
        fee = _fee(code="waiting", money="50.00")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].price_id == 9000 and updated[0].excluded is False
        assert dropped == []

    def test_same_code_once_per_batch(self, price_cfg, md_endpoint, fake_create):
        """同批多单同码 → 建档调用只一次。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="waiting", money="10.00")]) for _ in range(3)]
        report = run_fee_bootstrap(orders, create_order=True)
        assert len(report["created"]) == 1
        assert len(fake_create.calls[0][KIND_PRICE]) == 1

    def test_registry_persists_across_reload(self, price_cfg, md_endpoint, fake_create, tmp_path):
        """registry 持久化：进程重启（新实例）登记不丢（幂等跨批次/跨进程）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        run_fee_bootstrap([_make_order([_fee(code="waiting")])], create_order=True)
        from app.orders.bill.fee_registry import FeeRegistry

        restarted = FeeRegistry(tmp_path / "fee_registry.json")
        rec = restarted.lookup("waiting")
        assert rec is not None and rec["price_id"] == 9000
        assert rec["tms_name"] == "待时费"


class TestFailureRetry:
    """T25 失败降级 + 下批重试：registry 不记失败；当批该码全部降级（现状语义）。"""

    def test_failed_downgrades_and_retries_next_batch(
        self, price_cfg, md_endpoint, fake_create
    ):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create(fail_codes={"waiting"})
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        report = run_fee_bootstrap(orders, create_order=True)
        assert report["created"] == []
        assert report["failed"] == [
            {
                "code": "waiting",
                "tms_name": "待时费",
                "reason": "master_data_create_error：boom",
            }
        ]
        # registry 不记失败 → 下批重试
        assert get_registry().lookup("waiting") is None
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
        report2 = run_fee_bootstrap(orders, create_order=True)
        assert [c["code"] for c in report2["created"]] == ["waiting"]
        assert len(fake_create.calls) == 2


class TestDisabled:
    """T24 环境开关：enabled=false → 不自举（全降级），零建档调用。"""

    def test_disabled_no_bootstrap(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml({**BS_CFG, "enabled": False}))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        assert run_fee_bootstrap(orders, create_order=True) is None
        assert fake_create.calls == []
        fee = _fee(code="waiting", money="50.00")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].excluded is True
        assert dropped[0]["reason"] == "price_id null"


class TestEndpointMissing:
    """端点未配（TODO）→ 不自举（全降级），不 fail fast。"""

    def test_todo_endpoint_no_bootstrap(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint(endpoint="TODO")
        fake_create()
        assert bootstrap_endpoint() is None
        assert run_fee_bootstrap([_make_order([_fee(code="waiting")])], create_order=True) is None
        assert fake_create.calls == []


class TestPreviewReadOnly:
    """T25 preview 零副作用（与阶段三 preview 只读语义一致）：create_order=false
    只输出 planned 计划创建清单——不发建档请求、不查端点、不写 registry；
    费用仍走现状降级语义（preview 不建档则 dropped 非空）。"""

    def test_preview_planned_only_no_requests(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="waiting", money="50.00")])]
        report = run_fee_bootstrap(orders, create_order=False)
        assert report["mode"] == "preview"
        assert report["planned"] == [{"code": "waiting", "tms_name": "待时费"}]
        assert report["created"] == [] and report["failed"] == []
        assert fake_create.calls == []  # 零建档请求
        assert get_registry().snapshot() == {}  # 零 registry 写入

    def test_preview_keeps_downgrade_semantics(self, price_cfg, md_endpoint, fake_create):
        """preview 不建档 → apply_price_map 仍按现状降级（dropped 非空）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        report = run_fee_bootstrap(
            [_make_order([_fee(code="waiting", money="50.00")])],
            create_order=False,
        )
        assert report["planned"]
        fee = _fee(code="waiting", money="50.00")
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].excluded is True
        assert dropped[0]["reason"] == "price_id null"

    def test_preview_does_not_require_endpoint(self, price_cfg, md_endpoint, fake_create):
        """preview 不查端点：endpoint TODO 也输出 planned（提示待建码，零副作用）。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint(endpoint="TODO")
        fake_create()
        report = run_fee_bootstrap(
            [_make_order([_fee(code="waiting")])], create_order=False
        )
        assert report is not None and report["planned"]
        assert fake_create.calls == []


class TestDuplicateExternal:
    """T27b 费目自举 204「已存在」：registry 登记 exists_external 不再重试。"""

    def test_duplicate_marks_external_and_skips_retry(
        self, price_cfg, md_endpoint, monkeypatch
    ):
        """204+已存在 → exists_external 单列 + registry 标记；下批不再建档。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        calls: list[dict] = []

        def _fake(forms_by_kind, sk: str = ""):
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

        monkeypatch.setattr(md_client_module, "create_archives", _fake)
        orders = [_make_order([_fee(code="waiting")])]
        report = run_fee_bootstrap(orders, create_order=True)
        assert report["exists_external"] and report["exists_external"][0]["code"] == "waiting"
        assert report["failed"] == [] and report["created"] == []
        assert len(calls) == 1
        assert get_registry().exists_external("waiting") is True
        assert get_registry().lookup("waiting") is None  # price_id 保持 null（无查询接口）
        # 下批：不再重试建档
        report2 = run_fee_bootstrap(orders, create_order=True)
        assert len(calls) == 1
        assert report2 is None or report2.get("exists_external") == []

    def test_duplicate_non_marker_keeps_failed(self, price_cfg, md_endpoint, monkeypatch):
        """非「已存在」语义拒单 → 维持 failed + registry 不记 → 下批重试。"""
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        calls: list[dict] = []

        def _fake(forms_by_kind, sk: str = ""):
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

        monkeypatch.setattr(md_client_module, "create_archives", _fake)
        orders = [_make_order([_fee(code="waiting")])]
        report = run_fee_bootstrap(orders, create_order=True)
        assert report["failed"] and report["exists_external"] == []
        assert get_registry().exists_external("waiting") is False
        assert len(calls) == 1
        run_fee_bootstrap(orders, create_order=True)
        assert len(calls) == 2  # 下批重试


class TestIsOther:
    """T25 「其它费」特判：other 码建档额外发 is_other=1；失败进报告不抛断。"""

    def test_other_form_carries_is_other(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        orders = [_make_order([_fee(code="other", money="6.00", note="高速费")])]
        report = run_fee_bootstrap(orders, create_order=True)
        assert [c["code"] for c in report["created"]] == ["other"]
        form = fake_create.calls[0][KIND_PRICE]["other"]
        assert form["is_other"] == "1"
        assert form["name"] == "其它费"
        assert form["sn"] == "TST_OTHER"
        # create_defaults 全量发射（配置注入值，非硬编码）
        assert form["class_id"] == "4612" and form["classification_name"] == "运费"

    def test_other_failure_reported(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create(fail_codes={"other"})
        orders = [_make_order([_fee(code="other", money="6.00", note="高速费")])]
        report = run_fee_bootstrap(orders, create_order=True)
        assert report["created"] == []
        assert [f["code"] for f in report["failed"]] == ["other"]


class TestForm:
    """建档 form 构造：YAML 布尔（on/off）按 TMS checkbox 语义归一。"""

    def test_boolean_on_normalized(self, price_cfg):
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

    def test_false_value_omitted(self, price_cfg):
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

    def test_sn_prefix_default(self, price_cfg):
        """sn_prefix 未配置 → 默认 AUTO（配置缺省，非费目硬编码）。"""
        body = _fee_map_yaml(
            {"enabled": True, "endpoint_key": "price_create", "create_defaults": {}}
        )
        price_cfg(body)
        form = build_price_form("yangshan", "洋山费")
        assert form["sn"] == "AUTO_YANGSHAN"


class TestRegistryStore:
    """T24 registry 存储：损坏文件容错 / 缺 price_id 不算登记。"""

    def test_corrupted_file_ignored(self, tmp_path):
        from app.orders.bill.fee_registry import FeeRegistry

        path = tmp_path / "fee_registry.json"
        path.write_text("{broken json", encoding="utf-8")
        store = FeeRegistry(path)
        assert store.lookup("waiting") is None
        assert store.snapshot() == {}

    def test_lookup_requires_price_id(self, tmp_path):
        from app.orders.bill.fee_registry import FeeRegistry

        path = tmp_path / "fee_registry.json"
        path.write_text(
            '{"waiting": {"price_id": null, "tms_name": "待时费"}}',
            encoding="utf-8",
        )
        store = FeeRegistry(path)
        assert store.lookup("waiting") is None  # 无 price_id 不算登记


class TestServicePipeline:
    """service 管线集成：自举 → registry 登记 → apply 回填 → payload 发射（不降级）。"""

    def test_pipeline_backfills_and_emits(self, price_cfg, md_endpoint, fake_create):
        price_cfg(_fee_map_yaml(BS_CFG))
        md_endpoint()
        fake_create()
        from app.orders.bill.aggregator import group_canonical
        from app.orders.bill.payload import build_order_payload

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
        report = run_fee_bootstrap(orders, create_order=True)
        assert [c["code"] for c in report["created"]] == ["yangshan"]
        # 当批回填 + payload 发射（自举产物 price_id 正常录入）
        fee = orders[0].fees[0]
        (updated, dropped) = apply_price_map([fee])
        assert updated[0].price_id == 9000 and dropped == []
        form, _ = build_order_payload(orders[0])
        assert form["shou[0][洋山费][price_id]"] == "9000"
        assert form["driver[0][get_ys_zj]"] == "100.00"


class TestGoldenBootstrap:
    """golden 集成：秋怡家族全量——preview 零副作用（planned 清单）/ create 真实
    建档后 dropped 归零（1901 → 0，mock 建档固定 id）。

    使用仓库真实配置（test 环境 enabled=true + 全量码映射）；建档调用由 conftest
    全局 mock（零网络，成功返回递增 id）；create 用例另 mock 下单链路（零网络）。
    """

    @pytest.mark.skipif(
        not (FAMILIES_DIR / "qiuyi").exists(), reason="样本未入库（表格文件不入库）"
    )
    def test_qiuyi_preview_planned_only(self):
        """preview：只输出 planned 清单（零副作用）——dropped 保持现状（非零）。"""
        path = FAMILIES_DIR / "qiuyi" / "2019-01到2019-12上海秋怡应收对账单.xls"
        if not path.exists():
            pytest.skip("秋怡 2019 样本缺失")
        from app.orders.bill import build_result

        result = build_result(filename=path.name, file_bytes=path.read_bytes())
        reports = result.meta["reconciliation"]["reports"]
        bootstrap = reports.get("fee_bootstrap")
        assert bootstrap is not None
        assert bootstrap["mode"] == "preview"
        assert bootstrap["planned"] and bootstrap["created"] == []
        assert reports.get("price_null_dropped")  # 现状：未建档 → 降级清单非空
        assert get_registry().snapshot() == {}  # 零 registry 写入

    @pytest.mark.skipif(
        not (FAMILIES_DIR / "qiuyi").exists(), reason="样本未入库（表格文件不入库）"
    )
    def test_qiuyi_create_dropped_to_zero(self, monkeypatch):
        """create（真实导入）：建档成功 → dropped 归零 + 费用全部回填（1901 → 0）。"""
        path = FAMILIES_DIR / "qiuyi" / "2019-01到2019-12上海秋怡应收对账单.xls"
        if not path.exists():
            pytest.skip("秋怡 2019 样本缺失")
        import app.orders.bill.client as client_module
        from helpers import FakeResponse

        def fake_post(url, **_kwargs):
            return FakeResponse(
                {"code": "200", "msg": "添加成功", "data": [{"sn": "EX26080042"}]}
            )

        monkeypatch.setattr(client_module.httpx, "post", fake_post)
        from app.orders.bill import build_result

        result = build_result(
            filename=path.name, file_bytes=path.read_bytes(), create_order=True
        )
        reports = result.meta["reconciliation"]["reports"]
        bootstrap = reports.get("fee_bootstrap")
        assert bootstrap is not None and bootstrap["mode"] == "create"
        assert bootstrap["failed"] == []
        assert bootstrap["created"]  # 实测 14 码：other/yangshan/pre_inport/drop_box 等
        assert reports.get("price_null_dropped") == []  # 1901 条降级 → 0
        # 全量费用回填：非 excluded 项 price_id 均非空（当批正常录入）
        null_prices = [
            (o.bl_no, f.code)
            for o in result.canonical_orders
            for f in o.fees
            if not f.excluded and f.price_id is None
        ]
        assert null_prices == []
        # registry 与建档清单一致（登记即命中）
        registered = {
            c["code"]: c["price_id"]
            for c in bootstrap["created"]
            if get_registry().lookup(c["code"]) is not None
        }
        assert registered == {c["code"]: c["price_id"] for c in bootstrap["created"]}
