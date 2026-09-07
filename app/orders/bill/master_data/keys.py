"""计数键归一（T17 口径，P-C 自 orchestrator 拆出）：纯函数叶子，无包内依赖。

计数键共用规则：全角→半角 + 删除全部空白（任何空白差异不产生新计数键）；
车牌额外全大写；工厂/司机为复合键。store 的 (kind, key, owner) 三维键中
的 key 即本模块产出。
"""

from __future__ import annotations

import re

# 空白归一：全部空白（含全角空格/连续空白）一律删除——任何空白差异都不产生
# 新计数键（防「锦煦 」/「锦　煦」/「锦 煦」算两个；宁合并不拆分）
_WHITESPACE_RE = re.compile(r"\s+")
_FULL_WIDTH_RE = re.compile(r"[\uFF01-\uFF5E]")


def _to_half_width(text: str) -> str:
    """全角 → 半角（全角字母/数字/符号；全角空格单独处理）。"""

    def _sub(ch: str) -> str:
        code = ord(ch)
        return chr(code - 0xFEE0) if 0xFF01 <= code <= 0xFF5E else ch

    return "".join(_sub(ch) for ch in text)


def normalize_key(text: str) -> str:
    """归一名：全角→半角 + 删除全部空白（计数键共用口径；见 _WHITESPACE_RE 注释）。"""
    if not text:
        return ""
    return _WHITESPACE_RE.sub("", _to_half_width(str(text))).strip()


def plate_key(plate: str | None) -> str:
    """车牌归一：全大写 + 去空白（T17 明确：车牌额外做全大写+去空格归一）。"""
    if not plate:
        return ""
    return normalize_key(str(plate)).upper()


def client_key(name: str | None) -> str:
    """客户计数键：归一名。"""
    return normalize_key(name)


def factory_key(name: str | None, address: str | None) -> str:
    """工厂计数键：「名+地址」复合键（口径 1；地址缺失时退化为按名）。"""
    return f"{normalize_key(name)}|{normalize_key(address)}"


def driver_key(name: str | None, plate: str | None) -> str:
    """司机+车辆计数键：「司机名+车牌」组合键（口径 2——同人换车/同车换人
    算不同档案；车牌缺失时退化为按司机名）。"""
    return f"{normalize_key(name)}|{plate_key(plate)}"
