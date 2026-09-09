"""旧版账单模板：内置模板定义 + 历史固化 JSON 的回退读取（读取兼容）。

层级关系（2026-09 命名统一）：本模块是**旧版**单模板持久化
（内置模板 + 历史固化 JSON，sha1-16 指纹 = compute_legacy_fingerprint）；
现役 YAML 模板库与三级识别在 template_store.py（md5-8 指纹）——
本模块仅作为模板库未命中时的回退读取，两套库键不可互相替代。


模板 = 表头行各列归一化文本（去空白）按列序拼接的 sha1 前 16 位指纹，
外加列名 → 字段/费用的映射。来源分两类：
- builtin：内置模板（列名映射 = HEADER_COLUMN_MAP + 模板库旧式费目名 +
  HEADER_ALIASES，指纹按真实表头计算），
  代码即模板，不落盘；
- ai（历史管线，写入侧已移除）：设计上 AI 映射通过后自动固化 JSON 到
  storage/bill_templates/，但该写入链路从未上线；现行固化为人工确认制——
  L3 候选（template_store.build_template_config）经预览确认后
  save_yaml_template 落 YAML 库（templates/*.yaml）。本模块保留对
  历史目录 JSON 的读取兼容（load_template 磁盘分支），目录为空时该
  分支恒未命中。

保存格式（<fingerprint>.json）：
{fingerprint, name, source("builtin"|"ai"), column_map, fee_map,
 created_at, verified}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings

from ..schema import HEADER_ALIASES, HEADER_COLUMN_MAP
from .columns import normalize_header

# 真实应收对账单表头行（golden 2015-01到2015-12上海通寰应收对账单.xls
# 第 6 行，32 列；「 箱号」带前导空格、「落/还箱费」为别名，均原样保留，
# 指纹计算时统一去空白）——内置模板指纹必须按此真实表头计算，才能命中。
# 本元组仅为表头列布局（指纹载体），费用名集以模板库旧式 fees 声明为源
# （collect_legacy_fee_names，fee_map 组装处）；两处费用列不一致时以模板为准。
BUILTIN_HEADERS: tuple[str, ...] = (
    "序号",
    "日期",
    "当前状态",
    "客户名称",
    "客户联系人",
    "客户编号",
    "业务类型",
    "提单号",
    "门点",
    "箱型",
    "联系人",
    "联系电话",
    "装卸货地点",
    "装卸货地址",
    " 箱号",
    "做箱时间",
    "港区",
    "提箱堆场",
    "还箱堆场",
    "车牌号",
    "车队",
    "司机",
    "司机手机",
    "运费",
    "待时费",
    "预提费",
    "洋山费",
    "落/还箱费",
    "其它费",
    "已收/付金额",
    "备注",
    "应付备注",
)


def compute_legacy_fingerprint(headers: list[str]) -> str:
    """表头行指纹：各列归一化文本按列序拼接的 sha1 前 16 位。"""
    payload = "".join(normalize_header(h) for h in headers)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _build_builtin() -> BillTemplate:
    """内置模板：列名映射 = 现有 HEADER_COLUMN_MAP + 费用名 + 别名。

    费目名动态化（2026-09-04）：内置模板费用与 jinxin_v1 模板同源（旧式 fees
    声明）；函数内 lazy import 破 template ↔ template_store 互引环。
    """
    from . import template_store

    column_map = dict(HEADER_COLUMN_MAP)
    fee_map = {name: name for name in template_store.collect_legacy_fee_names()}
    fee_map.update(HEADER_ALIASES)
    return BillTemplate(
        fingerprint=compute_legacy_fingerprint(list(BUILTIN_HEADERS)),
        name="应收对账单（内置模板）",
        source="builtin",
        column_map=column_map,
        fee_map=fee_map,
        created_at="",
        verified=True,
    )


@dataclass
class BillTemplate:
    """一个账单模板（fingerprint 唯一标识）。"""

    fingerprint: str
    name: str
    source: str  # "builtin" | "ai"
    column_map: dict[str, str]  # 归一化列名 → BillRow 字段名
    fee_map: dict[str, str] = field(default_factory=dict)  # 归一化列名 → 标准费用名
    created_at: str = ""  # ISO8601；builtin 恒空
    verified: bool = False

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "name": self.name,
            "source": self.source,
            "column_map": self.column_map,
            "fee_map": self.fee_map,
            "created_at": self.created_at,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BillTemplate:
        return cls(
            fingerprint=data["fingerprint"],
            name=data.get("name", data["fingerprint"]),
            source=data.get("source", "ai"),
            column_map=data.get("column_map", {}),
            fee_map=data.get("fee_map", {}),
            created_at=data.get("created_at", ""),
            verified=bool(data.get("verified", False)),
        )


# 内置模板：代码即模板（不落盘），load_template 命中时直接返回
BUILTIN_TEMPLATE = _build_builtin()


def template_dir() -> Path:
    """模板存储目录（storage/bill_templates/），不存在时自动创建。"""
    path = settings.storage_dir / "bill_templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_template(fingerprint: str) -> BillTemplate | None:
    """按指纹加载模板：builtin 优先，其次磁盘 AI 模板；未命中返回 None。"""
    if fingerprint == BUILTIN_TEMPLATE.fingerprint:
        return BUILTIN_TEMPLATE
    path = template_dir() / f"{fingerprint}.json"
    if not path.exists():
        return None
    try:
        return BillTemplate.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError):
        # 损坏模板文件按未命中处理（可被新 AI 映射覆盖），不阻断导入
        return None
