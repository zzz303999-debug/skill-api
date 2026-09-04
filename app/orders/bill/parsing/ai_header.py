"""AI 表头映射：指纹未命中模板库时，用 LLM 映射异构账单表头。

原则：
- AI 只读表头区（前 MAX_HEADER_SCAN_ROWS 行 × 全部列，每格截断），
  绝不接触数据值；映射结果必须过代码校验闸门才采纳；
- 校验闸门（全过才采纳）：
  a. order_num1 / c_title / b_type 来源列必须映射
  b. 同一字段不被多列重复映射（c_name / factory_bei 多来源列除外）
  c. fee 目标必须在 RECEIVABLE_FEE_COLUMNS 白名单内，否则该列降级
     ignore 并记录 new_fees（上报人工确认）
  d. 抽样校验：用映射试读 20 行——提单号列 ≥50% 样本匹配
     ^[A-Za-z0-9-.]+$（字母数字/连字符/点，≥6 位）；费用列数字率 ≥80%；不过 → 拒绝
- 校验失败 → BadRequestError（400，details 含 AI 映射 / 失败原因 / 表头原文），
  不带着可疑映射继续解析；
- LLM 不可用/超时/响应非法 → LLMError / ParseError 上抛，由调用方（parser）
  回退现有精确匹配行为。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import BadRequestError
from app.core.logging_conf import get_logger

from ..schema import (
    HEADER_ALIASES,
    IGNORED_HEADERS,
    MAX_HEADER_SCAN_ROWS,
    RECEIVABLE_FEE_COLUMNS,
)
from .template_store import alias_dictionary

log = get_logger(__name__)

# 表头文本中的空白（含全角空格），归一化口径与 parser/template 一致
_HEADER_WHITESPACE_RE = re.compile(r"[\s\u3000]+")

# 每格输入 LLM 的最大字符数（表头区只做映射，20 字符足够识别列名）
_CELL_MAX_CHARS = 20

# 抽样校验：试读的最大数据行数
_SAMPLE_ROWS = 20
# 提单号列抽样匹配正则（需求口径：≥6 位字母数字/连字符/点）
_BL_NO_SAMPLE_RE = re.compile(r"^[A-Za-z0-9\-\.]{6,}$")
# 提单号列样本匹配率下限
_BL_NO_MIN_RATIO = 0.5
# 费用列数字率下限
_FEE_NUMERIC_MIN_RATIO = 0.8

# 字段清单（标准字段 → 面向表头映射的中文说明；与 schema.CanonicalOrder 对齐，
# 必映射字段显式标注）：LLM 只能从这份清单里选 target。L3 输出经人工确认后
# 固化为模板配置（columns 段），与既有模板库同 schema 同解析路径。
_FIELD_CATALOG: dict[str, str] = {
    "bl_no": "提单号（必映射）",
    "box_type_qty": "箱型/箱量（如 40HQ、40HQ*2，必映射）",
    "customer_name": "客户名称",
    "customer_no": "客户编号",
    "customer_contact": "客户联系人",
    "contact_person": "联系人",
    "contact_phone": "联系电话",
    "biz_no": "业务编号",
    "biz_type": "业务类型（进口/出口）",
    "io_type": "进出口",
    "order_date": "业务日期（月-日/完整日期）",
    "work_date": "做箱时间（日期）",
    "door_point": "门点/装卸工厂",
    "load_point": "装卸点",
    "load_address": "装卸货地址",
    "container_no": "箱号",
    "seal_no": "封条号",
    "vessel": "船名",
    "voyage": "航次",
    "ship_date": "船期",
    "discharge_port": "卸货港",
    "delivery_place": "交货地",
    "port_area": "港区",
    "pickup_point": "提箱点",
    "return_point": "还箱点",
    "pieces": "件数",
    "gross_weight": "毛重",
    "plate_no": "车牌号",
    "driver_name": "司机",
    "driver_phone": "司机手机",
    "fleet": "车队",
    "remark": "备注",
    "shipping_company": "船公司",
    "cargo_name": "货名",
    "port_open_time": "开港时间",
    "port_cut_time": "截港时间",
    "port_in_time": "进港时间",
}

# target 合法取值（json_schema enum 强约束）：字段名 + 应收费用名 + ignore
_AI_TARGETS: list[str] = [
    *_FIELD_CATALOG,
    *(f"fee:{name}" for name in RECEIVABLE_FEE_COLUMNS),
    "ignore",
]

# json_schema：AI 输出结构强约束（achat_json 优先走 structured output）。
# 输出即模板配置片段的输入：mapping（columns 段）+ two_row/fee_boundary 判定。
_AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "header_row": {
            "type": "integer",
            "description": "表头行号（1-based，在提供的前 15 行内）",
        },
        "two_row": {
            "type": "boolean",
            "description": "是否双行表头（表头上一行为区块名，如 应收/应付）",
        },
        "fee_boundary": {
            "type": "string",
            "enum": ["column_range", "section_header", "total_columns"],
            "description": "费用区边界模式：column_range=费用列在右侧连续区间 / "
            "section_header=双行表头区块分界 / total_columns=以应收合计列分界",
        },
        "mapping": {
            "type": "array",
            "description": "表头行每一列的目标映射（空白列不映射）",
            "items": {
                "type": "object",
                "properties": {
                    "col": {"type": "integer", "description": "列号（1-based）"},
                    "target": {
                        "type": "string",
                        "enum": _AI_TARGETS,
                        "description": "目标字段 / fee:费用名 / ignore",
                    },
                },
                "required": ["col", "target"],
            },
        },
        "confidence": {"type": "number", "description": "本次映射置信度 0-1"},
    },
    "required": ["header_row", "mapping", "confidence"],
}

_SYSTEM_PROMPT = (
    "你是竞品物流应收账单的表头映射助手。用户提供 Excel 前若干行的表格文本"
    "（行首为行号，竖线分隔单元格，空单元格为空），请定位表头行并把每一列"
    "映射到字段清单中的目标。规则：\n"
    "1. 只做表头映射，绝不输出、修改或猜测任何数据行内容；\n"
    "2. header_row 必须是表头行号（1-based）；mapping 覆盖表头行所有非空列；\n"
    "3. target 只能取字段清单中的值；应收金额列用 fee:费用名；\n"
    "4. 与解析无关的列（如状态、已收/付金额等）映射为 ignore；\n"
    "5. 同一列只映射一个目标；不要映射空白列；\n"
    "6. 双行表头（上一行为应收/应付等区块名）时 two_row=true，单行表头 false；\n"
    "7. fee_boundary 按表头结构判定：区块表头用 section_header，含合计分界列用"
    " total_columns，其余用 column_range。"
)


@dataclass
class AiHeaderResult:
    """AI 映射通过校验闸门后的结果（column_map/fee_map 可直接用于解析）。

    column_map 目标为标准字段（CanonicalOrder 字段名）；two_row/fee_boundary
    为模板结构判定（L3 固化进模板配置）；header_row 保留供新家族表头定位。
    """

    header_row: int
    column_map: dict[str, str]  # 归一化列名 → 标准字段名
    fee_map: dict[str, str]  # 归一化列名 → 标准费用名
    confidence: float
    two_row: bool = False
    fee_boundary: str = "column_range"
    new_fees: list[str] = field(default_factory=list)
    ignored_cols: set[int] = field(default_factory=set)  # AI 忽略列 + new_fee 降级列
    raw: dict[str, Any] = field(default_factory=dict)  # LLM 原始输出（报错详情用）


def _cell_text(value: Any) -> str:
    """单元格 → 文本并截断（AI 输入限长）。"""
    if value is None:
        return ""
    text = str(value).strip()
    return text[:_CELL_MAX_CHARS]


def format_header_zone(view) -> list[str]:
    """表头区文本行（前 MAX_HEADER_SCAN_ROWS 行 × 全部列，每格截断 20 字符）。

    行首为 1-based 行号，竖线分隔单元格；供 AI 输入与报错详情复用。
    """
    lines: list[str] = []
    scan_rows = min(view.nrows, MAX_HEADER_SCAN_ROWS)
    for row in range(1, scan_rows + 1):
        cells = [_cell_text(view.merged_cell(row, col)) for col in range(1, view.ncols + 1)]
        lines.append(f"{row} | " + " | ".join(cells))
    return lines


def _build_messages(zone_lines: list[str]) -> list[dict[str, str]]:
    """构造 AI 请求消息：字段清单 + 别名字典 + 表头区文本 + 输出要求。

    别名字典先查（L3 前道）：已收录的同义列名直接给出映射提示，未命中列
    才依赖 LLM 判断（命中率越高越省 token、越稳定）。
    """
    catalog = "\n".join(f"- {target}: {desc}" for target, desc in _FIELD_CATALOG.items())
    fee_targets = "、".join(f"fee:{name}" for name in RECEIVABLE_FEE_COLUMNS)
    ignored = "、".join(IGNORED_HEADERS)
    aliases = alias_dictionary()
    alias_lines = "\n".join(
        f"- {field}：{'/'.join(names)}" for field, names in aliases.items()
    )
    user_prompt = (
        "表头区文本（前 15 行，每格截断 20 字符，行首为行号）：\n"
        + "\n".join(zone_lines)
        + "\n\n字段清单（target 取值，含义：列中文名）：\n"
        + catalog
        + f"\n- {fee_targets}: 应收费用金额列（费用中文名）\n"
        + f"- ignore: 忽略列（如 {ignored} 等与解析无关的列）\n\n"
        "已知列名别名字典（表头列名命中以下别名时按对应字段映射，不必再猜）：\n"
        + alias_lines
        + "\n\n输出 {header_row, mapping: [{col, target}], two_row, fee_boundary, confidence}，"
        "mapping 覆盖表头行所有非空列。"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _normalize_fee_target(target: str) -> str | None:
    """fee:xxx → 标准费用名（过 HEADER_ALIASES 归一化）；白名单外返回 None。"""
    name = target.removeprefix("fee:").strip()
    name = HEADER_ALIASES.get(name, name)
    return name if name in RECEIVABLE_FEE_COLUMNS else None


def _to_money(value: Any) -> float | None:
    """费用单元格转金额（与 parser._to_money 同口径，抽样校验用）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _sample_check(
    view, header_row: int, data_cols: dict[int, str], fee_cols: dict[int, str]
) -> list[str]:
    """闸门 d：用映射试读数据行，返回失败原因列表（空 = 通过）。

    样本 = 表头行以下前 _SAMPLE_ROWS 个提单号列非空的行（尾部合计/箱量行
    提单号为空，天然不进样本，避免稀释匹配率）；提单号列样本匹配
    ^[A-Za-z0-9-.]+$（≥6 位）比例 ≥50%；费用列非空样本中数字可解析比例 ≥80%。
    """
    bl_col = next((col for col, target in data_cols.items() if target == "bl_no"), None)
    samples: list[int] = []
    for row in range(header_row + 1, view.nrows + 1):
        if bl_col is None:
            has_value = any(
                view.cell(row, col) is not None and str(view.cell(row, col)).strip()
                for col in range(1, view.ncols + 1)
            )
        else:
            has_value = bool(str(view.cell(row, bl_col) or "").strip())
        if has_value:
            samples.append(row)
        if len(samples) >= _SAMPLE_ROWS:
            break
    reasons: list[str] = []
    if not samples:
        # 表头下方无任何含提单号的数据行：header_row 错位（如 +1 漏掉首行数据）
        # 或提单号列映射错误 → 拒绝，不静默产出空/错行解析结果
        reasons.append("表头下方未找到含提单号的数据行（header_row 错位或提单号列映射可疑）")
        return reasons
    if bl_col is not None:
        matched = sum(
            1
            for row in samples
            if _BL_NO_SAMPLE_RE.fullmatch(str(view.cell(row, bl_col) or "").strip())
        )
        if matched / len(samples) < _BL_NO_MIN_RATIO:
            reasons.append(
                f"提单号列抽样不达标：{matched}/{len(samples)} 样本匹配 "
                f"^[A-Za-z0-9-.]+$（要求 ≥{_BL_NO_MIN_RATIO:.0%}）"
            )
    for fee_col in fee_cols:
        non_empty = [row for row in samples if str(view.cell(row, fee_col) or "").strip()]
        if not non_empty:
            continue
        numeric = sum(1 for row in non_empty if _to_money(view.cell(row, fee_col)) is not None)
        if numeric / len(non_empty) < _FEE_NUMERIC_MIN_RATIO:
            reasons.append(
                f"费用列（第 {fee_col} 列）抽样数字率不达标：{numeric}/{len(non_empty)} "
                f"可解析为数字（要求 ≥{_FEE_NUMERIC_MIN_RATIO:.0%}）"
            )
    return reasons


def _reject(raw: dict, reasons: list[str], zone_lines: list[str]) -> None:
    """校验失败：抛 BadRequestError（details 含 AI 映射 / 失败原因 / 表头原文）。"""
    log.warning("ai_header_mapping_rejected", extra={"reasons": reasons})
    raise BadRequestError(
        "AI header mapping failed validation",
        code="header_mapping_rejected",
        details={
            "ai_mapping": raw,
            "failure_reasons": reasons,
            "header_zone": zone_lines,
        },
    )


def build_llm_request(
    zone_lines: list[str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """构造 LLM 表头映射请求（纯函数）：返回 (messages, json_schema)。

    两段式编排在 async 层持本请求调用 achat_json（网络段真异步）。
    """
    return _build_messages(zone_lines), _AI_SCHEMA


def validate_ai_result(
    view, raw: dict, meta: dict, zone_lines: list[str]
) -> AiHeaderResult:
    """校验闸门 + 结果组装（纯 CPU，无 LLM 调用）：同步/两段两入口共用。

    四道闸门不过 → BadRequestError(header_mapping_rejected)（details 含
    AI 映射 / 失败原因 / 表头原文）；raw 非法形态（header_row/mapping 缺失
    等）同样闸门拒绝。编排层经 to_thread 执行（CPU 密集）。"""
    log.info(
        "ai_header_mapped",
        extra={
            "model": meta.get("model"),
            "header_row": raw.get("header_row"),
            "confidence": raw.get("confidence"),
        },
    )

    reasons: list[str] = []
    header_row = raw.get("header_row")
    # 上界取扫描行数与文件实际行数的较小值：小行数 .xls 文件（nrows<15）下
    # LLM 幻觉返回越界行号时走 400 header_mapping_rejected。注意必须在此
    # 立即拒绝——后续 header_texts 构建会按 header_row 读取单元格，xlrd
    # 越界抛 IndexError（.xlsx 虽容忍越界但列映射失真），与 .xlsx 行为对齐
    max_row = min(MAX_HEADER_SCAN_ROWS, getattr(view, "nrows", MAX_HEADER_SCAN_ROWS))
    if (
        not isinstance(header_row, int)
        or isinstance(header_row, bool)
        or not 1 <= header_row <= max_row
    ):
        _reject(
            raw,
            [f"header_row 非法：{header_row!r}（须为 1..{max_row} 的整数）"],
            zone_lines,
        )

    ncols = getattr(view, "ncols", 0)
    mapping = raw.get("mapping")
    if not isinstance(mapping, list):
        reasons.append("mapping 缺失或不是数组")
        mapping = []

    # 归一化列名 → target（列号转列名时对表头行取 merged 文本）；
    # 越界 col / 重复 col 条目丢弃（保留首个），非法 target 降级 ignore 并记录
    data_cols: dict[int, str] = {}
    fee_cols: dict[int, str] = {}
    seen_cols: set[int] = set()
    ignored_cols: set[int] = set()
    new_fees: list[str] = []
    skipped_targets: list[str] = []
    for item in mapping:
        if not isinstance(item, dict):
            continue
        col = item.get("col")
        target = item.get("target")
        if not isinstance(col, int) or not 1 <= col <= ncols:
            continue
        if col in seen_cols:
            continue
        seen_cols.add(col)
        if not isinstance(target, str):
            skipped_targets.append(str(target))
            continue
        if target in _FIELD_CATALOG:
            data_cols[col] = target
        elif target.startswith("fee:"):
            fee_name = _normalize_fee_target(target)
            if fee_name is None:
                # 白名单外费用：降级 ignore + 上报（闸门 c）
                new_fees.append(target.removeprefix("fee:").strip())
                ignored_cols.add(col)
            else:
                fee_cols[col] = fee_name
        elif target == "ignore":
            ignored_cols.add(col)
        else:
            skipped_targets.append(target)

    # 列号映射 → 归一化列名映射（模板持久化形态）
    header_texts = [
        str(view.merged_cell(header_row, col)).strip() if col else "" for col in range(1, ncols + 1)
    ]
    column_map = {
        _strip_whitespace(header_texts[col - 1]): target
        for col, target in data_cols.items()
        if header_texts[col - 1]
    }
    fee_map = {
        _strip_whitespace(header_texts[col - 1]): name
        for col, name in fee_cols.items()
        if header_texts[col - 1]
    }

    # 闸门 a：必映射字段齐全（TMS 通道必填：提单号 + 箱型，见《字段映射表》§4）
    for required in ("bl_no", "box_type_qty"):
        if required not in column_map.values():
            reasons.append(f"必映射字段缺失：{required}（{_FIELD_CATALOG[required]}）")

    # 闸门 b：字段重复映射（标准字段语义唯一，全部查重）
    dup = [
        target
        for target in column_map.values()
        if list(column_map.values()).count(target) > 1
    ]
    if dup:
        reasons.append(f"同一字段被多列重复映射：{sorted(set(dup))}")

    if skipped_targets:
        reasons.append(f"存在非法 target（已忽略）：{sorted(set(skipped_targets))}")
    if reasons:
        _reject(raw, reasons, zone_lines)

    # 闸门 d：抽样校验（此时 data_cols/fee_cols 以列号映射为准）
    sample_reasons = _sample_check(view, header_row, data_cols, fee_cols)
    if sample_reasons:
        _reject(raw, sample_reasons, zone_lines)

    # 模板结构判定（旧输出缺省：单行表头 + column_range）
    two_row = bool(raw.get("two_row"))
    fee_boundary = raw.get("fee_boundary")
    if fee_boundary not in ("section_header", "total_columns"):
        fee_boundary = "column_range"

    return AiHeaderResult(
        header_row=header_row,
        column_map=column_map,
        fee_map=fee_map,
        confidence=float(raw.get("confidence", 0.0)),
        two_row=two_row,
        fee_boundary=fee_boundary,
        new_fees=new_fees,
        ignored_cols=ignored_cols,
        raw=raw,
    )


def _strip_whitespace(text: str) -> str:
    """去全部空白（与 template 指纹归一化同口径）。"""
    return _HEADER_WHITESPACE_RE.sub("", text or "")
