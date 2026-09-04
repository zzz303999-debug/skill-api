"""账单模板泛化测试：指纹命中 / AI 映射 / 校验闸门 / 模板固化（mock LLM，零网络）。

覆盖：
- 金科信内置模板指纹命中（不调 AI）
- 异构模板（列名全换）→ mock AI 映射 → 解析与归集结果与手工映射一致
- AI 缺必映射字段 / 费用列抽样数字率不达标 → 400 header_mapping_rejected
- 白名单外费用 → 降级 ignore + new_fees 上报
- 对账 matched 后模板落盘；同指纹再次导入不再调 AI（mock 计数 == 0）

AI mock 方式与项目既有约定一致：monkeypatch ai_header.chat_json 返回
(payload_dict, meta) 二元组；conftest 的 autouse fixture 已把 chat_json
默认置为 LLM 不可用（回退精确匹配），本文件用例显式覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.orders.bill.service as service_module
from app.core.errors import BadRequestError
from app.orders.bill import build_result_async
from app.orders.bill.template import (
    BUILTIN_TEMPLATE,
    compute_legacy_fingerprint,
)
from helpers import REAL_XLS, build_bill_bytes, build_bill_xls_bytes

pytestmark = pytest.mark.asyncio

# 异构模板：列名全换（与金科信同构但名称完全不同）
HETERO_HEADERS = {
    "A": "序号",
    "B": "海运提单号",
    "C": "委托单位",
    "D": "箱型规格",
    "E": "海运费",
}
HETERO_ROWS = [
    {"A": 1, "B": "OOLU12345678", "C": "客户甲", "D": "40HQ", "E": 100},
    {"A": 2, "B": "OOLU12345679", "C": "客户乙", "D": "20GP", "E": 200},
]

# mock AI 返回的正确映射（build_bill_bytes 表头在第 2 行，5 列；
# L3 语义：target 为标准字段，序号列 ignore，白名单费用 fee:运费）
AI_MAPPING_OK = {
    "header_row": 2,
    "two_row": False,
    "fee_boundary": "column_range",
    "mapping": [
        {"col": 1, "target": "ignore"},
        {"col": 2, "target": "bl_no"},
        {"col": 3, "target": "customer_name"},
        {"col": 4, "target": "box_type_qty"},
        {"col": 5, "target": "fee:运费"},
    ],
    "confidence": 0.95,
}


def _patch_ai(monkeypatch, payload: dict) -> dict:
    """mock service.achat_json（生产两段式编排唯一 LLM 入口）返回固定映射，并计数。"""
    calls = {"n": 0}

    async def fake_achat_json(_messages, **_kwargs):
        calls["n"] += 1
        return dict(payload), {"model": "fake", "usage": None}

    monkeypatch.setattr(service_module, "achat_json", fake_achat_json)
    return calls


def _write_hetero(tmp_path, rows: list[dict] | None = None) -> Path:
    """异构模板账单 → 临时文件路径（表头第 2 行，数据从第 3 行起）。"""
    path = tmp_path / "hetero.xlsx"
    path.write_bytes(build_bill_bytes(HETERO_HEADERS, rows or HETERO_ROWS))
    return path


@pytest.mark.skipif(
    not REAL_XLS.exists(), reason="golden 样本未入库（表格文件不入库），本地放置后自动启用"
)
class TestBuiltinTemplate:
    """内置模板（jinxin_v1.yaml）：指纹按真实表头 md5[:8] 计算，命中后不调 AI。"""

    async def test_fingerprint_matches_real_bill_header(self, real_parse):
        """真实账单解析命中的指纹即 jinxin 模板指纹（新算法按真实表头计算）。"""
        assert real_parse.template == {
            "fingerprint": "e31deea1",
            "source": "builtin",
            "name": "应收对账单（内置模板迁移）",
        }
        # 与逐列归一化拼接（非空单元格以 ¶ 连接）口径一致
        from app.orders.bill.template import BUILTIN_HEADERS
        from app.orders.bill.template_store import compute_fingerprint as fp_md5

        assert fp_md5(list(BUILTIN_HEADERS)) == "e31deea1"
        assert compute_legacy_fingerprint(list(BUILTIN_HEADERS)) == BUILTIN_TEMPLATE.fingerprint

    async def test_real_bill_no_ai_called(self, monkeypatch):
        """金科信真实账单指纹命中 → 直接模板映射，AI 调用次数 0。"""
        calls = _patch_ai(monkeypatch, AI_MAPPING_OK)
        output = await service_module._parse_stage_async(
            REAL_XLS.name, REAL_XLS.read_bytes()
        )
        assert calls["n"] == 0
        assert output.template["source"] == "builtin"
        assert len(output.rows) == 1094  # parse_bill 返回原始行（尾部行过滤在归集步骤）


class TestAiMapping:
    """L3 AI 映射（标准字段）：命中流程、四道校验闸门、new_fees 上报、候选固化。"""

    async def test_hetero_mapped_and_aggregated(self, tmp_path, monkeypatch):
        """列名全换 → AI 映射 → 标准字段解析/归集结果正确，产出候选模板配置。"""
        path = _write_hetero(tmp_path)
        calls = _patch_ai(monkeypatch, AI_MAPPING_OK)
        output = await service_module._parse_stage_async(path.name, path.read_bytes())
        assert calls["n"] == 1
        assert output.template["source"] == "template"
        assert output.canonical_rows is not None
        assert len(output.canonical_rows) == 2
        assert output.canonical_rows[0]["bl_no"] == "OOLU12345678"
        assert output.canonical_rows[0]["customer_name"] == "客户甲"
        assert output.canonical_rows[0]["box_type_qty"] == [{"type": "40HQ", "qty": 1}]
        assert output.canonical_rows[1]["bl_no"] == "OOLU12345679"
        # 候选模板配置（人工确认固化的载体）：columns 段 + 默认归一化
        assert output.new_template is not None
        assert output.new_template["columns"]["bl_no"] == ["海运提单号"]
        assert output.new_template["normalizers"]["box_type_qty"] == "box_parse"
        # 归集：box 同型累加
        from app.orders.bill.canonical_aggregator import group_canonical

        orders = group_canonical(
            output.canonical_rows, output.new_template, output.period
        )
        assert [o.bl_no for o in orders] == ["OOLU12345678", "OOLU12345679"]
        assert orders[0].box_groups[0].b_type == "40HQ"
        assert orders[0].missing_fields == []

    async def test_missing_required_column_rejected(self, tmp_path, monkeypatch):
        """AI 映射缺 bl_no → 400，details 含 AI 映射 / 失败原因 / 表头原文。"""
        path = _write_hetero(tmp_path)
        payload = dict(AI_MAPPING_OK)
        payload["mapping"] = [m for m in payload["mapping"] if m["target"] != "bl_no"]
        _patch_ai(monkeypatch, payload)
        with pytest.raises(BadRequestError) as caught:
            await service_module._parse_stage_async(path.name, path.read_bytes())
        assert caught.value.http_status == 400
        assert caught.value.code == "header_mapping_rejected"
        assert any(
            "必映射字段缺失：bl_no" in r for r in caught.value.details["failure_reasons"]
        )
        assert caught.value.details["ai_mapping"] == payload
        assert caught.value.details["header_zone"]

    async def test_fee_sample_numeric_ratio_rejected(self, tmp_path, monkeypatch):
        """费用列抽样数字率不达标（全中文金额）→ 400（不带着可疑映射跑）。"""
        path = tmp_path / "fee-text.xlsx"
        path.write_bytes(
            build_bill_bytes(
                HETERO_HEADERS,
                [{"A": 1, "B": "OOLU12345678", "C": "客户甲", "D": "40HQ", "E": "壹佰元"}],
            )
        )
        _patch_ai(monkeypatch, AI_MAPPING_OK)
        with pytest.raises(BadRequestError) as caught:
            await service_module._parse_stage_async(path.name, path.read_bytes())
        assert caught.value.code == "header_mapping_rejected"
        assert any("费用列" in r and "抽样" in r for r in caught.value.details["failure_reasons"])

    async def test_duplicate_field_mapping_rejected(self, tmp_path, monkeypatch):
        """同一标准字段被两列重复映射 → 400（闸门 b）。"""
        path = _write_hetero(tmp_path)
        payload = {
            "header_row": 2,
            "mapping": [
                {"col": 2, "target": "bl_no"},
                {"col": 3, "target": "bl_no"},
                {"col": 4, "target": "box_type_qty"},
                {"col": 5, "target": "fee:运费"},
            ],
            "confidence": 0.8,
        }
        _patch_ai(monkeypatch, payload)
        with pytest.raises(BadRequestError) as caught:
            await service_module._parse_stage_async(path.name, path.read_bytes())
        assert caught.value.code == "header_mapping_rejected"
        assert any("重复映射" in r for r in caught.value.details["failure_reasons"])

    async def test_header_row_shifted_rejected(self, tmp_path, monkeypatch):
        """header_row 错位（+1 吞掉全部数据行）→ 样本为空 → 400，不静默产出错行。"""
        path = tmp_path / "shifted.xlsx"
        path.write_bytes(build_bill_bytes(HETERO_HEADERS, [HETERO_ROWS[0]]))
        payload = dict(AI_MAPPING_OK)
        payload["header_row"] = 3  # 真实表头在第 2 行，错报为 3 → 唯一数据行被吞
        _patch_ai(monkeypatch, payload)
        with pytest.raises(BadRequestError) as caught:
            await service_module._parse_stage_async(path.name, path.read_bytes())
        assert caught.value.code == "header_mapping_rejected"
        assert any("未找到含提单号的数据行" in r for r in caught.value.details["failure_reasons"])

    async def test_small_xls_header_row_out_of_bounds_rejected(self, tmp_path, monkeypatch):
        """小行数 .xls（nrows=3）+ AI 幻觉返回 header_row=15 → 400 而非 500。

        修复前 xlrd 越界抛 IndexError 落全局 500；修复后上界取
        min(MAX_HEADER_SCAN_ROWS, nrows)，与 .xlsx 一致走 400 header_mapping_rejected。
        """
        path = tmp_path / "small.xls"
        path.write_bytes(
            build_bill_xls_bytes(
                HETERO_HEADERS,
                [{"A": 1, "B": "OOLU12345678", "C": "客户甲", "D": "40HQ", "E": 100}],
            )
        )
        payload = dict(AI_MAPPING_OK)
        payload["header_row"] = 15  # 幻觉：超过文件实际行数（3 行）
        _patch_ai(monkeypatch, payload)
        with pytest.raises(BadRequestError) as caught:
            await service_module._parse_stage_async(path.name, path.read_bytes())
        assert caught.value.http_status == 400
        assert caught.value.code == "header_mapping_rejected"
        assert any(
            "header_row 非法" in r and "1..3" in r for r in caught.value.details["failure_reasons"]
        )

    async def test_small_xlsx_header_row_out_of_bounds_rejected(self, tmp_path, monkeypatch):
        """同场景 .xlsx 对照：行为与 .xls 一致（400 header_mapping_rejected）。"""
        path = tmp_path / "small.xlsx"
        path.write_bytes(
            build_bill_bytes(
                HETERO_HEADERS,
                [{"A": 1, "B": "OOLU12345678", "C": "客户甲", "D": "40HQ", "E": 100}],
            )
        )
        payload = dict(AI_MAPPING_OK)
        payload["header_row"] = 15
        _patch_ai(monkeypatch, payload)
        with pytest.raises(BadRequestError) as caught:
            await service_module._parse_stage_async(path.name, path.read_bytes())
        assert caught.value.http_status == 400
        assert caught.value.code == "header_mapping_rejected"
        assert any("header_row 非法" in r for r in caught.value.details["failure_reasons"])

    async def test_new_fee_ignored_and_reported(self, tmp_path, monkeypatch):
        """白名单外费用（fee:运杂费）→ 该列降级 ignore，new_fees 上报。"""
        path = tmp_path / "newfee.xlsx"
        path.write_bytes(
            build_bill_bytes(
                HETERO_HEADERS,
                [{"A": 1, "B": "OOLU12345678", "C": "客户甲", "D": "40HQ", "E": 100}],
            )
        )
        payload = {
            "header_row": 2,
            "mapping": [
                {"col": 1, "target": "ignore"},
                {"col": 2, "target": "bl_no"},
                {"col": 3, "target": "customer_name"},
                {"col": 4, "target": "box_type_qty"},
                {"col": 5, "target": "fee:运杂费"},
            ],
            "confidence": 0.9,
        }
        _patch_ai(monkeypatch, payload)
        output = await service_module._parse_stage_async(path.name, path.read_bytes())
        assert output.new_fees == ["运杂费"]
        # 已通过 new_fee: 前缀上报；表头原文不重复上报
        assert "new_fee:运杂费" in output.unmatched_headers
        assert "运杂费" not in output.unmatched_headers
        # 降级 ignore：费用不解析（标准字段行无费用键）
        assert "fees" not in (output.canonical_rows or [{}])[0]
        assert output.canonical_rows[0]["bl_no"] == "OOLU12345678"

    async def test_ai_ignore_cols_not_reported(self, tmp_path, monkeypatch):
        """AI 明确 ignore 的列（如当前状态）不进 unmatched_headers。"""
        headers = {
            "A": "序号",
            "B": "海运提单号",
            "C": "当前状态",
            "D": "委托单位",
            "E": "箱型规格",
        }
        path = tmp_path / "ignored.xlsx"
        path.write_bytes(
            build_bill_bytes(
                headers,
                [{"A": 1, "B": "OOLU12345678", "C": "已审核", "D": "客户甲", "E": "40HQ"}],
            )
        )
        payload = {
            "header_row": 2,
            "mapping": [
                {"col": 1, "target": "ignore"},
                {"col": 2, "target": "bl_no"},
                {"col": 3, "target": "ignore"},
                {"col": 4, "target": "customer_name"},
                {"col": 5, "target": "box_type_qty"},
            ],
            "confidence": 0.9,
        }
        _patch_ai(monkeypatch, payload)
        output = await service_module._parse_stage_async(path.name, path.read_bytes())
        assert output.unmatched_headers == []
        assert output.canonical_rows[0]["customer_name"] == "客户甲"


class TestTemplatePersist:
    """L3 模板固化：AI 映射 → 候选配置 → 人工确认 save_yaml_template → 二次上传 L1 命中。"""

    async def test_saved_after_confirm_and_reused_without_ai(self, tmp_path, monkeypatch):
        """候选配置经 save_yaml_template 固化到 templates/，同指纹再次导入不再调 AI。"""
        import app.orders.bill.template_store as store
        from app.orders.bill.template_store import save_yaml_template

        # 隔离模板目录：固化写入 tmp_path，识别读取同一目录
        monkeypatch.setattr(store, "_TEMPLATES_DIR", tmp_path)
        store.reload_templates()

        path = tmp_path / "hetero.xlsx"
        path.write_bytes(build_bill_bytes(HETERO_HEADERS, HETERO_ROWS))
        # 第一次导入走生产两段式编排（build_result_async → _parse_stage_async
        # → service.achat_json），同步/异步两入口均 mock 且共享计数
        calls1 = _patch_ai(monkeypatch, AI_MAPPING_OK)
        result = await build_result_async(filename=path.name, file_bytes=path.read_bytes())
        assert calls1["n"] >= 1  # 首次导入确实走了 AI（下方 calls2 归零断言的前提）
        # 预览：候选模板配置在 meta.l3_template，不自动落盘（人工确认前置）
        assert result.meta["template"]["source"] == "template"
        candidate = result.meta["l3_template"]
        assert candidate is not None
        assert candidate["match"]["fingerprints"] == [result.meta["template"]["fingerprint"]]
        assert not (tmp_path / "auto_v1.yaml").exists()

        # 人工确认 → 固化（family 由人工指定）
        saved = save_yaml_template(candidate, "hetero")
        assert saved.name == "hetero_v1.yaml"
        assert saved.exists()
        persisted = store.all_templates()["hetero_v1"]
        assert persisted["columns"]["bl_no"] == ["海运提单号"]
        assert persisted["normalizers"]["box_type_qty"] == "box_parse"

        # 同指纹再次导入 → L1 命中，不再调 AI
        calls2 = _patch_ai(monkeypatch, AI_MAPPING_OK)
        output2 = await service_module._parse_stage_async(path.name, path.read_bytes())
        assert calls2["n"] == 0
        assert output2.template_match is not None
        assert output2.template_match.level == "L1"
        assert output2.template["source"] == "template"
        assert output2.new_template is None  # 已固化，不再产生候选
        assert output2.canonical_rows[0]["bl_no"] == "OOLU12345678"
