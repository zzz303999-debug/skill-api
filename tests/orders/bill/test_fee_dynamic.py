"""费用项动态化专项测试（2026-09-04：费用项以模板库 fees 声明为唯一源）。

锁定验收语义：
- collect_fee_names：旧式模板 fees 键 + channels 应收(shou) mapping 费目名并集；
  应付/成本等非 shou 通道费目不混入
- collect_legacy_fee_names：仅旧式（无 channels）模板声明；池空回退金科信六费目
  （内置/精确兜底链不静默归零）；jinxin_v1 六费目镜像与声明一致
- aggregator._fee_entries：模板新增费目自动收录（不再按代码常量白名单）
- ai_header._ai_schema/_ai_targets：请求时现算——模板 reload 后 enum 与白名单
  同步扩展（修复 import 时快照冻结）
"""

from __future__ import annotations

import app.orders.bill.parsing.template_store as template_store
from app.orders.bill.aggregation.aggregator import _fee_entries
from app.orders.bill.schema import BillRow

_LEGACY_TEMPLATE = {
    "jinxin_v1": {
        "fees": {
            "运费": "运费",
            "待时费": "待时费",
            "预提费": "预提费",
            "洋山费": "洋山费",
            "落还箱费": "落/还箱费",
            "其它费": "其它费",
            "税金": "税金",  # 模板扩展费目（验收点）
        }
    }
}

_CHANNELS_TEMPLATE = {
    "junyu_v1": {
        "fees": {
            "channels": {"应收": "shou", "应付": "pay"},
            "mapping": {
                "应收.运费": "freight",
                "应收.打/改单费": "amend",
                "应收.其它费": "other",
                "应付.扣除费": "deduction",  # 非 shou 通道 → 不混入应收池
            },
        }
    }
}


def _fake_all(monkeypatch, templates: dict) -> None:
    monkeypatch.setattr(template_store, "all_templates", lambda: templates)


class TestCollectFeeNames:
    def test_merges_legacy_keys_and_shou_mapping(self, monkeypatch):
        """宽池 = 旧式键 + shou 通道费目名并集；应付通道费目排除。"""
        _fake_all(monkeypatch, {**_LEGACY_TEMPLATE, **_CHANNELS_TEMPLATE})
        names = template_store.collect_fee_names()
        assert "运费" in names and "税金" in names  # 旧式键全收
        assert "打/改单费" in names  # shou mapping 收
        assert "扣除费" not in names  # 应付通道排除
        # 去重保序：同名字（jinxin 运费 与 junyu 应收.运费）只出现一次
        assert names.count("运费") == 1
        # 首见序 = 模板文件序（jinxin 先于 junyu）
        assert names.index("运费") < names.index("打/改单费")

    def test_legacy_pool_only_legacy_templates(self, monkeypatch):
        """旧池不收 channels 模板费目（jinxin 语义隔离）。"""
        _fake_all(monkeypatch, {**_LEGACY_TEMPLATE, **_CHANNELS_TEMPLATE})
        names = template_store.collect_legacy_fee_names()
        assert names == ["运费", "待时费", "预提费", "洋山费", "落还箱费", "其它费", "税金"]
        assert "打/改单费" not in names

    def test_legacy_pool_falls_back_when_empty(self, monkeypatch):
        """库空/全 channels → 回退金科信六费目（兜底链不静默归零）。"""
        _fake_all(monkeypatch, _CHANNELS_TEMPLATE)  # 无旧式模板
        assert template_store.collect_legacy_fee_names() == [
            "运费", "待时费", "预提费", "洋山费", "落还箱费", "其它费",
        ]
        _fake_all(monkeypatch, {})
        assert template_store.collect_legacy_fee_names() == [
            "运费", "待时费", "预提费", "洋山费", "落还箱费", "其它费",
        ]

    def test_jinxin_live_pool_matches_six_fees(self):
        """实库（磁盘模板）：jinxin_v1 六费目镜像与既有基线一致。"""
        names = template_store.collect_legacy_fee_names()
        assert names == ["运费", "待时费", "预提费", "洋山费", "落还箱费", "其它费"]


class TestFeeEntriesDynamic:
    """单行费用条目（一行一票：2026-08-31 拍板后每行独立成单，签名收单行）。"""

    def test_new_fee_name_auto_collected(self):
        """模板扩展费目（税金）→ 归集自动收录（fees 键序即列发现序）。"""
        row = BillRow(seq="1", fees={"运费": 2100.0, "税金": 60.0})
        entries, total = _fee_entries(row)
        keys = [next(iter(e)) for e in entries]
        assert keys == ["运费", "税金"]  # 行内键序（模板声明序）
        assert entries[1]["税金"]["money"] == 60.0
        assert total == 2160.0

    def test_zero_and_text_values_unchanged(self):
        """0 金额建条目、非数字原文不建条目（与旧实现等价）。"""
        row = BillRow(seq="1", fees={"运费": 2100.0, "待时费": 0.0, "其它费": "壹佰元"})
        entries, total = _fee_entries(row)
        keys = [next(iter(e)) for e in entries]
        assert keys == ["运费", "待时费"]  # 数字（含 0）收；文本不收
        assert total == 2100.0

    def test_each_row_independent_entries(self):
        """一行一票：同名费用跨行不累加，两行各自独立成条目（行缺键不丢费目）。"""
        row1 = BillRow(seq="1", fees={"运费": 100.0})
        row2 = BillRow(seq="2", fees={"运费": 200.0, "税金": 30.0})
        entries1, total1 = _fee_entries(row1)
        entries2, total2 = _fee_entries(row2)
        assert entries1[0]["运费"]["money"] == 100.0 and total1 == 100.0
        assert entries2[0]["运费"]["money"] == 200.0
        assert entries2[1]["税金"]["money"] == 30.0 and total2 == 230.0


class TestAiTargetsDynamic:
    def test_schema_enum_tracks_template_additions(self, monkeypatch):
        """W1 修复锁定：_ai_schema 请求时现算——reload 后新费目同步进 enum。"""
        import app.orders.bill.parsing.ai_header as ai_header

        _fake_all(monkeypatch, _LEGACY_TEMPLATE)
        enum_base = ai_header._ai_schema()["properties"]["mapping"]["items"]["properties"]["target"]["enum"]
        assert "fee:税金" in enum_base
        assert "fee:运费" in enum_base

        # 模板再扩展（模拟固化新家族）→ 不重启进程 enum 同步
        _fake_all(monkeypatch, {**_LEGACY_TEMPLATE, "newfam_v1": {
            "fees": {"channels": {"应收": "shou"}, "mapping": {"应收.港杂费": "port_misc"}}
        }})
        enum_after = ai_header._ai_schema()["properties"]["mapping"]["items"]["properties"]["target"]["enum"]
        assert "fee:港杂费" in enum_after
        assert "fee:港杂费" not in enum_base  # 新快照含、旧快照不含（证明非 import 冻结）
