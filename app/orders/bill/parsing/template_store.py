"""模板库：YAML 模板加载 + 三级识别（L1 指纹精确 / L2 族级近似 / 未命中触发 L3）。

层级关系（2026-09 命名统一）：本模块是**现役** YAML 模板库
+ 三级识别（L1/L2/L3）；template.py 是旧版单模板持久化（内置模板 +
历史固化 JSON），仅作为本库未命中时的回退读取——两套指纹算法
（本模块 md5-8 / template.py sha1-16 经 compute_legacy_fingerprint）
分别对应各自库键，**不可互相替代**。


指纹算法（《模板配置初稿》约定）：md5(表头行非空单元格去空白后以 '¶' 连接)[:8]。
- L1：扫描前 MAX_HEADER_SCAN_ROWS 行，某行指纹 ∈ 模板 match.fingerprints → 精确命中，
  该行即表头行（列名行；two_row 家族的上方区块行不参与指纹）；
- L2：无 L1 命中时，按行级列名集合与模板期望列名集合（columns+fees 源列名）的重合度
  ≥ family_min_overlap 做族级近似（应对同家族表头逐年漂移）；缺失列记入 missing 不报错；
- 均未命中返回 None，由调用方触发 L3 AI 映射（ai_header）。

模板来源两类，同一识别入口：
- YAML 配置库（templates/*.yaml，启动加载，新增家族只加配置）；
- AI 固化模板（storage/bill_templates/*.json，L3 人工确认后落盘，指纹为旧算法
  sha1 前 16 位，见 template.py——本模块一并查询，保证既有固化模板仍可命中）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.core.logging_conf import get_logger

from ..schema import MAX_HEADER_SCAN_ROWS

log = get_logger(__name__)

# 模板配置目录（项目根 templates/，与 storage 运行时数据分离）
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "templates"

# 表头文本中的空白（含全角空格），与 parser 表头归一化口径一致
_HEADER_WHITESPACE_RE = re.compile(r"[\s\u3000]+")

# 列名消歧后缀：配置源列名「车牌号#1」→ 参与指纹/重合度比较时取「车牌号」
_COL_ORDINAL_RE = re.compile(r"^(.*?)#\d+$")


def _normalize(text: str) -> str:
    """表头文本归一化：去除全部空白（指纹与列名比较共用口径）。"""
    return _HEADER_WHITESPACE_RE.sub("", text or "")


def _source_col_names(template: dict) -> set[str]:
    """模板期望列名集合：columns + fees 的源列名（去空白、去 #N 消歧后缀、去区块前缀）。

    「区块.列名」（two_row 家族）取列名部分参与重合度比较——区块名不参与，
    因为 L2 扫描的是列名行；「列名#N」取 # 前部分（同名列消歧不影响列名集合）。
    fees 段仅旧 schema（费目名 → 源列名，jinxin_v1 既有语义）参与；新 schema
    （T10 费用通道配置，含 channels 键）费用列自动发现/配置声明，不参与 L2。
    2026-09-03：match.optional_headers（识别可选列，如新式样手机号/车牌——抽取
    需要但缺列不应降重合度）从期望集合剔除，避免加列挤掉精简/旧式样文件的 L2 命中。
    """
    names: set[str] = set()
    for value in template.get("columns", {}).values():
        for col in value if isinstance(value, list) else [value]:
            col = str(col).strip()
            col = col.split(".", 1)[-1]
            m = _COL_ORDINAL_RE.match(col)
            if m:
                col = m.group(1)
            if col:
                names.add(_normalize(col))
    fees = template.get("fees", {}) or {}
    if not (isinstance(fees, dict) and "channels" in fees):
        # 旧 fees schema（费目名 → 源列名）参与 L2；新 schema 自动发现不参与
        for value in fees.values():
            for col in value if isinstance(value, list) else [value]:
                col = str(col).strip()
                if col:
                    names.add(_normalize(col))
    optional = template.get("match", {}).get("optional_headers") or []
    if optional:
        names -= {_normalize(str(h)) for h in optional if str(h).strip()}
    return names


def compute_fingerprint(headers: list[str]) -> str:
    """表头行指纹：非空单元格去空白后以 '¶' 连接，md5 前 8 位。"""
    parts = [_normalize(h) for h in headers if str(h or "").strip()]
    return hashlib.md5("¶".join(parts).encode("utf-8")).hexdigest()[:8]


@dataclass
class TemplateMatch:
    """识别结果：命中模板 + 表头行号 + 命中级别（L1/L2）+ 缺失列清单。"""

    template: dict
    header_row: int  # 1-based 列名行
    level: str  # "L1" | "L2"
    fingerprint: str | None = None  # L1 命中时的表头指纹
    missing: list[str] = field(default_factory=list)  # L2：配置有而文件无的列
    template_id: str = ""


def _load_yaml_templates() -> dict[str, dict]:
    """加载 templates/*.yaml 全部模板；单个损坏跳过并告警，不阻断启动。"""
    loaded: dict[str, dict] = {}
    if not _TEMPLATES_DIR.is_dir():
        log.warning("templates_dir_missing", extra={"path": str(_TEMPLATES_DIR)})
        return loaded
    for path in sorted(_TEMPLATES_DIR.glob("*.yaml")):
        if path.name == "alias_dictionary.yaml":
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            log.warning("template_yaml_invalid", extra={"file": path.name, "error": str(exc)})
            continue
        if not isinstance(data, dict) or "template_id" not in data:
            log.warning("template_yaml_invalid", extra={"file": path.name, "reason": "no template_id"})
            continue
        loaded[data["template_id"]] = data
    return loaded


# 模板库缓存：启动时惰性加载（首次 identify 触发）；测试可用 reload_templates() 重置
_TEMPLATE_CACHE: dict[str, dict] | None = None


def reload_templates() -> dict[str, dict]:
    """重新加载 YAML 模板库（测试/热更新用），返回模板字典。"""
    global _TEMPLATE_CACHE
    _TEMPLATE_CACHE = _load_yaml_templates()
    return _TEMPLATE_CACHE


def all_templates() -> dict[str, dict]:
    """全部 YAML 模板（template_id → 配置），启动后首次调用加载。"""
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        reload_templates()
    return _TEMPLATE_CACHE


def _row_headers(view, row: int) -> list[str]:
    """行内全部单元格文本（合并填充，与 parser 表头读取口径一致）。"""
    return [
        str(view.merged_cell(row, col)).strip() if view.merged_cell(row, col) is not None else ""
        for col in range(1, view.ncols + 1)
    ]


def _row_nonempty(view, row: int) -> set[str]:
    """行内非空单元格去空白后的列名集合。"""
    return {_normalize(h) for h in _row_headers(view, row) if h.strip()}


def identify(view) -> TemplateMatch | None:
    """三级识别主入口：L1 指纹精确命中 → L2 族级近似 → 未命中返回 None（触发 L3）。

    L1 在同一行可能命中多模板（理论上），按 template_id 字典序取第一个（确定性）；
    L2 取重合度最高（并列时 template_id 字典序优先）的候选。
    """
    templates = all_templates()
    scan_rows = min(view.nrows, MAX_HEADER_SCAN_ROWS)

    # L1：指纹精确命中
    for row in range(1, scan_rows + 1):
        headers = _row_headers(view, row)
        if not any(h.strip() for h in headers):
            continue
        fingerprint = compute_fingerprint(headers)
        for template_id in sorted(templates):
            fingerprints = templates[template_id].get("match", {}).get("fingerprints") or []
            if fingerprint in fingerprints:
                log.info(
                    "template_l1_hit",
                    extra={"template_id": template_id, "header_row": row, "fingerprint": fingerprint},
                )
                return TemplateMatch(
                    template=templates[template_id],
                    header_row=row,
                    level="L1",
                    fingerprint=fingerprint,
                    template_id=template_id,
                )

    # L2：列名集合重合度 ≥ family_min_overlap（族级近似，应对逐年漂移）
    best: TemplateMatch | None = None
    best_overlap = 0.0
    for row in range(1, scan_rows + 1):
        file_cols = _row_nonempty(view, row)
        if not file_cols:
            continue
        for template_id, template in templates.items():
            expected = _source_col_names(template)
            if not expected:
                continue
            overlap = len(file_cols & expected) / len(expected)
            min_overlap = float(
                template.get("match", {}).get("family_min_overlap", 0.85)
            )
            if overlap < min_overlap:
                continue
            # 重合度更高，或同分按 template_id 字典序更小 → 替换候选
            if overlap > best_overlap or (
                overlap == best_overlap
                and (best is None or template_id < best.template_id)
            ):
                missing = sorted(expected - file_cols)
                best = TemplateMatch(
                    template=template,
                    header_row=row,
                    level="L2",
                    missing=missing,
                    template_id=template_id,
                )
                best_overlap = overlap
    if best is not None:
        log.info(
            "template_l2_hit",
            extra={
                "template_id": best.template_id,
                "header_row": best.header_row,
                "overlap": round(best_overlap, 3),
                "missing": best.missing,
            },
        )
    return best


def load_storage_template(fingerprint: str):
    """查询 AI 固化模板（storage/bill_templates/*.json，旧 sha1 指纹）。

    与 YAML 库互补：L3 固化模板不依赖 YAML 即可被 L1 命中（兼容既有
    template.py 的固化机制）；返回 None 表示未命中。
    函数内 lazy import：template_store ↔ template（_build_builtin 反向依赖本模块）
    互引，模块级双向 import 会环。
    """
    from .legacy_template import load_template

    return load_template(fingerprint)


# ---- 字段别名字典（L3 AI 映射前先查此表） ----

_ALIAS_CACHE: dict[str, list[str]] | None = None


def alias_dictionary() -> dict[str, list[str]]:
    """字段别名字典（templates/alias_dictionary.yaml）：标准字段 → 源列名别名。

    L3 AI 映射前先查此表（命中率越高越省 token、越稳定）；随模板接入持续累积。
    """
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None:
        return _ALIAS_CACHE
    path = _TEMPLATES_DIR / "alias_dictionary.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.warning("alias_dictionary_load_failed", extra={"path": str(path)})
        data = {}
    _ALIAS_CACHE = {k: [str(v) for v in vals] for k, vals in data.items() if isinstance(vals, list)}
    return _ALIAS_CACHE


# ---- 费目名动态收集（2026-09-04：费用项不写死，以模板库为唯一声明源） ----
# 模板 fees 段两种 schema：旧式（费目名 → 源列名，全应收）与 T10 channels
# （mapping 键「区块.费目名」→ 码）。两函数皆按模板文件序去重保序。

# legacy 池空兜底（2026-09-04 审查修复）：模板库空/全为 channels schema 时，
# 内置/精确兜底链的费用识别不能静默归零（会丢 shou/金额且无告警）——回退
# 金科信基线六费目并显式告警，保证兜底链恒可识别
_LEGACY_FALLBACK_FEES: tuple[str, ...] = (
    "运费", "待时费", "预提费", "洋山费", "落还箱费", "其它费"
)


def collect_fee_names() -> list[str]:
    """应收费目名宽池：模板库全部应收费用名（旧式键 + channels 中映射到 shou
    的费目名）。L3 AI 映射的可选费用目标（模板加费目 → AI 池自动扩）。"""
    names: list[str] = []
    seen: set[str] = set()
    for template in all_templates().values():
        fees = template.get("fees") or {}
        if not isinstance(fees, dict):
            continue
        if "channels" in fees:
            channels = fees["channels"] or {}
            shou_sections = {s for s, ch in channels.items() if ch == "shou"}
            for key in (fees.get("mapping") or {}):
                section, _, name = str(key).partition(".")
                if section in shou_sections and name and name not in seen:
                    seen.add(name)
                    names.append(name)
        else:
            for name in fees:
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
    return names


def collect_legacy_fee_names() -> list[str]:
    """旧式（BillRow 语义）费目名：仅无 channels 的模板 fees 键。

    exact/内置模板兜底链与 jinxin 同语义（费用键 = AddWork 表单应收字段），
    只收旧式声明——jinxin_v1 模板加费目即自动扩展，不混入 T10 家族费目。
    池空（库空/模板损坏/全 channels）时回退 _LEGACY_FALLBACK_FEES 并告警，
    避免兜底链费用静默归零（见 _LEGACY_FALLBACK_FEES 注释）。
    """
    names: list[str] = []
    seen: set[str] = set()
    for template in all_templates().values():
        fees = template.get("fees") or {}
        if not isinstance(fees, dict) or "channels" in fees:
            continue
        for name in fees:
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    if not names:
        log.warning("legacy_fee_pool_empty_fallback_used")
        return list(_LEGACY_FALLBACK_FEES)
    return names


def reload_alias_dictionary() -> None:
    """重置别名字典缓存（测试用）。"""
    global _ALIAS_CACHE
    _ALIAS_CACHE = None


# ---- L3 模板配置固化（AI 映射 → 人工确认 → templates/{family}_v1.yaml） ----

# L3 候选配置的默认归一化集（模板无关，全局共享）：AI 只输出列映射，
# 常用脏数据形态的清洗规则按字段名默认注入（box_type_qty 不归一化则
# 无法聚合成必填的 box_groups）
_L3_DEFAULT_NORMALIZERS: dict[str, str] = {
    "bl_no": "strip_float_tail",
    "biz_no": "strip_float_tail",
    "plate_no": "strip_float_tail",
    "box_type_qty": "box_parse",
    "order_date": "date_flex",
    "work_date": "date_flex",
    "ship_date": "date_flex",
    "port_open_time": "date_flex",
    "port_cut_time": "date_flex",
    "port_in_time": "date_flex",
    "pieces": "to_int",
    "gross_weight": "to_number",
}


def build_template_config(
    *,
    fingerprint: str,
    header_row: int,
    two_row: bool,
    fee_boundary: str,
    column_map: dict[str, str],
    family: str = "auto",
    ignored_cols: set[int] | None = None,
) -> dict:
    """L3 AI 映射结果 → 模板配置片段（YAML 固化载体）。

    产出合法模板配置（含 match.fingerprints/required/group_key 默认段与默认
    归一化集）；columns 按 AI 映射逐列生成；AI 忽略列记录于 _ignored_cols
    （不参与未识别列告警，固化时剔除下划线私有键）。
    """
    normalizers = {
        field: _L3_DEFAULT_NORMALIZERS[field]
        for field in column_map.values()
        if field in _L3_DEFAULT_NORMALIZERS
    }
    config = {
        "template_id": f"{family}_v1",
        "family": family,
        "name": f"AI映射-{family}",
        "match": {"fingerprints": [fingerprint], "family_min_overlap": 0.85},
        "header": {"row_anchor": "序号", "two_row": two_row},
        "data": {"row_filter": "seq_numeric", "stop_on": ["合计"]},
        "fee_boundary": fee_boundary,
        "required": ["bl_no", "box_type_qty"],
        "group_key": {"primary": "bl_no", "fallback": "biz_no"},
        "columns": {target: [col] for col, target in sorted(column_map.items())},
        "normalizers": normalizers,
        "_l3_header_row": header_row,
    }
    if ignored_cols:
        config["_ignored_cols"] = sorted(ignored_cols)
    return config


def save_yaml_template(template: dict, family: str) -> Path:
    """L3 模板固化为 templates/{family}_v1.yaml（临时文件 + 原子替换）。

    固化前按 family 更新 template_id/family/name（候选配置默认 auto，人工
    确认时指定家族名）；固化后立即重载模板库缓存，同指纹再次上传直接 L1 命中。
    """
    path = _TEMPLATES_DIR / f"{family}_v1.yaml"
    payload = {k: v for k, v in template.items() if not k.startswith("_")}
    payload["template_id"] = f"{family}_v1"
    payload["family"] = family
    payload["name"] = f"AI映射-{family}"
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    reload_templates()
    return path
