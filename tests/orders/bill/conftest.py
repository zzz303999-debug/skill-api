"""竞品账单导入测试 fixture：golden 资产路径与解析/归集结果。"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.orders.bill.ai_header as ai_header_module
from app.errors import LLMError
from app.orders.bill import BillOrder, group_orders, parse_bill
from helpers import REAL_XLS, build_bill_bytes

# golden 样本（表格文件）不入库：本地存在时才跑真实账单用例，否则按存在性跳过
_REAL_MISSING = "golden 样本未入库（表格文件不入库），本地放置后自动启用"


@pytest.fixture(autouse=True)
def _llm_unavailable_for_bill(monkeypatch):
    """既有用例默认回退路径：AI 表头映射视为 LLM 不可用（测试环境无网关）。

    指纹未命中的构造账单会触发 AI 流程；此 fixture 让 chat_json 抛 LLMError，
    parser 回退现有精确匹配，行为与改造前一致。需要真实 AI 映射的用例
    （test_template.py）自行 monkeypatch 覆盖 ai_header.chat_json。
    """

    def _unavailable(*_args, **_kwargs):
        raise LLMError("LLM unavailable in tests")

    monkeypatch.setattr(ai_header_module, "chat_json", _unavailable)


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
def _no_real_archive_calls(monkeypatch):
    """全局拦截建档族网络调用（零网络）：默认全部成功返回递增 archive_id。

    费目自举/基础资料建档的编排用例可自行 monkeypatch 覆盖（如 test_master_data
    的 fake_create）；未 mock 的用例（test_template/test_route 等 build_result
    调用）不会向真实 s3.jxt56.com 发建档请求（建档失败本就不抛断）。
    yield 真实 create_archives：需要验证建档内部调用链的用例（如
    TestClientDirectURL）可显式依赖本 fixture 并恢复真实实现。
    """
    import itertools

    import app.orders.bill.master_data_client as md_client_module

    real_create = md_client_module.create_archives  # 真实函数（此刻未被 mock）
    counter = itertools.count(9000)

    def _fake(forms_by_kind: dict):
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

    monkeypatch.setattr(md_client_module, "create_archives", _fake)
    yield real_create


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
