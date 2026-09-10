"""费目归一化（T10）：费目名 → 标准费目码，三级解析全配置驱动。

1. 模板 `fees.mapping`（`区块.费目` → 标准费目码，支持 `{code, import, reconcile}` 扩展写法）；
2. 全局 `config/fee_alias_dictionary.yaml`（费目名 → 码，L3/新家族冷启动）；
3. `unmapped_fee` 策略：`to_other`（码=other，原名进备注）| `skip_report`（该列不生成记录）。

**模板外费目独立建档（用户拍板「按列名判定」）**：任何路径解析到
`other` 的列，只要原列名不是其它费近义名（fee_alias_dictionary 中 other 的
别名清单：其它费/其他费用/其它）→ 一律改用动态码（x+sha1(名)[:8]）作费目码——
费用以独立条目建档后挂账，不再并进「其它费」大杂烩；真其它费列（列名即近义名）
维持 other 归并语义。动态码经 fee_bootstrap 建档闭环（registry 登记 → apply 回填
price_id → 独立发射）；建档失败降级归并 other（apply_price_map 保底，不丢费）。

费目级 `import: false`（如税金）→ 不生成录入记录，金额计入对账排除项。
配置零竞品名/零费目名硬编码：新增家族/费目只改 YAML。
"""

from __future__ import annotations

import hashlib

import yaml

from app.core.config import settings
from app.core.logging_conf import get_logger

log = get_logger(__name__)

# unmapped_fee 策略取值
UNMAPPED_TO_OTHER = "to_other"
UNMAPPED_SKIP_REPORT = "skip_report"

_ALIAS_CACHE: dict[str, str] | None = None


def _dynamic_fee_code(name: str) -> str:
    """模板外费目动态码：ASCII 稳定唯一（registry 键 + 建档 sn 用）。

    中文费目名不能直接进 sn（{prefix}_{code} 拼写），拼音不可靠（多音字）；
    取 sha1 前 8 位 hex 作后缀，同名恒同码（幂等），跨批复用 registry。
    （移入本模块：模板外列独立建档的统一码规约，fee_bootstrap 复用）
    """
    return "x" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def is_dynamic_code(code: str) -> bool:
    """动态码判定（模板外费目码 x+8hex，_dynamic_fee_code 规约）：建档失败
    降级/保底语义用它识别；补 8 位 hex 校验防 x 前缀等长业务码误判（
    审查 S2：仅长度+前缀判定会把映射表显式 x 开头码误走归并降级）。"""
    return (
        bool(code)
        and len(code) == 9
        and code[0] == "x"
        and all(c in "0123456789abcdef" for c in code[1:])
    )


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
    path = settings.config_dir / "fee_alias_dictionary.yaml"
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
    `{code, import, reconcile, negative, negative_policy}` 扩展写法原样保留
    （默认 import/reconcile=True）。

    negative（T27a）：true → 金额取负录入（负向扣减项，如「扣除费」）；
    negative_policy=skip_report → 负项不录入仅对账（TMS 拒收负值时切回）。
    """
    if isinstance(entry, dict):
        policy = str(entry.get("negative_policy") or "").strip()
        negative = bool(entry.get("negative", False))
        return {
            "code": str(entry.get("code") or "").strip(),
            "import": bool(entry.get("import", True)),
            "reconcile": bool(entry.get("reconcile", True)),
            "negative": negative,
            "negative_policy": policy if policy in ("skip_report",) else "",
        }
    return {
        "code": str(entry or "").strip(),
        "import": True,
        "reconcile": True,
        "negative": False,
        "negative_policy": "",
    }


def other_alias_names() -> frozenset[str]:
    """其它费近义名集（fee_alias_dictionary 中码=other 的名字清单）。

    按列名判定（拍板）用：解析到 other 的列，列名在集内 = 真其它费
    列（维持归并语义）；不在集内 = 模板外费目（动态码独立建档发射）。
    """
    return frozenset(
        name for name, code in fee_alias_dictionary().items() if code == "other"
    )


def _resolve_other_by_name(code: str, name: str) -> str:
    """按列名判定收口：解析结果为 other 但列名不是其它费近义 → 动态码独立费目。"""
    if code == "other" and name not in other_alias_names():
        return _dynamic_fee_code(name)
    return code


def canonicalize_fee(
    section: str, name: str, fees_cfg: dict | None
) -> dict:
    """三级解析：费目名（区块.费目）→ {code, import, reconcile, negative, negative_policy}。

    模板 fees.mapping（区块.费目 或 费目 键）→ 全局别名字典 → unmapped_fee 策略：
    - to_other：码=other，原名进备注（真其它费列——列名即其它费近义名）
    - skip_report：{code: "", import: False}（不生成记录）
    - **模板外费目（列名非其它费近义）→ 动态码**（拍板：独立建档
      发射，不再并其它费；原名由调用方 name 字段携带进 note/建档）
    - negative（T27a）：负向扣减项标记（扣除费）；negative_policy=skip_report
      时负项不录入仅对账（import=False，金额仍参与对账恒等）。
    """
    key = f"{section}.{name}" if section else name
    mapping = (fees_cfg or {}).get("mapping") or {}
    entry = mapping.get(key) or mapping.get(name)
    if entry:
        meta = _parse_mapping_entry(entry)
        if meta["code"]:
            meta = dict(meta)
            meta["code"] = _resolve_other_by_name(meta["code"], name)
            if meta["negative"] and meta["negative_policy"] == UNMAPPED_SKIP_REPORT:
                # 降级兜底：负项不录入仅对账（金额进 excluded，恒等仍成立）
                meta["import"] = False
            return meta
    code = fee_alias_dictionary().get(name)
    if code:
        return {"code": code, "import": True, "reconcile": True, "negative": False, "negative_policy": ""}
    strategy = (fees_cfg or {}).get("unmapped_fee", UNMAPPED_TO_OTHER)
    if strategy == UNMAPPED_SKIP_REPORT:
        return {"code": "", "import": False, "reconcile": True, "negative": False, "negative_policy": ""}
    return {
        "code": _resolve_other_by_name("other", name),
        "import": True,
        "reconcile": True,
        "negative": False,
        "negative_policy": "",
    }


def canonicalize_fee_name(name: str) -> tuple[str, str | None]:
    """费目名 → (费目码, note)：字典命中取码、note=None；未命中归动态码（模板外
    费目独立建档，拍板）并原名进 note（与 to_other 同名口径——原名
    保留供建档命名/降级归并其它费）。

    供无模板上下文的转换路径（to_canonical 等）使用。真其它费近义名恒在字典
    （other 别名清单），不会落入本分支。
    """
    code = fee_alias_dictionary().get(name)
    if code:
        return code, None
    return _dynamic_fee_code(name), name
