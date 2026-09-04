"""中文大写金额解析（账单「合计大写」锚点专用）。

合计大写由账单导出时写死、不经公式，可用作费用总额第二锚点（不受 SUM 公式
缓存旧值影响）；由 aggregator._collect_anchors 采集、reconciliation 对账消费。
支持 零壹贰叁肆伍陆柒捌玖、拾佰仟万亿、元角分、整。
"""

from __future__ import annotations

import re

_CN_UPPER_DIGITS = {
    "零": 0,
    "壹": 1,
    "贰": 2,
    "叁": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
}
_CN_UPPER_DEC_UNITS = {"拾": 10, "佰": 100, "仟": 1000}
_CN_UPPER_BIG_UNITS = {"万": 10000, "亿": 100000000}
_CN_MONEY_UNITS = ("元", "角", "分")
_CN_UPPER_SEGMENT_RE = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元角分整]+")


def _parse_cn_integer(s: str) -> float:
    """中文大写整数段式解析（拾佰仟万亿），空串 → 0；「拾」等省壹写法按 1 计。"""
    if not s:
        return 0.0
    total = 0.0
    section = 0.0
    num = 0
    for ch in s:
        if ch in _CN_UPPER_DIGITS:
            num = _CN_UPPER_DIGITS[ch]
        elif ch in _CN_UPPER_DEC_UNITS:
            section += (num if num else 1) * _CN_UPPER_DEC_UNITS[ch]
            num = 0
        elif ch in _CN_UPPER_BIG_UNITS:
            # 万/亿前数字缺失（num=0 且本段无累计）才按省壹补 1，如「万」=1 万；
            # 「贰拾万」num=0 但 section=20，不补（否则误加 1 万）
            section = (section + (num if (num or section) else 1)) * _CN_UPPER_BIG_UNITS[ch]
            total += section
            section = 0.0
            num = 0
    return total + section + num


def parse_cn_upper_amount(text: str | None) -> float | None:
    """解析中文大写金额 → float；无大写金额段 → None。

    支持 零壹贰叁肆伍陆柒捌玖、拾佰仟万亿、元角分、整：
    「壹佰壹拾壹万贰仟肆佰零伍元整」→ 1112405.0；「壹元贰角叁分」→ 1.23；「伍角」→ 0.5。
    """
    if not text:
        return None
    segment = next(
        (s for s in _CN_UPPER_SEGMENT_RE.findall(text) if any(u in s for u in _CN_MONEY_UNITS)),
        None,
    )
    if segment is None:
        return None
    # 段内须含数字或整数单位（拾佰仟万亿），仅货币单位（如孤「元」）不构成金额
    if not any(
        ch in _CN_UPPER_DIGITS or ch in _CN_UPPER_DEC_UNITS or ch in _CN_UPPER_BIG_UNITS
        for ch in segment
    ):
        return None
    if "元" in segment:
        int_part, _, tail = segment.partition("元")
    else:
        int_part, tail = "", segment
    amount = _parse_cn_integer(int_part)
    if "角" in tail:
        jiao_part, _, tail = tail.partition("角")
        amount += _parse_cn_integer(jiao_part) * 0.1
    if "分" in tail:
        fen_part, _, _ = tail.partition("分")
        amount += _parse_cn_integer(fen_part) * 0.01
    return round(amount, 2)
