"""费目归一化（T10）：费目名 → 标准费目码，三级解析全配置驱动。

1. 模板 `fees.mapping`（`区块.费目` → 标准费目码，支持 `{code, import, reconcile}` 扩展写法）；
2. 全局 `config/fee_alias_dictionary.yaml`（费目名 → 码，L3/新家族冷启动）；
3. `unmapped_fee` 策略：`to_other`（码=other，原名进备注）| `skip_report`（该列不生成记录）。

费目级 `import: false`（如税金）→ 不生成录入记录，金额计入对账排除项。
配置零竞品名/零费目名硬编码：新增家族/费目只改 YAML。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.logging_conf import get_logger

log = get_logger(__name__)

# 全局费目别名字典目录（config/，与 templates/ 字段别名字典分离）
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "config"

# unmapped_fee 策略取值
UNMAPPED_TO_OTHER = "to_other"
UNMAPPED_SKIP_REPORT = "skip_report"

_ALIAS_CACHE: dict[str, str] | None = None


def is_new_fee_schema(fees_cfg: dict) -> bool:
    """fees 段 schema 判定：含 channels 键 → 新 schema（T10 费用通道配置）；
    否则旧 schema（费目名 → 源列名，jinxin_v1 既有语义）。"""
    return bool(fees_cfg) and "channels" in fees_cfg


def fee_alias_dictionary() -> dict[str, str]:
    """费目别名字典（费目名 → 标准费目码），config/fee_alias_dictionary.yaml。

    反查表构建：YAML 为 `code: [费目名列表]`；文件缺失/损坏 → 空字典（仅影响
    归一化命中率，不阻断——mapping 显式映射仍可用）。
    """
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None:
        return _ALIAS_CACHE
    path = _CONFIG_DIR / "fee_alias_dictionary.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.warning("fee_alias_dictionary_load_failed", extra={"path": str(path)})
        data = {}
    lookup: dict[str, str] = {}
    for code, names in data.items():
        if not isinstance(names, list):
            continue
        for name in names:
            key = str(name).strip()
            if key and key not in lookup:
                lookup[key] = str(code)
    _ALIAS_CACHE = lookup
    return _ALIAS_CACHE


def reload_fee_alias_dictionary() -> None:
    """重置费目别名字典缓存（测试用）。"""
    global _ALIAS_CACHE
    _ALIAS_CACHE = None


def _parse_mapping_entry(entry) -> dict:
    """模板 mapping 条目归一：`freight` → {code, import, reconcile}；
    `{code, import, reconcile}` 扩展写法原样保留（默认 import/reconcile=True）。"""
    if isinstance(entry, dict):
        return {
            "code": str(entry.get("code") or "").strip(),
            "import": bool(entry.get("import", True)),
            "reconcile": bool(entry.get("reconcile", True)),
        }
    return {
        "code": str(entry or "").strip(),
        "import": True,
        "reconcile": True,
    }


def canonicalize_fee(
    section: str, name: str, fees_cfg: dict | None
) -> dict:
    """三级解析：费目名（区块.费目）→ {code, import, reconcile}。

    模板 fees.mapping（区块.费目 或 费目 键）→ 全局别名字典 → unmapped_fee 策略：
    - to_other：{code: other, import: True, note=原名}
    - skip_report：{code: "", import: False}（不生成记录）
    """
    key = f"{section}.{name}" if section else name
    mapping = (fees_cfg or {}).get("mapping") or {}
    entry = mapping.get(key) or mapping.get(name)
    if entry:
        meta = _parse_mapping_entry(entry)
        if meta["code"]:
            return meta
    code = fee_alias_dictionary().get(name)
    if code:
        return {"code": code, "import": True, "reconcile": True}
    strategy = (fees_cfg or {}).get("unmapped_fee", UNMAPPED_TO_OTHER)
    if strategy == UNMAPPED_SKIP_REPORT:
        return {"code": "", "import": False, "reconcile": True}
    return {"code": "other", "import": True, "reconcile": True}


def canonicalize_fee_name(name: str) -> tuple[str, str | None]:
    """费目名 → (标准费目码, note)：未命中字典 → (other, 原名进 note)。

    供无模板上下文的转换路径（to_canonical 等）使用：字典命中取码、note=None；
    未命中归 other 并原名进 note（to_other 语义）。
    """
    code = fee_alias_dictionary().get(name)
    if code:
        return code, None
    return "other", name
