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
from app.orders.bill.parsing.parser import _SheetView, read_data_rows
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


class TestIgnoredColsRecovery:
    """B1（2026-09-07 用户拍板）：忽略列金额判据二次收录。

    背景：L3 闸门 c 将白名单外费用列降级 ignore 并固化 _ignored_cols，
    堵死动态收录入口（模板路径新费用列沉默丢失、费用管理无建档）。
    """

    @staticmethod
    def _view(rows: list[list[object]]) -> _SheetView:
        """构造单 sheet 视图：rows[0] 为表头，其余为数据行（1-based 访问）。"""

        def get_cell(r: int, c: int):
            try:
                return rows[r - 1][c - 1]
            except (IndexError, TypeError):
                return None

        return _SheetView(len(rows), max(len(r) for r in rows), {}, get_cell)

    def test_ignored_money_col_recovered_as_fee(self):
        """忽略列数据区有金额 → 收为费用列（闸门 c 降级列复活，费用名=列名）。"""
        view = self._view([
            ["序号", "提单号", "未知附加费", "备注列"],
            ["1", "BL001", 100.0, "文本"],
        ])
        rows, unmatched = read_data_rows(view, 1, ignored_cols={3, 4})
        assert rows[0].fees == {"未知附加费": 100.0}
        assert unmatched == []  # 忽略列不告警语义保持

    def test_ignored_reconcile_headers_not_recovered(self):
        """列名命中 IGNORED_HEADERS（已付金额）→ 不收（防对账列误收；数据区全是金额）。"""
        view = self._view([
            ["序号", "提单号", "已付金额"],
            ["1", "BL001", 500.0],
        ])
        rows, unmatched = read_data_rows(view, 1, ignored_cols={3})
        assert rows[0].fees == {}

    def test_ignored_text_col_not_recovered_no_warning(self):
        """纯文本忽略列（无可转金额）→ 不收、不告警（保持既有语义）。"""
        view = self._view([
            ["序号", "提单号", "月份"],
            ["1", "BL001", "九月"],
        ])
        rows, unmatched = read_data_rows(view, 1, ignored_cols={3})
        assert rows[0].fees == {}
        assert unmatched == []

    def test_non_ignored_unknown_money_col_still_dynamic(self):
        """非忽略的未知金额列 → 原有动态收录不受 B1 影响（回归锁定）。"""
        view = self._view([
            ["序号", "提单号", "新费用X"],
            ["1", "BL001", 88.0],
        ])
        rows, unmatched = read_data_rows(view, 1)  # 无 ignored_cols
        assert rows[0].fees == {"新费用X": 88.0}
        assert unmatched == []  # 已收为费用列，不再上报未识别


class TestIgnoredColsTemplatePath:
    """B1' 补丁（2026-09-07 小王费实证）：_parse_with_template 路径收录
    _ignored_cols（AI 降级 ignore 固化列）——canonical 家族金额列进 _fees。"""

    HEADERS = ["序号", "客户名称", "提单号", "箱型箱量", "小王费"]

    @staticmethod
    def _template(ignored_cols: list[int]) -> dict:
        """L3 候选形态模板：columns 标准字段 + _ignored_cols（无 fees 段）。"""
        return {
            "template_id": "ignored_recover_v1",
            "family": "ignored_recover",
            "name": "忽略列收录最小模板（测试）",
            "match": {
                "fingerprints": [template_store.compute_fingerprint(TestIgnoredColsTemplatePath.HEADERS)],
                "family_min_overlap": 0.3,
            },
            "header": {"row_anchor": "序号", "two_row": False},
            "data": {"row_filter": "seq_numeric"},
            "columns": {
                "seq": "序号",
                "customer_name": "客户名称",
                "bl_no": "提单号",
                "box_type_qty": "箱型箱量",
            },
            "normalizers": {"box_type_qty": "box_parse"},
            "_ignored_cols": ignored_cols,
        }

    COL_LETTERS = ["A", "B", "C", "D", "E"]

    @classmethod
    def _headers_dict(cls) -> dict:
        return dict(zip(cls.COL_LETTERS, cls.HEADERS, strict=True))

    def _rows(self, fee_value: object) -> list[dict]:
        return [{"A": 1, "B": "客户甲", "C": "OOLU1", "D": "40HQ", "E": fee_value}]

    def _parse(self, monkeypatch, ignored_cols: list[int], fee_value: object):
        from pathlib import Path

        from app.orders.bill import parse_bill
        from app.orders.bill.aggregation.canonical_aggregator import group_canonical
        from helpers import build_bill_bytes

        tpl = self._template(ignored_cols)
        monkeypatch.setattr(template_store, "_TEMPLATE_CACHE", {"ignored_recover_v1": tpl})
        bill = build_bill_bytes(self._headers_dict(), self._rows(fee_value))
        path = Path(".tmp") / "ignored_recover.xlsx"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(bill)
        out = parse_bill(path)
        canon = group_canonical(out.canonical_rows, out.template_match.template, out.period)
        return out, (canon[0].fees if canon else [])

    def test_ignored_fee_col_recovered_to_other(self, monkeypatch):
        """AI 降级 ignore 的费用列（小王费，金额 77）→ 收进 _fees（other+原名），
        建档由 fee_bootstrap B2 兑底（费用管理自动建档）。"""
        out, fees = self._parse(monkeypatch, ignored_cols=[5], fee_value=77.0)
        other = [f for f in fees if f.code == "other"]
        assert other and other[0].money == 77.0
        assert "小王费" in (other[0].note or "")

    def test_ignored_anchor_col_not_recovered(self, monkeypatch):
        """ignored 的对账列（未付，锚点特征）数据区全金额 → 不收（防重复计费）。"""

        # 前提：未付靠锚点特征排除（_is_anchor_column），非 IGNORED_HEADERS
        tpl = self._template(ignored_cols=[5])
        monkeypatch.setattr(template_store, "_TEMPLATE_CACHE", {"ignored_recover_v1": tpl})
        from pathlib import Path

        from app.orders.bill import parse_bill
        from app.orders.bill.aggregation.canonical_aggregator import group_canonical
        from helpers import build_bill_bytes

        bill = build_bill_bytes(self._headers_dict(), self._rows(15.0))
        path = Path(".tmp") / "ignored_anchor.xlsx"
        path.write_bytes(bill)
        out = parse_bill(path)
        canon = group_canonical(out.canonical_rows, out.template_match.template, out.period)
        fees = canon[0].fees if canon else []
        assert not [f for f in fees if f.note and "未付" in f.note]

    def test_ignored_text_col_kept_unmatched(self, monkeypatch):
        """纯文本 ignored 列（月份类）→ 不收录、保持静默（_ignored_cols 不告警）。"""
        tpl = self._template(ignored_cols=[5])
        monkeypatch.setattr(template_store, "_TEMPLATE_CACHE", {"ignored_recover_v1": tpl})
        from pathlib import Path

        from app.orders.bill import parse_bill
        from helpers import build_bill_bytes

        bill = build_bill_bytes(self._headers_dict(), self._rows("九月"))
        path = Path(".tmp") / "ignored_text.xlsx"
        path.write_bytes(bill)
        out = parse_bill(path)
        assert out.unmatched_headers == []  # 忽略列不告警语义保持
