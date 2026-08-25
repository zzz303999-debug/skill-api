"""自由文本订单纯解析器：只读显式标签；做箱时间会归一为 YYYY-MM-DD。"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError

from app.errors import BadRequestError
from app.skills.tuoshu.normalizer import normalize_date_value

from .schema import BoxItem, DriverItem, OrderTextExtraction

_LABELS: dict[str, tuple[str, ...]] = {
    "factory_bei": ("门点地址", "工厂地址", "装箱地址"),
    "loading_time": ("做箱时间", "做箱日期", "装箱时间", "装箱日期"),
    "vessel_voyage": ("船名航次", "船名/航次"),
    "order_num1": ("提单号", "主提单号", "主单号"),
    "box_text": ("箱型箱量", "箱型/箱量"),
    "c_title": ("托运人/公司名称", "托运人公司名称", "托运人", "公司名称"),
    "c_name": ("托运人联系人", "联系人"),
    "c_phone": ("托运人联系电话", "联系电话"),
    "b_ship_name": ("船名",),
    "b_ship_num": ("航次",),
    "b_ship_company": ("船公司",),
    "factory_name": ("门点简称", "工厂名称"),
    "b_factory_not": ("装箱备注",),
    "b_start_dock": ("启运港", "起运港"),
    "b_end_port": ("中转港代码", "中转港"),
    "b_end_dock": ("目的港",),
    "b_wharf": ("港区",),
    "b_open_ship_time": ("开港时间/开航时间", "开港时间", "开航时间", "ETD"),
    "c_sn": ("内部编号", "业务编号", "我司业务编号"),
    "c_note": ("客户备注", "备注"),
    "packages": ("件数",),
    "gross_weight": ("毛重",),
    "volume": ("体积",),
    "cargo_name": ("货名",),
    "marks": ("唛头",),
}
_ALIAS_TO_FIELD = {
    alias.casefold(): field for field, aliases in _LABELS.items() for alias in aliases
}
_BOX_PATTERNS = (
    re.compile(r"(?P<qty>\d+)\s*[*xX×]\s*(?P<type>\d{2}[A-Za-z]+)"),
    re.compile(r"(?P<type>\d{2}[A-Za-z]+)\s*[*xX×]\s*(?P<qty>\d+)"),
)
# 纯箱型无数量（如 ``20GP``）：位置未被带数量条目覆盖时默认箱量 1；
# (?<!\d) 拒绝数字前缀（如 100GP 中的 00GP），(?!\dA-Za-z) 拒绝字母数字
# 粘连后缀（如 OCR 丢失乘号的 20GP2/20GPx0），避免从粘连文本制造幽灵箱型
_BOX_TYPE_ONLY_RE = re.compile(r"(?<!\d)\d{2}[A-Za-z]+(?![\dA-Za-z])")

# 日期：YYYY-MM-DD（与 document.py 的校验口径一致）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 日期+时间：YYYY-MM-DDTHH:MM:SS
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def _split_loading_value(value: str) -> tuple[str | None, str | None]:
    """拆分做箱时间值：

    - 完整年月日（可带时间，如 `2026-07-20 08:00`）→ (b_date YYYY-MM-DD, 时间部分)
    - 年+月+日 + 中文时间尾巴（如 `2026-08-2 8点到厂` / `2026年8月2日 8点到厂`）
      → (b_date 归一 YYYY-MM-DD, 时间尾巴原样)
    - 缺年份中文日期（如 `7月20日`，可带时间尾巴如 `8点`）→ (b_date 按当前年补全, 时间部分)
    - 纯时间描述（如 `早上8点`/`9:00`/`下午2点`）→ (None, b_date_time_start 原文)
    - 无法识别 → (None, None)

    对齐订单创建接口文档：driver[N][b_date] 为 YYYY-MM-DD，
    driver[N][b_date_time_start] 为装箱时间描述。
    """
    text = value.strip()
    if not text:
        return None, None
    # 完整年月日（可带时间）：时间部分归 b_date_time_start
    normalized = normalize_date_value(text, allow_time=True)
    if isinstance(normalized, str) and _DATETIME_RE.fullmatch(normalized):
        date_part, time_part = normalized.split("T")
        return date_part, time_part
    normalized = normalize_date_value(text, allow_time=False)
    if isinstance(normalized, str) and _DATE_RE.fullmatch(normalized):
        # 格式合法后仍需日历校验（normalize 失败时原样返回可能碰巧命中格式，
        # 如 2026-13-40），非法日期不产出脏数据
        try:
            datetime.strptime(normalized, "%Y-%m-%d")
        except ValueError:
            return None, None
        return normalized, None
    # 年+月+日（月/日可单数字，分隔符 -/./／/年） + 中文时间尾巴（如
    # `2026-08-2 8点到厂`）：日期段归一为 YYYY-MM-DD，剩余时间描述归
    # b_date_time_start；正常 ISO 日期已在上面分支返回，到这里必然带尾巴
    match = re.match(
        r"\s*(\d{4})\s*(?:年\s*|[./-]\s*)(\d{1,2})(?!\d)\s*(?:月\s*|[./-]\s*)(\d{1,2})(?!\d)\s*日?",
        text,
    )
    if match:
        try:
            b_date = (
                date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
            )
        except ValueError:
            return None, None
        rest = text[match.end():].strip()
        return b_date, rest or None
    # 缺年份的中文日期（如 7月20日，可带时间尾巴如 8点/上午8:00）：
    # 日期段按当前年份补全，剩余时间描述归 b_date_time_start
    match = re.match(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?", text)
    if match:
        try:
            b_date = (
                date(date.today().year, int(match.group(1)), int(match.group(2))).isoformat()
            )
        except ValueError:
            return None, None
        rest = text[match.end():].strip()
        return b_date, rest or None
    # 时间描述（含数字且非日期）：早上8点 / 9:00 / 下午2点
    if re.search(r"\d", text):
        return None, text
    return None, None


def _fields(text: str) -> dict[str, str]:
    return {
        _ALIAS_TO_FIELD[label.casefold()]: value
        for label, value in parse_source_fields(text).items()
        if label.casefold() in _ALIAS_TO_FIELD
    }


def parse_source_fields(text: str) -> dict[str, str]:
    """Return labels and values as written, excluding only surrounding delimiters."""
    result: dict[str, str] = {}
    for segment in re.split(r"[；;\n]+", text):
        match = re.fullmatch(r"\s*([^：:]+?)\s*[：:]\s*(.*?)\s*", segment)
        if match and match.group(2):
            result[match.group(1)] = match.group(2)
    return result


# 末尾独立 token 的航次形态（船名航次合写无分隔符时的兜底拆分）：
# - 数字开头 + 字母结尾（2617N / 752E / 043E）
# - 字母开头 + 数字 + 字母结尾（MSC 式，如 QB633W）
_VOYAGE_TOKEN_RE = re.compile(r"^(?:\d{2,}[A-Z]|[A-Z]{1,3}\d{2,}[A-Z])$")


def _split_vessel_voyage(value: str) -> tuple[str | None, str | None]:
    match = re.fullmatch(r"(.+?)\s+(?:V\.?|VOY\.?)\s*([A-Za-z0-9-]+)", value, re.IGNORECASE)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    parts = [part.strip() for part in re.split(r"[/／]", value, maxsplit=1)]
    if len(parts) == 2:
        return parts[0] or None, parts[1] or None
    # 末尾独立 token 为航次形态（无 VOY 前缀/斜杠，如 `MSC CRAPOLLA QB633W`）：
    # 仅拆最后一个 token（船名本身可含多 token，如 `CMA CGM ALEXANDER VON HUMBOLDT`，
    # 其末尾 token 不匹配航次形态则不动），前部归船名
    tokens = value.strip().split()
    if len(tokens) >= 2 and _VOYAGE_TOKEN_RE.match(tokens[-1]):
        return " ".join(tokens[:-1]).strip() or None, tokens[-1]
    return value, None


def _parse_boxes(value: str | None) -> list[BoxItem]:
    """解析箱型箱量文本，支持两种写法混用（如 ``40HQ*3+2*20GP``）。

    两种 pattern（数量*箱型 / 箱型*数量）都会被匹配，按原文出现顺序输出；
    同一箱型多次出现时数量累加。箱量为 0 或负数的条目视为无效并忽略，
    避免把 OCR 噪声或非法数量带进订单（其占位区间同样覆盖兜底，不得复活）。

    仅写箱型未写数量（如 ``20GP``）时默认箱量为 1；位置已被带数量条目
    （含无效数量）覆盖的箱型不重复计入，位置不重叠的裸箱型按 1 计入并累加
    （如 ``1*20GP+20GP`` → 20GP×2）。
    """
    if not value:
        return []
    found: list[tuple[int, int, str, int]] = []
    covered: list[tuple[int, int]] = []
    for pattern in _BOX_PATTERNS:
        for match in pattern.finditer(value):
            # 无论数量是否有效都占位：无效条目（0/负数）不得被兜底以箱量 1 复活
            covered.append((match.start(), match.end()))
            try:
                qty = int(match.group("qty"))
            except ValueError:
                continue
            if qty < 1:
                continue
            found.append((match.start(), match.end(), match.group("type"), qty))
    # 纯箱型兜底：位置未被任何带数量条目覆盖时按箱量 1 计入
    for match in _BOX_TYPE_ONLY_RE.finditer(value):
        if any(start <= match.start() < end for start, end in covered):
            continue
        found.append((match.start(), match.end(), match.group(0), 1))
    quantities: dict[str, int] = {}
    order: list[str] = []
    for _position, _end, b_type, qty in sorted(found, key=lambda item: item[0]):
        if b_type not in quantities:
            order.append(b_type)
            quantities[b_type] = 0
        quantities[b_type] += qty
    return [BoxItem(b_type=b_type, box_num=quantities[b_type]) for b_type in order]


def extract_order_text(text: str) -> tuple[OrderTextExtraction, dict[str, Any]]:
    fields = _fields(text)
    vessel = fields.get("b_ship_name")
    voyage = fields.get("b_ship_num")
    if combined := fields.get("vessel_voyage"):
        vessel, voyage = _split_vessel_voyage(combined)

    cargo_values = {
        "j": fields.get("packages"),
        "m": fields.get("gross_weight"),
        "t": fields.get("volume"),
        "hh": fields.get("cargo_name"),
        "mt": fields.get("marks"),
    }
    loading_time = fields.get("loading_time")
    b_date, b_date_time_start = (
        _split_loading_value(loading_time) if loading_time else (None, None)
    )
    try:
        extracted = OrderTextExtraction.model_validate(
            {
                "order_num1": fields.get("order_num1"),
                "c_title": fields.get("c_title"),
                "c_name": fields.get("c_name"),
                "c_phone": fields.get("c_phone"),
                "b_ship_name": vessel,
                "b_ship_num": voyage,
                "b_ship_company": fields.get("b_ship_company"),
                "factory_name": fields.get("factory_name"),
                "factory_bei": fields.get("factory_bei"),
                "b_factory_not": fields.get("b_factory_not"),
                "b_start_dock": fields.get("b_start_dock"),
                "b_end_port": fields.get("b_end_port"),
                "b_end_dock": fields.get("b_end_dock"),
                "b_wharf": fields.get("b_wharf"),
                "b_open_ship_time": fields.get("b_open_ship_time"),
                "c_sn": fields.get("c_sn"),
                "c_note": fields.get("c_note"),
                "data": [cargo_values] if any(cargo_values.values()) else [],
                "box": _parse_boxes(fields.get("box_text")),
                "driver": (
                    [DriverItem(b_date=b_date, b_date_time_start=b_date_time_start)]
                    if (b_date or b_date_time_start)
                    else []
                ),
            }
        )
    except ValidationError as exc:
        # 解析器内部的 Pydantic 校验失败不泄漏成 500，转成业务错误码
        raise BadRequestError(
            "extracted order text failed schema validation",
            code="order_text_parse_failed",
            details={"errors": exc.errors(include_input=False)},
        ) from exc
    return extracted, {"extractor": "explicit_labels", "value_mode": "verbatim"}
