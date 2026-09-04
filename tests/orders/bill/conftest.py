"""竞品账单导入测试 fixture：golden 资产路径与解析/归集结果。"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.orders.bill.service as service_module
from app.core.errors import LLMError
from app.orders.bill import BillOrder, group_orders, parse_bill
from helpers import REAL_XLS, build_bill_bytes

# golden 样本（表格文件）不入库：本地存在时才跑真实账单用例，否则按存在性跳过
_REAL_MISSING = "golden 样本未入库（表格文件不入库），本地放置后自动启用"


@pytest.fixture(autouse=True)
def _llm_unavailable_for_bill(monkeypatch):
    """既有用例默认回退路径：AI 表头映射视为 LLM 不可用（测试环境无网关）。

    指纹未命中的构造账单会触发 AI 流程；生产两段式编排（_parse_stage_async）
    的 achat_json 抛 LLMError → 回退精确匹配（同步 parse_bill 为纯 CPU 基准，
    不含 LLM，2026-09 第二波同步链清理）。需要真实 AI 映射的用例
    （test_template.py / test_ai_header_async.py）自行 monkeypatch 覆盖。
    """

    async def _unavailable_async(*_args, **_kwargs):
        raise LLMError("LLM unavailable in tests")

    monkeypatch.setattr(service_module, "achat_json", _unavailable_async)


@pytest.fixture(autouse=True)
def _isolate_master_data_store(tmp_path, monkeypatch):
    """基础资料计数存储隔离：每用例重建到临时目录。

    防止 create 模式用例（service 编排）写入真实 storage/master_data.json
    并在用例间泄漏计数。
    """
    from app.orders.bill import master_data_store

    master_data_store.reload_store(tmp_path / "master_data.json")
    yield
    master_data_store.reload_store(tmp_path / "master_data.json")


@pytest.fixture(autouse=True)
def _isolate_fee_registry(tmp_path):
    """费目自举注册表隔离：每用例重建到临时目录。

    防止自举用例（apply_price_map 会读 registry）写入真实 storage/fee_registry.json
    并在用例间泄漏登记结果（幂等命中会掩盖重试/懒创建断言）。
    """
    from app.orders.bill import fee_registry

    fee_registry.reload_registry(tmp_path / "fee_registry.json")
    yield
    fee_registry.reload_registry(tmp_path / "fee_registry.json")


@pytest.fixture(autouse=True)
def _isolate_imported_registry(tmp_path):
    """成功单注册表隔离：每用例重建到临时目录。

    防止 create 模式用例（client/service 编排）写入真实 storage/imported_orders.json
    并在用例间泄漏成功记录（去重命中会掩盖重导/并发断言）。
    """
    from app.orders.bill import imported_registry

    imported_registry.reload_registry(tmp_path / "imported_orders.json")
    yield
    imported_registry.reload_registry(tmp_path / "imported_orders.json")


@pytest.fixture(autouse=True)
def _no_real_archive_calls(monkeypatch):
    """全局拦截建档族网络调用（零网络）：默认全部成功返回递增 archive_id。

    拦截 create_archives_async（async 生产链路唯一建档入口，同步版已随
    Phase 4b 清理删除）。费目自举/基础资料建档的编排用例可自行 monkeypatch
    覆盖（如 test_master_data 的 fake_create）；未 mock 的用例（test_template/
    test_route 等 build_result_async 调用）不会向真实 s3.jxt56.com 发建档请求
    （建档失败本就不抛断）。
    yield 真实 create_archives_async：需要验证建档内部调用链的用例（如
    TestClientDirectURL）可显式依赖本 fixture 并恢复真实实现。
    """
    import itertools

    import app.orders.bill.master_data_client as md_client_module

    real_create = md_client_module.create_archives_async  # 真实函数（此刻未被 mock）
    counter = itertools.count(9000)

    async def _fake(forms_by_kind: dict, sk: str = ""):
        return {
            kind: {
                key: {
                    "success": True,
                    "archive_id": str(next(counter)),
                    "error": None,
                }
                for key in forms
            }
            for kind, forms in forms_by_kind.items()
        }

    monkeypatch.setattr(md_client_module, "create_archives_async", _fake)
    yield real_create


@pytest.fixture(autouse=True)
def _isolate_concurrency_primitives():
    """进程级并发原语隔离：每用例重建共享信号量并清空 per-key 异步锁。

    信号量首次竞争等待时绑定运行事件循环（pytest-asyncio 每用例新 loop）；
    重建避免遗留 waiter 跨用例触发 "bound to a different event loop"；
    锁字典清空防 per-bl_no 锁对象随用例历史无限累积。
    """
    import asyncio

    import app.orders.bill.client as bill_client_mod
    import app.orders.bill.imported_registry as imported_registry_mod
    from app.core.config import settings

    def _reset():
        bill_client_mod._create_batch_guard = asyncio.Semaphore(
            settings.bill_create_concurrency
        )
        bill_client_mod._create_downstream_slots = asyncio.Semaphore(
            settings.bill_create_concurrency
        )
        imported_registry_mod._ASYNC_LOCKS.clear()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _isolate_template_cache():
    """模板库缓存隔离：用例 monkeypatch _TEMPLATES_DIR 后还原真实模板库。

    teardown 里先保存的 real_dir 显式恢复目录再 reload（不依赖 monkeypatch
    的还原时序——pytest 的 autouse fixture 与测试参数 fixture 的 teardown
    顺序不可靠，实测 monkeypatch 还原晚于本 fixture）。test_template 固化
    用例会把 _TEMPLATE_CACHE 留在 tmp_path 内容上，污染后续文件上传识别。
    """
    from app.orders.bill import template_store

    real_dir = template_store._TEMPLATES_DIR  # setup 时捕获（尚未被用例 patch）
    yield
    template_store._TEMPLATES_DIR = real_dir  # 显式恢复目录（不依赖 monkeypatch）
    template_store.reload_templates()  # 重载真实模板库


@pytest.fixture(autouse=True)
def _isolate_fee_mapping_caches():
    """费用映射/自举/基础资料配置缓存重置（每用例后）：防止配置注入用例
    （monkeypatch 临时文件路径）在 teardown 后残留缓存污染后续用例；
    幂等无副作用（各模块配置重载即读回真实配置文件）。
    """
    yield
    from app.orders.bill import (
        fee_bootstrap,
        fee_name_map,
        fee_price_map,
        master_data,
        template_store,
    )

    try:
        fee_name_map.reload_fee_alias_dictionary()
        fee_price_map.reload_price_map()
        fee_bootstrap.reload_bootstrap_config()
        master_data.reload_config()
        template_store.reload_alias_dictionary()
    except RuntimeError:
        pass  # 真实配置文件缺失时容错跳过（与 test_fees._fee_caches 同口径）


@pytest.fixture()
def bill_builder(tmp_path):
    """把内存构造的账单写入临时文件，返回路径（供 parse_bill 等按路径解析）。"""

    def _build(
        headers: dict[str, str], rows: list[dict[str, object]], name: str = "bill.xlsx"
    ) -> Path:
        path = tmp_path / name
        path.write_bytes(build_bill_bytes(headers, rows))
        return path

    return _build


@pytest.fixture()
def real_xls_bytes() -> bytes:
    if not REAL_XLS.exists():
        pytest.skip(_REAL_MISSING)
    return REAL_XLS.read_bytes()


@pytest.fixture()
def real_parse():
    """真实账单 parse_bill 结果（.xls 直读）；样本缺失时跳过。"""
    if not REAL_XLS.exists():
        pytest.skip(_REAL_MISSING)
    return parse_bill(REAL_XLS)


@pytest.fixture()
def real_orders(real_parse) -> list[BillOrder]:
    return group_orders(real_parse.rows, real_parse.period).orders
