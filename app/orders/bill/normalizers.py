"""模板无关的字段归一化函数库（配置驱动引用，全局共享）。

竞品账单的脏数据形态（浮点尾巴/月-日日期/箱型×数量/船名航次合并列）与具体
模板无关，全部收敛为这里的一组纯函数；模板配置的 `normalizers:` 段按字段名
引用（如 `bl_no: strip_float_tail`），新家族接入时零代码。

函数契约：入参为单元格原文（str）或引擎原生值（float/datetime），返回归一化
后的值；解析失败统一返回 None（字段缺失语义），不抛异常。
"""

from __future__ import annotations

import re
from datetime import date, datetime

# ---- 日期 ----

# 月-日（账单内通常只有月-日，如 "9-1"）
_MONTH_DAY_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
# 完整日期（YYYY-M-D 或 YYYY-MM-DD）
_FULL_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
# 带时间的日期串（"2020-12-11 12:05"，取日期部分）
_DATETIME_STR_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T]")
# 文件名中的结算区间（"2018-01到2018-12" / "2020-10" / "2018-01到2019-12"）
_FILENAME_RANGE_RE = re.compile(r"(\d{4})-(\d{1,2})到(\d{4})-(\d{1,2})")
# 文件名中的单个日期区间（"利润明细表(2021-08-01-2021-12-31)"）
_FILENAME_PERIOD_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})")
# 文件名中的年-月或年字（"2020-10" / "2017年对账单"）
_FILENAME_YEAR_RE = re.compile(r"(\d{4})(?:-(\d{1,2})|年)")
# 表头上方结算日期行（"结算日期：2018-01-01-2018-12-31"）
_SETTLEMENT_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

# 跨年区间内 M-D 的年份归属分界：M ≤ 6 归结束年（次年 1-6 月），M > 6 归起始年
# （12 月属区间起点年份）。示例：区间 2018-01~2019-12，"12-7"→2018-12-07、
# "1-5"→2019-01-05。近似规则，实测冲突时以实测调整配置或本函数。
_YEAR_SPLIT_MONTH = 6

# ---- 数字尾巴 ----

# 浮点尾巴：纯数字 + .0+（xlrd/openpyxl 数字单元格，如 574848870.0）
_FLOAT_TAIL_RE = re.compile(r"^\d+\.0+$")

# ---- 箱型 ----

# 标准箱型：两位数字 + 2~3 位大写字母（40HQ/20GP/45RH/40HC…），可选 *数量
_BOX_ITEM_RE = re.compile(r"(\d{2}[A-Z]{2,3})(?:\s*\*\s*(\d+))?")
# 箱型字段内的多个箱型分隔符（"40GP+40HQ" / "40GP、40HQ"）
_BOX_SPLIT_RE = re.compile(r"[+、,，]")
# 箱型原文中的描述性后缀（"20GP 主段" / "40HQ*2 主段"），参与解析的字符集
_BOX_TOKEN_RE = re.compile(r"\d{2}[A-Z]{2,3}\s*\*\s*\d+|\d{2}[A-Z]{2,3}")


def _is_valid_date(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
        return True
    except ValueError:
        return False


def parse_year_hint(
    filename: str, settlement_text: str | None = None
) -> tuple[int, int] | None:
    """从文件名或表头上方结算日期行提取年份区间 (start_year, end_year)。

    优先级：结算日期行（含完整日期，最精确）→ 文件名。两种来源都解析不到
    返回 None（调用方不补年份，M-D 日期原样保留）。
    """
    if settlement_text:
        dates = _SETTLEMENT_RE.findall(settlement_text)
        if len(dates) >= 2:
            start_year = int(dates[0][0])
            end_year = int(dates[-1][0])
            if start_year <= end_year:
                return start_year, end_year
    if not filename:
        return None
    m = _FILENAME_RANGE_RE.search(filename)
    if m:
        return int(m.group(1)), int(m.group(3))
    m = _FILENAME_PERIOD_RE.search(filename)
    if m:
        return int(m.group(1)), int(m.group(4))
    m = _FILENAME_YEAR_RE.search(filename)
    if m:
        return int(m.group(1)), int(m.group(1))
    return None


def date_flex(value, year_hint: tuple[int, int] | None = None) -> str | None:
    """日期归一化 → YYYY-MM-DD；解析失败返回 None。

    支持三种输入形态：
    - `1-1`（M-D）：年份来自 year_hint；跨年区间（end > start）时 12→1 月进位
      （M ≤ 6 归结束年，M > 6 归起始年），单年区间直接用该年
    - `2021-01-01` / `2020-12-11 12:05`：完整日期（后者取日期部分）
    - datetime 对象（.xls 引擎原生值）
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None

    m = _FULL_DATE_RE.match(text)
    if m:
        year, month, day = (int(g) for g in m.groups())
        if _is_valid_date(year, month, day):
            return f"{year:04d}-{month:02d}-{day:02d}"
        return None
    m = _DATETIME_STR_RE.match(text)
    if m:
        year, month, day = (int(g) for g in m.groups())
        if _is_valid_date(year, month, day):
            return f"{year:04d}-{month:02d}-{day:02d}"
        return None

    m = _MONTH_DAY_RE.match(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        year = _pick_year(month, day, year_hint)
        if year is None or not _is_valid_date(year, month, day):
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _pick_year(month: int, day: int, year_hint: tuple[int, int] | None) -> int | None:
    """M-D 日期补年份：year_hint 为 (start_year, end_year)；解析失败返回 None。

    单年区间直接取该年；跨年区间按 12→1 月进位规则归年（M ≤ 6 → end_year，
    M > 6 → start_year），保证「12-31 后接 1-1」的年序连续。
    """
    if year_hint is None:
        return None
    start_year, end_year = year_hint
    if start_year == end_year:
        return start_year
    if start_year > end_year:
        return None
    return end_year if month <= _YEAR_SPLIT_MONTH else start_year


def strip_float_tail(value) -> str | None:
    """去浮点尾巴：`574848870.0` → `574848870`、`6588.0` → `6588`。

    仅处理「纯数字 + .0+」形态（Excel 数字单元格）；非数字字符串原样返回
    （封条号等混文本不误伤）；空值返回 None。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _FLOAT_TAIL_RE.match(text):
        return text.split(".")[0]
    return text


def box_parse(value) -> list[dict] | None:
    """箱型解析 → [{"type": 箱型, "qty": 数量}]；空值返回 None。

    - `40HQ` → qty=1；`40HQ*2` → qty=2；`40GP+40HQ` → 两条
    - `20GP 主段` 等带描述后缀的原文 → 清洗后缀后解析
    - 非标箱型（大冷/飞翼/拼箱/2X20 等不匹配标准正则）→ 原样保留 type
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    items: list[dict] = []
    seen: set[str] = set()
    for part in _BOX_SPLIT_RE.split(text):
        part = part.strip()
        if not part:
            continue
        m = _BOX_ITEM_RE.search(part)
        if m:
            box_type = m.group(1)
            qty = int(m.group(2)) if m.group(2) else 1
        else:
            # 非标箱型：整段原文作为 type，qty=1（保留给下游人工确认）
            box_type, qty = part, 1
        if box_type not in seen:
            seen.add(box_type)
            items.append({"type": box_type, "qty": qty})
    return items or None


def vessel_voyage_split(value) -> tuple[str | None, str | None]:
    """船名/航次合并列拆分 → (船名, 航次)；无 `/` 时整体归船名。

    示例：`MAERSK SARNIA/752E` → ("MAERSK SARNIA", "752E")。
    按最后一个 `/` 分割（船名可能含空格，航次不含）。
    """
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    vessel, sep, voyage = text.rpartition("/")
    if not sep:
        return text, None
    vessel = vessel.strip() or None
    voyage = voyage.strip() or None
    return vessel, voyage


def to_int(value) -> int | None:
    """件数 → int；空值/非数字 → None（缺失语义，不报错）。

    兼容 `12`、`12.0`（Excel 数字单元格）两种形态。
    """
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def to_number(value) -> float | None:
    """毛重 → float；空值/非数字 → None（缺失语义，不报错）。"""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


# 配置引用的归一化函数注册表：模板配置 `normalizers:` 段引用的名字 → 实现。
# 新归一化规则在此登记即可被所有模板引用（列名 → 函数签名保持一致：value 为
# 首个参数；date_flex 的 year_hint 由解析管线注入，不在此表签名内体现）。
NORMALIZER_REGISTRY: dict[str, object] = {
    "date_flex": date_flex,
    "strip_float_tail": strip_float_tail,
    "box_parse": box_parse,
    "vessel_voyage_split": vessel_voyage_split,
    "to_int": to_int,
    "to_number": to_number,
}
