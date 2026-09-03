"""TMS 箱型白名单校验（上传前拦截，2026-08-18 用户拍板）。

规则（叠加在既有「箱型非空即合法、非标表述放行」之上，不改变既有语义）：
- 账单箱型为标准代码形态（两位数字 + 2~3 位大写字母，如 40HQ/40GOH/45HC）
  时必须命中 `config/box_type_whitelist.yaml` 的 box_types，否则拦截该单并返回
  「系统没有此箱型：<箱型>，请联系客服」，不调下游 TMS；
- 非标准形态（大冷/拼箱/17M飞翼车/12T/B/L 等中文、车型、尺寸、单位表述）
  不校验，照常透传提交（既有「非空即合法」规则不变）；
- 白名单来源：TMS 下单界面箱型下拉提取的短代码（用户提供 2026-08-18）
  + 补充 40HQ/45HQ（live 实测可录/真实账单存在，用户确认补充）；
- 配置缺失/损坏 → 校验整体禁用（全放行 + 日志告警），绝不误拦核心箱型；
  配置更新后调用 reload_box_types 生效（模块级缓存，测试用 reload 重置）。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.core.logging_conf import get_logger

log = get_logger(__name__)

# 标准代码形态：两位数字 + 2~3 位大写字母（40HQ/40GOH/45HC/53HC/48FR…）；
# 与 normalizers 的 _BOX_ITEM_RE 同源（解析层匹配成功即视为标准代码候选）
_STANDARD_CODE_RE = re.compile(r"^\d{2}[A-Z]{2,3}$")

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "config"

_CACHE: frozenset[str] | None = None


def box_types_path() -> Path:
    """白名单文件路径：config/box_type_whitelist.yaml（测试可 monkeypatch）。"""
    return _CONFIG_DIR / "box_type_whitelist.yaml"


def load_box_types() -> frozenset[str]:
    """加载白名单（模块缓存；测试用 reload_box_types 重置）。

    缺文件/YAML 错误/结构不符 → 返回空集 + 日志告警（校验整体禁用）：
    白名单是拦截依据，宁可放行也不误拦核心箱型（40HQ 等实测可录箱型）。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = box_types_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = data.get("box_types") or []
        if not isinstance(entries, list):
            raise ValueError("box_types must be a list")
        box_types = frozenset(
            str(e).strip().upper() for e in entries if str(e).strip()
        )
    except (OSError, yaml.YAMLError, ValueError) as exc:
        log.warning(
            "box_whitelist_unavailable",
            extra={"path": str(path), "error_type": exc.__class__.__name__},
        )
        box_types = frozenset()
    _CACHE = box_types
    return _CACHE


def reload_box_types() -> frozenset[str]:
    """重置白名单缓存（测试用；配置变更热加载）。"""
    global _CACHE
    _CACHE = None
    return load_box_types()


def is_standard_code(text: str) -> bool:
    """标准代码形态（两位数字 + 2~3 位大写字母）？非标准表述 → False（不校验）。"""
    return bool(_STANDARD_CODE_RE.match(text.strip().upper()))


def check_unknown_box_types(box_types: list[str]) -> list[str]:
    """过滤出「标准代码形态但不在白名单」的箱型（去重保序）；空列表 = 全部放行。

    非标准表述（大冷/拼箱/17M飞翼车/12T 等）一律不过滤（既有规则放行）。
    """
    whitelist = load_box_types()
    if not whitelist:
        return []  # 配置缺失/损坏 → 校验禁用，全放行
    unknown: list[str] = []
    seen: set[str] = set()
    for raw in box_types:
        if not raw:
            continue
        text = str(raw).strip().upper()
        if not is_standard_code(text) or text in whitelist:
            continue
        if text not in seen:
            seen.add(text)
            unknown.append(text)
    return unknown
