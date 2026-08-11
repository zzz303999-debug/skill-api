"""附件文档 → 下单接口字段转换（不实际下单）。

流程：上传文件 → 复用 tuoshu 转换链得到 markdown/vision 内容 → LLM 按
`OrderDocumentExtraction` schema 抽取 → 归一化并校验必填字段 → 组装成
下单接口的 order_data 返回，调用方自行决定是否提交。
"""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import settings
from app.document_parsers import mineru
from app.errors import BadRequestError, ConvertError, ParseError
from app.llm import chat_json, image_to_data_url
from app.logging_conf import get_logger
from app.skills.tuoshu.convert_service import (
    SUPPORTED_EXTS,
    convert_image_to_parse_result,
    convert_to_markdown,
    is_image,
    render_pdf_pages,
)
from app.skills.tuoshu.normalizer import (
    STANDARD_CONTAINER_LENGTHS,
    STANDARD_CONTAINER_SUFFIXES,
    normalize_date_value,
)

from .schema import DocumentBoxItem, DocumentCargoItem, OrderDocumentExtraction

log = get_logger(__name__)

# 箱型格式：箱长（20/25/40）+ 两位字母后缀，共 4 位
_CONTAINER_TYPE_RE = re.compile(
    rf"^({'|'.join(STANDARD_CONTAINER_LENGTHS)})([A-Za-z]{{2}})$"
)
_CONTAINER_SUFFIXES = frozenset(STANDARD_CONTAINER_SUFFIXES)

# 提单号：纯数字或字母数字组成，至少 8 位；排除纯字母（船名/人名）
_BILL_NO_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9]{8,}$")

# 日期：YYYY-MM-DD
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 做箱通知中 FM/FROM 行（发件人/客户）的匹配
_FROM_FM_RE = re.compile(r"^(?:FM|FROM)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

# 明确客户栏（行形式与表格形式）的匹配
_CUSTOMER_LABEL_RE = re.compile(
    r"^(?:客户|客户名称|客户简称)\s*[：:]\s*(.+?)\s*$", re.MULTILINE
)
_CUSTOMER_CELL_RE = re.compile(
    r"(?:^|\|)\s*(?:客户|客户名称|客户简称)\s*\|\s*([^|\n]+?)\s*(?=\||$)",
    re.MULTILINE,
)

# 老式 .doc 展平段落前缀（_pN_ / _pN: (empty)_）
_MARKDOWN_STRIP_RE = re.compile(r"^_(?:p|l)\d+(?:: \(empty\))?_\s*", re.IGNORECASE)

# 文档抬头区域取前 N 个非空行
_HEADER_LINES = 6

# “客户简称+单据标题”抬头形式：装箱/做箱通知、海运出口运输委托单/托书等
# （长后缀在前，避免“海运出口运输委托单”被“委托单”抢先匹配）
_DOC_HEADING_SUFFIXES = (
    "海运出口运输委托单|出口运输委托单|运输委托单|"
    "装箱通知书|做箱通知书|装箱通知|做箱通知|"
    "委托单|委托书|托书|托运单"
)
_DOC_HEADING_RE = re.compile(
    rf"^([^|：:\n]{{2,40}}?)(?:{_DOC_HEADING_SUFFIXES})$"
)

# 公司特征词：用于从文档抬头主动识别公司名
_COMPANY_TOKEN_RE = re.compile(
    r"公司|物流|货代|贸易|集团|有限|股份|国际|实业|工贸"
)

# 业务标签词：含这些词的单元格视为字段标签而非公司名
_LABEL_WORDS_RE = re.compile(
    r"做箱|装箱|时间|日期|地址|联系人|电话|备注|编号|客户|船名|航次|"
    r"港区|工厂|件数|毛重|体积|箱型|门点|开船|截单|委托|提单|关单|"
    r"船期|起运|目的|中转|箱单|货物|唛头|发货方|收货方|收货人|托运人|"
    r"承运人|通知方"
)


def _is_label_cell(cell: str) -> bool:
    """单元格是否为字段标签：短文本（≤8 字）且含标签词。

    长文本（如“XX国际货物运输代理有限公司”）即使含“货物”等标签词
    子串也不视为标签，避免误伤公司名。
    """
    return len(cell) <= 8 and bool(_LABEL_WORDS_RE.search(cell))


def _extract_company_name(value: str) -> str:
    """从 FM/FROM 行值中剔除疑似联系人（末段 2-4 个汉字且无公司后缀）。"""
    parts = re.split(r"[\s　]+", value.strip())
    if (
        len(parts) > 1
        and re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", parts[-1])
        and not re.search(r"公司|厂|物流|货代|贸易|集团|有限", parts[-1])
    ):
        return parts[0]
    return value.strip()


def _header_company_lines(source_text: str) -> list[str]:
    """取文档前部非空行的清洗后内容（用于校验 c_title 的抬头依据）。"""
    lines: list[str] = []
    for raw_line in source_text.splitlines():
        line = _MARKDOWN_STRIP_RE.sub("", raw_line.strip()).strip()
        line = line.lstrip("# ").strip().strip("*_` ")
        # 去掉 markdown/OCR 常见前导符号（如 “# +公司名委托单” 的 +/列表符）
        line = line.lstrip("+·•＋").strip()
        if line.startswith("|"):
            line = line.strip("|").strip()
        if line:
            lines.append(line)
        if len(lines) >= _HEADER_LINES:
            break
    return lines


def _looks_like_company(value: str) -> bool:
    """FM/FROM 值是否像公司名（而非联系人姓名）。

    与 _extract_company_name 的剔除口径一致：含公司后缀词视为公司；
    否则按"是否为 2-4 个汉字的单段人名"判断——人名（范颖晰/陈小姐）
    与 2 字公司简称（海丰）无法可靠区分，宁可漏检走人工确认，
    不把联系人姓名写入客户字段。
    """
    if re.search(r"公司|厂|物流|货代|贸易|集团|有限|股份|国际|运输|实业|工贸", value):
        return True
    return not re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", value)


def _is_header_company(value: str, header_lines: list[str]) -> bool:
    """FM 公司是否为文档抬头公司（通知发出方/货代）。

    门点装箱通知类单据的 FM 行常是印抬头的货代公司（如
    "FM: 上海威世国际货物运输代理有限公司"），此时 FM 是通知发出方
    而非客户，不得作为 c_title。判定规则：
    - 与某个抬头行整行相等 → 抬头公司；
    - 标签行（FM/FROM/TO/致/ATTN）本身不算抬头，但其值若与 FM 值相同
      （如顶部区域 "FM: xxx" 行值即 xxx），说明 FM 就是文档通知发出方
      ——按保守原则视为货代（顶部区域真实客户 FROM 行也会被拒绝，
      走人工确认比误放行货代更安全）；
    - 子串出现仅限"类公司抬头"行：排除标签行、表格行（含 |）与短行，
      且值至少 3 个字符，避免 2 字简称与表格单元格误伤。
    """
    for header in header_lines:
        if header == value:
            return True
        m = re.match(
            r"^(?:FM|FROM|TO|致|ATTN)\s*[:：]\s*(.+)$", header, re.IGNORECASE
        )
        if m:
            if m.group(1).strip() == value:
                return True
            continue
        if len(value) >= 3 and len(header) >= 8 and "|" not in header and value in header:
            return True
    return False


# 标签起点：单行混排时捕获值在此截断（避免把后续标签/港区名吞进值里）
_PORT_LABEL_START_RE = re.compile(
    r"\s*(?:中转港代码|转运港代码|中转港|转运港|目的港|卸货港|港区|启运港|起运港|装货港|"
    r"船期|开航|开港|船名|船次|航次|提单号|做箱|装箱|件数|毛重|体积|箱型|箱量|备注|"
    r"PORT\s+OF|TRANSSHIPMENT|DISCHARGE|FINAL)",
    re.IGNORECASE,
)
# 待查/占位描述：b_end_dock（目的港）不允许此类值，命中则置 None 走人工确认
_PLACEHOLDER_PORT_VALUE_RE = re.compile(
    r"^(?:见设|见设备单|见设备交接单|待定|待查|同中转港|同左|同上|无|未知|详见.*|以.*为准|看.*)$"
)


def _clean_port_value(value: str) -> str:
    """截断到下一个标签起点并去空白，避免单行混排污染捕获值。"""
    cleaned = value.strip()
    m = _PORT_LABEL_START_RE.search(cleaned)
    return cleaned[: m.start()].strip() if m else cleaned


# 中转港标签（映射 b_end_port）；长标签在前避免“中转港代码”被“中转港”
# 抢先匹配，捕获值不跨行（字符类不含换行），跨标签由 _clean_port_value 截断
_TRANSIT_PORT_LABEL_RE = re.compile(
    r"(?:PORT\s+OF\s+TRANSSHIPMENT|TRANSSHIPMENT\s*PORT|中转港代码|转运港代码|中转港|转运港)\s*[:：]?\s*"
    r"([A-Za-z\u4e00-\u9fa5][A-Za-z0-9\u4e00-\u9fa5 ()/'.-]{0,40})",
    re.IGNORECASE,
)
# 目的港标签（映射 b_end_dock）
_DEST_PORT_LABEL_RE = re.compile(
    r"(?:FINAL\s+PORT\s+OF\s+DISCHARGE|PORT\s+OF\s+DISCHARGE|DISCHARGING\s+PORT|目的港|卸货港)\s*[:：]?\s*"
    r"([A-Za-z\u4e00-\u9fa5][A-Za-z0-9\u4e00-\u9fa5 ()/'.-]{0,40})",
    re.IGNORECASE,
)


def _extract_port_values(pattern: re.Pattern[str], source_text: str) -> list[str]:
    """提取标签值并清洗：截断跨标签污染、去空白；空值剔除。"""
    values: list[str] = []
    for m in pattern.finditer(source_text):
        value = _clean_port_value(m.group(1))
        if value:
            values.append(value)
    return values


def _revise_port_fields(
    end_port: str | None, end_dock: str | None, source_text: str | None
) -> tuple[str | None, str | None]:
    """兜底修复：b_end_port（中转港）与 b_end_dock（目的港）互不混淆。

    字段语义对齐订单创建接口文档与自由文本抽取（extractor.py）：
    b_end_port=中转港、b_end_dock=目的港。LLM 输出若把目的港值填进
    b_end_port 或把中转港值填进 b_end_dock，则按原文标签修正；
    空缺字段按原文标签补全——b_end_port 允许待查描述（如“见设”）
    逐字保留，b_end_dock 遇待查/占位描述（见设/待定/同中转港等）
    置 None 走人工确认，不放行占位值冒充目的港。
    """
    if not source_text:
        return end_port, end_dock

    transit_values = _extract_port_values(_TRANSIT_PORT_LABEL_RE, source_text)
    dest_values = _extract_port_values(_DEST_PORT_LABEL_RE, source_text)

    def hit(values: list[str], value: str | None) -> bool:
        if not value:
            return False
        v = value.strip().upper()
        return any(v in t.upper() or t.upper() in v for t in values if t)

    end_port_v, end_dock_v = end_port, end_dock
    # b_end_port 中是被“目的港”标签标注的值（LLM 混淆）→ 挪到 b_end_dock
    if hit(dest_values, end_port_v) and not hit(transit_values, end_port_v):
        if not end_dock_v:
            end_dock_v = end_port_v
        end_port_v = None
    # b_end_dock 中是被“中转港”标签标注的值 → 挪到 b_end_port
    if hit(transit_values, end_dock_v) and not hit(dest_values, end_dock_v):
        if not end_port_v:
            end_port_v = end_dock_v
        end_dock_v = None
    # 空缺字段按原文标签补全；目的港补全时过滤待查/占位描述
    if not end_dock_v and dest_values:
        dest_candidate = dest_values[0]
        end_dock_v = (
            None if _PLACEHOLDER_PORT_VALUE_RE.match(dest_candidate) else dest_candidate
        )
    if not end_port_v and transit_values:
        end_port_v = transit_values[0]
    return end_port_v, end_dock_v


# 做箱/装箱时间标签（b_date_time_start 唯一合法来源）
_LOADING_TIME_LABEL_RE = re.compile(
    r"(?:做箱|装箱|进箱)\s*时间[:：]?\s*([^。\n]{0,30})"
)
# 截单/截关等时间标签（禁止作为装箱时间）
_CUTOFF_TIME_LABEL_RE = re.compile(
    r"(?:截单|截关|截信息|截SI|截VGM)\s*时间?[:：]?\s*([^。\n]{0,30})",
    re.IGNORECASE,
)


def _revise_loading_time(value: str | None, source_text: str | None) -> str | None:
    """兜底修复：b_date_time_start 只认“做箱/装箱时间”标签后的值。

    LLM 把“截单时间”等标签的时间描述当作装箱时间时（如
    “截单时间：1-7早上9点”被输出为“早上9点”），置 None——
    截单时间就是截单时间，不是装箱时间。
    """
    if not value or not source_text:
        return value
    wanted = value.strip()
    for m in _LOADING_TIME_LABEL_RE.finditer(source_text):
        if wanted in m.group(1):
            return value
    for m in _CUTOFF_TIME_LABEL_RE.finditer(source_text):
        if wanted in m.group(1):
            return None
    return value


def _revise_c_title_to_value(raw_value: str | None, source_text: str | None) -> str | None:
    """兜底修复：c_title 只认原文真实存在的客户来源。

    客户只可能来自 FM/FROM 后的公司名（但排除文档抬头货代/通知发出方）、
    明确客户栏（客户/客户名称/客户简称）或文档抬头公司（顶部区域公司名行
    或“客户简称+装箱/做箱通知”标题）；原文均无有效来源时返回 None 走人工
    确认，不放行 LLM 臆造值（例如从文件名前缀推断出的公司名）。

    注意：展平段落流（_pN_/_lN_ 前缀）中的 FM 行不在此匹配——FM 值既可能
    是公司简称（海丰）也可能是联系人姓名（范颖晰），无法可靠区分，
    保持漏检走人工确认比误当客户更安全。
    """
    if not raw_value or not source_text:
        return raw_value
    wanted = raw_value.strip()

    # 1. FM/FROM 后的公司名优先：扫描全部 FM/FROM 行，取第一个
    #    "像公司名"且非文档抬头货代（通知发出方）的值；单段人名
    #    （范颖晰/陈小姐）不算公司，抬头货代不作为客户来源，均继续
    #    扫描后续行而不是直接返回
    header_lines = _header_company_lines(source_text)
    for line in source_text.splitlines():
        match = _FROM_FM_RE.match(line)
        if match:
            company = _extract_company_name(match.group(1)) or None
            if (
                company
                and _looks_like_company(company)
                and not _is_header_company(company, header_lines)
            ):
                return company

    # 2. 明确客户栏（行形式或表格形式）
    for raw_line in source_text.splitlines():
        line = _MARKDOWN_STRIP_RE.sub("", raw_line.strip()).strip()
        match = _CUSTOMER_LABEL_RE.match(line)
        if match and match.group(1).strip() == wanted:
            return wanted
    for match in _CUSTOMER_CELL_RE.finditer(source_text):
        value = match.group(1).strip().strip("*_` ")
        if value == wanted:
            return wanted

    # 3. 文档抬头区域：整行相等；或表格单元格相等（非标签行且值像公司名）；
    #    或“客户简称+装箱/做箱通知”标题前缀
    for line in header_lines:
        if line == wanted:
            return wanted
        heading = _DOC_HEADING_RE.fullmatch(line)
        if heading and heading.group(1).strip() == wanted:
            return wanted
        cells = [cell.strip() for cell in line.split("|") if cell.strip()]
        if not cells or any(_is_label_cell(cell) for cell in cells):
            continue
        if wanted in cells and _looks_like_company(wanted):
            return wanted

    # 4. 原文无任何客户来源，置空走人工确认
    return None


def _customer_cell_value(line: str) -> str | None:
    """从单行文本取明确客户栏的值（行形式“客户：xxx”或表格 | 客户 | xxx |）。"""
    match = _CUSTOMER_LABEL_RE.match(line)
    if match:
        return match.group(1).strip()
    match = _CUSTOMER_CELL_RE.search(line)
    if match:
        return match.group(1).strip().strip("*_` ") or None
    return None


def _extract_header_company(source_text: str | None) -> str | None:
    """LLM 未提取到客户时，从文档抬头区域主动补全公司名（文档抬头公司即客户）。

    支持普通行与表格行（按 | 拆单元格判断）：优先明确客户栏（客户/客户简称），
    其次只认含公司特征词的公司名，或“客户简称+装箱/做箱通知”标题前缀
    （前缀 ≥5 字或含公司特征词）；数字行号、含冒号单元格、字段标签行
    （装箱工厂/发货方/收货方等）及其值、纯单据标题一律不算。
    """
    if not source_text:
        return None
    for line in _header_company_lines(source_text):
        cells = [cell.strip() for cell in line.split("|") if cell.strip()]
        if customer_value := _customer_cell_value(line):
            return customer_value
        # 表格行：含字段标签的行，其值单元格不作为客户来源
        if len(cells) > 1 and any(_is_label_cell(cell) for cell in cells):
            continue
        for cell in cells:
            if cell.isdigit() or "：" in cell or ":" in cell:
                continue
            # “客户简称+装箱/做箱通知/运输委托单”标题优先（否则会被标签词拦截）
            heading = _DOC_HEADING_RE.fullmatch(cell)
            if heading:
                company = heading.group(1).strip()
                if len(company) >= 5 or _COMPANY_TOKEN_RE.search(company):
                    return company
                continue
            if _is_label_cell(cell):
                continue
            if _COMPANY_TOKEN_RE.search(cell):
                return cell
    return None


def _clean_json_schema(schema: dict) -> dict:
    """去除 Pydantic 生成的 $defs / anyOf，转成模型友好的简化 schema。"""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def _resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref and ref.startswith("#/$defs/"):
                return _resolve(defs.get(ref[len("#/$defs/"):], {}))
            result: dict[str, Any] = {}
            for key, value in node.items():
                if key in ("title", "default", "$schema"):
                    continue
                if key == "anyOf":
                    options = [_resolve(opt) for opt in value]
                    non_null = [opt for opt in options if opt.get("type") != "null"]
                    has_null = len(non_null) != len(options)
                    if has_null and len(non_null) == 1:
                        nullable_type = non_null[0].get("type")
                        if isinstance(nullable_type, str):
                            non_null[0]["type"] = [nullable_type, "null"]
                        elif isinstance(nullable_type, list):
                            non_null[0]["type"] = [*nullable_type, "null"]
                        result.update(non_null[0])
                    else:
                        result["anyOf"] = options
                elif key == "properties":
                    result[key] = {pk: _resolve(pv) for pk, pv in value.items()}
                elif key == "items":
                    result[key] = _resolve(value)
                else:
                    result[key] = value
            return result
        return node

    return _resolve(schema)


_SYSTEM_PROMPT = """你是海运托书/做箱通知结构化抽取助手。读取 Markdown/图片正文，只输出符合 JSON Schema 的英文 key 对象；不输出解释或另一份摘要。拿不准填 null。

# 字段来源表（唯一目标）
| 原文标签/版面角色 | 字段 | 缺失处理 |
|---|---|---|
| `关单号`/`提单号`/`主提单号`/`主单号`/`B/L NO`/`MBL NO`（关单号即提单号；与`运编号`并存时取关单号） | `order_num1` | null |
| 文档抬头公司（顶部公司名称）或 `FM`/`FROM` 后的公司名称（发件人/托运人，仅取公司名部分，剔除同行联系人姓名与电话） | `c_title` | null |
| 做箱/装箱地址（详细街道地址，含省市区县、道路、门牌号，**并附原文现场联系人/电话**） | `factory_bei` | null |
| `做箱日期`/`装箱日期`（日期格式） | `b_date` | null |
| `做箱时间`/`装箱时间`（时间描述，如 `早上8点`/`9:00`） | `b_date_time_start` | null |
| `开船时间`/`开航时间`/`ETD` | `b_open_ship_time` | null |
| 件数（数字 + `CTNS`，单位可省略） | `packages` | null |
| 毛重（数字 + `KGS`，单位可省略，保留 2-3 位小数） | `gross_weight` | null |
| 体积（数字 + `CBM`，单位可省略，保留 2-3 位小数） | `volume` | null |
| 箱型箱量（如 `3*40HQ`、`40HQ*2`、`1x20GP+1x40HQ`） | `box` | null |
| `船名`/`VESSEL`；`船名航次`合写时拆开 | `b_ship_name` | null |
| `航次`/`船次`/`VOY`/`VOYAGE` | `b_ship_num` | null |
| `船公司`/`CARRIER` | `b_ship_company` | null |
| `目的港`/`卸货港`/`PORT OF DISCHARGE`（**最终卸货港**，不含中转港） | `b_end_dock` | null |
| `中转港`/`转运港`/`中转港代码`/`TRANSSHIPMENT PORT`（待查描述如 `见设备单` 逐字保留） | `b_end_port` | null |
| `港区`/做箱港区 | `b_wharf` | null |
| `启运港`/`装货港`/`PORT OF LOADING` | `b_start_dock` | null |
| 门点简称/`工厂名称` | `factory_name` | null |
| `装箱备注` | `b_factory_not` | null |
| `联系人`/`现场联系人`/`装箱联系人` | `c_name` | null |
| `电话`/`手机`/`TEL`（随联系人出现） | `c_phone` | null |
| `内部编号`/`运编号`/`业务编号`/`我司业务编号`（内部编号，**禁止**填入 order_num1） | `c_sn` | null |
| `备注`/`注意事项`/`REMARK`/`NOTE` | `c_note` | null |
| 货物明细行（表格中每个数据行：提单号+件数+毛重+体积，一票多客户/多提单号时每行一条） | `data[]` | null |
| 货物明细行中的 `货名`/`货物名称` | `data[].hh` | null |
| 货物明细行中的 `唛头`/`MARKS`/`N/M` | `data[].mt` | null |

# 抽取规则
1. 编号逐字复制，严禁改大小写、形近字或 O/0、I/1；图片中的红章、水印、logo、品牌图及其 OCR 一律忽略。
2. `order_num1` 必须至少 8 位且仅由数字或英文字母数字组成（纯数字也允许）；不得保留空格、连字符或其他符号；纯字母串（船名/人名）不算提单号，不符合时填 null。**关单号即提单号**；`报关单号`/`报关号` 不是提单号，禁止作为 `order_num1`；`运编号`/`业务编号`/`我司业务编号` 是内部编号（归 `c_sn`），禁止作为 `order_num1`；关单号与运编号并存时取关单号。
3. `box[].b_type` 必须是 4 位：箱长 `20`/`25`/`40` 加两位字母后缀（`GP/HC/HQ/RF/OT/TK/FR/PL/OH/RH/UT/VH` 等），与原文一致，禁止在 HQ/HC/DV/GP 等代码间改写；`box[].box_num` 为箱量（`3*40HQ` → box_num=3）。多个箱型分多条输出；**多数据行同为相同箱型时 box_num 必须累加**（如 3 个数据行各 `1*40HC` → `[{"b_type": "40HC", "box_num": 3}]`），禁止只取第一行的箱量。
4. `c_title` 取文档抬头公司（顶部公司名称）或 `FM：`/`FROM：` 后的公司名称（一般为公司名称或简称）；同行含联系人姓名/电话时只取公司名部分；都无 → 填 null（人工确认）。`TO:`/`ATTN:`/`致:` 后的值是收件/通知对象，**禁止**作为 c_title；文件名不是原文，**禁止**从文件名前缀推断 c_title。
5. `factory_bei` 取门点详细街道地址（保留省市区县、道路、门牌号和园区/楼栋信息），并**附上原文的现场联系人姓名与电话**——地址同行或独立的联系人/电话行都要并入（格式如 `金泰路转诚泰路17号 朱劲松 13776121224`，对齐订单创建接口文档：门点地址含现场联系人、电话）；不要把公司名并入地址（公司名在 `factory_name`）。
6. `b_date` 输出 `YYYY-MM-DD`；原文缺年时按文档日期、文件名年份推断，无法推断填 null。`b_date_time_start` 只取 `做箱时间`/`装箱时间` 标签后的时间描述（如 `早上8点`/`9:00`/`下午2点`），值为日期格式时归 `b_date` 而非 `b_date_time_start`；**`截单时间`/`截关时间` 等不是装箱时间，禁止填入 `b_date_time_start`**。
7. `packages`/`gross_weight`/`volume` 只清洗单位和千分位：件数为整数；毛重、体积按原文精度保留 2-3 位小数，超过 3 位四舍五入到 3 位，不补无意义的尾零；单位（CTNS/KGS/CBM）可省略。任一值 ≤0 填 null。
8. 老式 `.doc` 等文档转换后可能被展平为 `_pN_` 段落流（`_pN: (empty)_` 是空单元格）：标签与值分属不同段落，把标签后第一个非空、非标签的段落当作该标签的值；`提单号` 的 8+ 位纯数字或字母数字值可按格式特征在全文中定位。
9. `b_ship_name`/`b_ship_num`/`b_ship_company`/`b_start_dock`/`b_end_port`/`b_end_dock`/`b_wharf`/`b_open_ship_time`/`b_date_time_start`/`factory_name`/`b_factory_not`/`c_name`/`c_phone`/`c_sn`/`c_note` 等可选字段只在原文明确出现时逐字抽取；原文未给出时填 null，禁止填 `未知`/`待定`/`看设备单上`/`还未知`/`无` 等占位表述（`b_end_port` 中转港标签后明确写出的待查描述除外，见规则 13）。
10. `b_open_ship_time`/`b_date`/`b_date_time_start` 是三个不同字段：前者是开船时间，`b_date` 是做箱/装箱**日期**（`YYYY-MM-DD`），`b_date_time_start` 是做箱/装箱**时间描述**（如 `早上8点`/`9:00`），按标签与值格式严格区分，禁止混填。
11. `data` 为货物明细列表，**必须列出文档中每一个数据行**（表格数据行/按客户编号或提单号分组的行），禁止只取第一行或把多行合并成一行：每条含该行提单号 `b_order_num`（无提单号的行填 null，禁止填整票提单号）、件数 `j`、毛重 `m`、体积 `t`；某行三项（件数/毛重/体积）不全时跳过该行。`data[].hh`（货名）与 `data[].mt`（唛头）可选，原文有则逐字保留，无则 null。`packages`/`gross_weight`/`volume` 单值字段填第一条数据行的值（与 `data[0]` 一致）；文档只有一行数据时 `data` 同样输出一条。文档整体无货物明细行（件数/毛重/体积均未出现）时，`data` 输出一条仅含整票提单号的行（`b_order_num` 填 `order_num1`，件数/毛重/体积/货名/唛头填 null）。
12. `b_end_dock` 只取**最终卸货港**（`目的港`/`卸货港`/`PORT OF DISCHARGE` 标签后的值）。带 `中转港`/`转运港`/`中转港代码`/`TRANSSHIPMENT PORT` 等标签或其旁注含"中转/转运/transship"字样的港口**禁止**填入 `b_end_dock`（如"中转港：INCHON"时 INCHON 不是目的港，应填入 `b_end_port`）；原文未明确给出最终目的港（只有中转港或中转描述）时 `b_end_dock` 填 null（人工确认），禁止用中转港冒充目的港。
13. `b_end_port` 只取**中转港**（`中转港`/`转运港`/`中转港代码`/`TRANSSHIPMENT PORT` 标签后的值），与 `b_end_dock`（目的港）严格区分；**中转港标签后明确写出**的待查描述（如 `见设备单`/`见设`/`待定`）逐字保留，其他位置出现的占位词仍按规则 9 填 null，只有原文真正缺失时才是 null。
"""


def _build_system_prompt() -> str:
    return _SYSTEM_PROMPT


def _build_user_message_text(markdown: str, filename: str, doc_format: str) -> str:
    return (
        f"待抽取单据。\n"
        f"file={filename}\n"
        f"doc_format={doc_format}\n\n"
        f"===== 文档内容开始 =====\n"
        f"{markdown}\n"
        f"===== 文档内容结束 =====\n\n"
        f"请输出 JSON。"
    )


def _build_user_message_vision(
    image_data_urls: str | list[str],
    filename: str,
    doc_format: str,
    parsed_text: str | None = None,
) -> list[dict]:
    urls = [image_data_urls] if isinstance(image_data_urls, str) else image_data_urls
    parsed_section = ""
    if parsed_text:
        parsed_section = (
            "\n\n以下是 MinerU OCR 辅助文本，不是独立事实来源。所有自由文本字段必须在图片中"
            "肉眼可见；OCR 中存在但图片上看不到的词句必须剔除并填 null。"
            "图片与文本冲突时以图片为准：\n"
            "===== 已解析文本开始 =====\n"
            f"{parsed_text}\n"
            "===== 已解析文本结束 ====="
        )
    return [
        {
            "type": "text",
            "text": (
                f"待抽取单据（图片/扫描件）。\n"
                f"file={filename}\n"
                f"doc_format={doc_format}\n\n"
                f"请直接从图片正文中提取，忽略红色图章、水印、logo 和其他图片区域中的文字，"
                f"输出 JSON。{parsed_section}"
            ),
        },
        *[
            {
                "type": "image_url",
                "image_url": {"url": url, "detail": "high"},
            }
            for url in urls
        ],
    ]


def _convert_file(
    file_bytes: bytes, filename: str
) -> tuple[str | None, str, dict[str, Any], list[dict] | str]:
    """把附件转成 LLM 可用的输入。

    返回 (source_text, doc_format, conversion_meta, user_content)。
    user_content 为字符串（纯文本）或 list[dict]（vision 消息）。
    """
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise BadRequestError(
            f"unsupported extension: {ext}",
            details={"supported": SUPPORTED_EXTS},
        )

    doc_format = ext.lstrip(".")
    source_text: str | None = None
    conversion_meta: dict[str, Any] = {}

    if is_image(ext):
        parse_result = convert_image_to_parse_result(file_bytes, filename)
        doc_format = parse_result.input_format
        conversion_meta = parse_result.meta()
        source_text = parse_result.markdown or None
        # 当前 LLM 无视觉能力（llm_vision_enabled=False）时一律跳过 vision
        skip_vision = (
            not settings.llm_vision_enabled
            or (
                settings.image_vision_skip_when_confident
                and parse_result.parser == "mineru"
                and not parse_result.parser_fallback
            )
        )
        if parse_result.vision_images and not skip_vision:
            images = list(parse_result.vision_inputs)
            total_image_bytes = sum(len(image) for image, _ in images)
            if total_image_bytes > settings.vision_max_image_bytes:
                if not source_text:
                    # 无 OCR 文本可降级时，绝不能把空文档喂给 LLM——模型会输出
                    # 整份捏造数据。直接拒绝并提示压缩/拆分图片。
                    raise ConvertError(
                        "image exceeds the vision upload limit and no OCR text is "
                        "available; compress or split the image and retry",
                        code="vision_image_too_large",
                        details={
                            "file": Path(filename).name,
                            "bytes": total_image_bytes,
                            "max_bytes": settings.vision_max_image_bytes,
                        },
                    )
                conversion_meta["vision_skipped_reason"] = "image_too_large"
                user_content = _build_user_message_text(
                    source_text or "", filename, doc_format
                )
            else:
                data_urls = [
                    image_to_data_url(image, mime=mime)
                    for image, mime in images
                ]
                user_content = _build_user_message_vision(
                    data_urls,
                    filename,
                    doc_format,
                    parsed_text=source_text,
                )
        else:
            if skip_vision:
                conversion_meta["vision_cross_check"] = "skipped_confident"
            if not source_text:
                # 无 OCR 文本可降级且模型无视觉时，绝不能把空文档喂给 LLM——
                # 模型会输出整份捏造数据。直接拒绝并提示检查 MinerU 服务。
                raise ConvertError(
                    "image has no OCR text and the current LLM model has no "
                    "vision capability; check the MinerU service or use a "
                    "vision-capable model",
                    code="vision_disabled_no_ocr",
                    details={"file": Path(filename).name},
                )
            user_content = _build_user_message_text(
                source_text, filename, doc_format
            )
        return source_text, doc_format, conversion_meta, user_content

    markdown = convert_to_markdown(file_bytes, filename)
    parse_result = getattr(markdown, "parse_result", None)
    if parse_result is not None:
        conversion_meta = parse_result.meta()
    else:
        parser = getattr(markdown, "parser", None)
        if parser:
            conversion_meta = {"parser": parser}

    if markdown.startswith("SCAN_OR_IMAGE_HINT:"):
        # 扫描件 PDF：无视觉模型时交 MinerU OCR，否则整本转图片走 vision
        if ext != ".pdf":
            raise ConvertError(
                "document has no extractable text; convert it to PDF/image",
                details={
                    "file": Path(filename).name,
                    "convert_hint": markdown.partition("#")[2].strip(),
                },
            )
        if not settings.llm_vision_enabled:
            # 扫描 PDF：模型无视觉，改交 MinerU OCR 解析而不是直接拒绝
            try:
                scanned = mineru.parse_document(
                    file_bytes, filename, mime_type="application/pdf"
                )
            except Exception as exc:
                raise ConvertError(
                    "scan PDF has no extractable text and MinerU OCR failed; "
                    "check the MinerU service or use a text-based PDF",
                    code="vision_disabled_no_ocr",
                    details={
                        "file": Path(filename).name,
                        "mineru_error": f"{exc.__class__.__name__}: {exc}",
                    },
                ) from exc
            if not scanned.markdown.strip():
                raise ConvertError(
                    "scan PDF has no extractable text and MinerU OCR returned "
                    "empty; check the MinerU service or use a text-based PDF",
                    code="vision_disabled_no_ocr",
                    details={"file": Path(filename).name},
                )
            # MinerU OCR 成功：走纯文本抽取；扫描件无独立文本层可交叉核验
            conversion_meta = {
                "parser": "mineru",
                "parser_fallback": True,
                "input_format": "pdf",
                "ocr_unverified": True,
            }
            return (
                scanned.markdown,
                doc_format,
                conversion_meta,
                _build_user_message_text(scanned.markdown, filename, doc_format),
            )
        page_images = render_pdf_pages(
            file_bytes,
            max_pages=settings.vision_max_pdf_pages,
            scale=settings.vision_pdf_render_scale,
        )
        total_image_bytes = sum(len(image) for image in page_images)
        if total_image_bytes > settings.vision_max_image_bytes:
            # 渲染出的 PNG 总字节同样受 vision 直传上限约束，超限时报错提示拆分
            raise ConvertError(
                "rendered scan pages exceed the vision upload limit; "
                "split the PDF into smaller parts and retry",
                code="vision_image_too_large",
                details={
                    "file": Path(filename).name,
                    "bytes": total_image_bytes,
                    "max_bytes": settings.vision_max_image_bytes,
                },
            )
        data_urls = [image_to_data_url(image, mime="image/png") for image in page_images]
        conversion_meta = {
            "parser": "vision",
            "parser_fallback": True,
            "input_format": "pdf",
        }
        return None, doc_format, conversion_meta, _build_user_message_vision(
            data_urls, filename, doc_format
        )

    source_text = str(markdown)
    if (
        parse_result is not None
        and parse_result.vision_images
        and settings.llm_vision_enabled
    ):
        images = list(parse_result.vision_inputs)
        total_image_bytes = sum(len(image) for image, _ in images)
        if total_image_bytes > settings.vision_max_image_bytes:
            conversion_meta["vision_skipped_reason"] = "image_too_large"
            user_content = _build_user_message_text(source_text, filename, doc_format)
        else:
            data_urls = [
                image_to_data_url(image, mime=mime) for image, mime in images
            ]
            user_content = _build_user_message_vision(
                data_urls,
                filename,
                doc_format,
                parsed_text=source_text,
            )
    else:
        user_content = _build_user_message_text(source_text, filename, doc_format)
    return source_text, doc_format, conversion_meta, user_content


# ---- 归一化 ----

_MEASUREMENT_LEAD = re.compile(
    r"^(?:约|大约|大概|約|approx(?:oximately)?\.?|about)\s*", re.IGNORECASE
)


def _extract_number(value: str) -> float | None:
    """从字符串中提取第一个数字（剥离前缀修饰词），失败返回 None。"""
    text = _MEASUREMENT_LEAD.sub("", value.replace(",", "").strip())
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def _format_decimal(number: float, *, max_decimals: int = 3) -> str:
    """保留 2-3 位小数：四舍五入到 3 位，去掉无意义尾零但保留至少 2 位。"""
    if number <= 0:
        raise ValueError("value must be positive")
    rounded = round(number, max_decimals)
    if rounded == int(rounded):
        return str(int(rounded))
    text = f"{rounded:.{max_decimals}f}".rstrip("0")
    return text


def _normalize_bill_no(value: Any) -> str | None:
    """提单号：去空格/连字符后必须为 8+ 位纯数字或字母数字。"""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[\s\-/]", "", value.strip())
    if not _BILL_NO_RE.fullmatch(cleaned):
        return None
    return cleaned


# 关单号标签（order_num1 提单号来源：关单号即提单号；报关单号不是提单号）。
# 值模式收紧为纯字母数字（6-20 位），避免吞并同行相邻标签文本（如
# “关单号：CNWW036474 B/L：...”）；前边界要求行首/空白/标点，
# 配合 _extract_customs_no 中的报关前缀屏蔽，排除“报关单号”误匹配
_CUSTOMS_NO_LABEL_RE = re.compile(
    r"(?:^|(?<=[\s，。；;：:、]))关单号\s*[:：]?\s*([A-Za-z0-9]{6,20})",
    re.IGNORECASE,
)

# 报关单号/报关号（含 OCR 空格变体）：提取前整体屏蔽，防止子串误匹配
_CUSTOMS_DECLARATION_SHIELD_RE = re.compile(
    r"报\s*关\s*单\s*号|报\s*关\s*号",
    re.IGNORECASE,
)

# 提单号/主提单号/主单号标签：提单号优先（原文明确标注时以其为准，关单号次之）
_MASTER_BILL_LABEL_RE = re.compile(
    r"(?:^|(?<=[\s，。；;：:、]))(?:提单号|主提单号|主单号)\s*[:：]?\s*"
    r"([A-Za-z0-9]{6,40})",
    re.IGNORECASE,
)


def _extract_customs_no(source_text: str) -> str | None:
    """从原文提取关单号，清洗分隔符后按提单号格式校验。

    “报关单号/报关号”（含 OCR 空格变体）先整体替换为含“报”字的形式，
    使“关单号”标签匹配时其前边界不再成立，从而排除误提取。
    """
    shielded = _CUSTOMS_DECLARATION_SHIELD_RE.sub("报关单号", source_text)
    m = _CUSTOMS_NO_LABEL_RE.search(shielded)
    if not m:
        return None
    return _normalize_bill_no(m.group(1))


def _revise_bill_no(order_num1: str | None, source_text: str | None) -> str | None:
    """兜底修复：提单号优先，关单号即提单号（LLM 可能把运编号误作 order_num1）。

    - 原文明确标注 `提单号/主提单号/主单号` 标签时，以其值为准（提单号优先）；
    - 否则原文存在合法关单号时以关单号覆盖/补全——如文档同时含
      “运编号：MAX...”、“关单号：CNWW...”时取关单号。
    """
    if not source_text:
        return order_num1
    bill_match = _MASTER_BILL_LABEL_RE.search(source_text)
    if bill_match:
        bill_value = _normalize_bill_no(bill_match.group(1))
        if bill_value:
            return bill_value
    customs_no = _extract_customs_no(source_text)
    if not customs_no:
        return order_num1
    if order_num1 is None or order_num1 != customs_no:
        return customs_no
    return order_num1


def _normalize_container_type(value: Any) -> str | None:
    """箱型：必须为 4 位（箱长 20/25/40 + 两位字母后缀）。"""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"['’\s\"`]", "", value.strip()).upper()
    match = _CONTAINER_TYPE_RE.fullmatch(cleaned)
    if not match:
        return None
    suffix = match.group(2)
    if suffix not in _CONTAINER_SUFFIXES:
        return None
    return cleaned


def _normalize_data_items(value: Any) -> list[DocumentCargoItem]:
    """货物明细行：每行归一化件数/毛重/体积，三项齐全才保留。

    行内 b_order_num 为该行提单号（可空，不强制每行都有）；
    件数/毛重/体积任一项缺失或非法时整行跳过（不产生脏数据），
    行内提单号缺失由缺失校验/回退环节把关。
    """
    if not isinstance(value, list):
        return []

    items: list[DocumentCargoItem] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_bill_no = item.get("b_order_num")
        raw_j = item.get("j")
        raw_m = item.get("m")
        raw_t = item.get("t")
        # LLM 可能输出 JSON number（如 680 而非 "680"），统一转字符串再归一化
        packages = _normalize_packages(str(raw_j) if raw_j is not None else None)
        gross_weight = _normalize_weight(str(raw_m) if raw_m is not None else None)
        volume = _normalize_volume(str(raw_t) if raw_t is not None else None)
        if not (packages and gross_weight and volume):
            continue
        items.append(
            DocumentCargoItem(
                b_order_num=_normalize_bill_no(
                    str(raw_bill_no) if raw_bill_no is not None else None
                ),
                j=packages,
                m=gross_weight,
                t=volume,
                hh=_normalize_text(item.get("hh")),
                mt=_normalize_text(item.get("mt")),
            )
        )
    return items


def _normalize_boxes(value: Any) -> list[DocumentBoxItem]:
    """箱型箱量归一：同箱型合并累加（多数据行各 1*40HC → 一条 box_num=3），
    保持首次出现顺序；非法箱型/箱量 <1 的行跳过。"""
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    merged: dict[str, int] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        b_type = _normalize_container_type(item.get("b_type"))
        box_num_raw = item.get("box_num")
        try:
            box_num = int(box_num_raw)
        except (TypeError, ValueError):
            box_num = 0
        if not b_type or box_num < 1:
            continue
        merged[b_type] = merged.get(b_type, 0) + box_num
    return [
        DocumentBoxItem(b_type=b_type, box_num=box_num)
        for b_type, box_num in merged.items()
    ]


def _normalize_packages(value: Any) -> str | None:
    """件数：整数数字字符串，单位 CTNS 可省略。"""
    if not isinstance(value, str):
        return None
    number = _extract_number(value)
    if number is None or number <= 0:
        return None
    if not number.is_integer():
        return None
    return str(int(number))


def _normalize_weight(value: Any) -> str | None:
    """毛重：数字字符串，保留 2-3 位小数，单位 KGS 可省略。"""
    if not isinstance(value, str):
        return None
    number = _extract_number(value)
    if number is None:
        return None
    try:
        return _format_decimal(number)
    except ValueError:
        return None


def _normalize_volume(value: Any) -> str | None:
    """体积：数字字符串，保留 2-3 位小数，单位 CBM 可省略。"""
    return _normalize_weight(value)


def _normalize_date(value: Any) -> str | None:
    """做箱日期：归一为 YYYY-MM-DD。"""
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = normalize_date_value(value, allow_time=False)
    if not isinstance(normalized, str) or not _DATE_RE.fullmatch(normalized):
        return None
    return normalized


def _normalize_text(value: Any) -> str | None:
    """通用文本字段：去首尾空白，空值返回 None。"""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


# 标签词残留：LLM 把标签当值输出（如“中转港代码：”空值被输出为“代码”）
_PORT_LABEL_TOKEN_RE = re.compile(
    r"^(?:代码|中转港代码|转运港代码|中转港|转运港|目的港|卸货港|港区|停靠港区)$"
)


def _strip_port_label_token(value: str | None) -> str | None:
    """清洗端口字段的标签词残留：命中标签词本身（如“代码”）时置 None。"""
    if value and _PORT_LABEL_TOKEN_RE.match(value.strip()):
        return None
    return value


# 中文字符之间的 OCR 断字空格（如“上海凯福国际物流有限公 司”），
# 仅删除“汉字+空格+汉字”形态；英文公司名（如 XILINMEN GRID）不受影响
_CN_INTERNAL_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])")


def _normalize_c_title(value: Any) -> str | None:
    """客户名称：去首尾空白，并删除中文字符之间的 OCR 断字空格。"""
    cleaned = _normalize_text(value)
    if not cleaned:
        return None
    return _CN_INTERNAL_SPACE_RE.sub("", cleaned)


# 日期形态：b_date_time_start 拒绝此类文本（日期应入 b_date）
_DATE_LIKE_RE = re.compile(
    r"^\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}[日号]?)?$|^\d{8}$"
)


def _normalize_b_date_time_start(value: Any) -> str | None:
    """装箱时间：只接受时间描述（如 早上8点/9:00/下午2点）。

    日期形态（YYYY-MM-DD、YYYY年M月D日 等）拒绝归 null——日期属于 b_date，
    避免日期文本冒充时间进入 driver。
    """
    cleaned = _normalize_text(value)
    if not cleaned:
        return None
    if _DATE_LIKE_RE.match(cleaned):
        return None
    return cleaned


def normalize_document_extraction(data: dict[str, Any]) -> OrderDocumentExtraction:
    """把 LLM raw dict 归一到 OrderDocumentExtraction 并做值校验。"""
    order_num1 = _normalize_bill_no(data.get("order_num1"))
    packages = _normalize_packages(data.get("packages"))
    gross_weight = _normalize_weight(data.get("gross_weight"))
    volume = _normalize_volume(data.get("volume"))
    # data 明细行为主；无完整明细行（LLM 未输出 data 键、显式空列表或行内
    # 三项缺失/非法被过滤）但单值三项齐全时，用单值字段回退组装一条完整行；
    # 单值不全时才在提单号存在时回退仅含提单号的行（对齐自由文本 mapper 契约）
    raw_data = data.get("data")
    data_items = _normalize_data_items(raw_data)
    if not data_items and (packages and gross_weight and volume):
        data_items = [
            DocumentCargoItem(
                b_order_num=order_num1,
                j=packages,
                m=gross_weight,
                t=volume,
            )
        ]
    if data_items:
        # 单值字段与 data[0] 对齐（prompt 规则约定），保证响应自洽
        packages = data_items[0].j
        gross_weight = data_items[0].m
        volume = data_items[0].t
        # 单行明细且行内提单号缺失时回退主提单号（对齐自由文本 mapper 约定）
        if len(data_items) == 1 and not data_items[0].b_order_num:
            data_items[0].b_order_num = order_num1
    elif order_num1:
        # 无任何完整明细行但有整票提单号：data 输出一条仅含提单号的行，
        # 对齐自由文本 mapper 契约（data 不允许为空，下游每行提单号非空）；
        # 件数/毛重/体积缺失仍由 _missing_fields 标记走人工确认
        data_items = [DocumentCargoItem(b_order_num=order_num1)]
    try:
        return OrderDocumentExtraction.model_validate(
            {
                "order_num1": order_num1,
                "c_title": _normalize_c_title(data.get("c_title")),
                "c_name": _normalize_text(data.get("c_name")),
                "c_phone": _normalize_text(data.get("c_phone")),
                "b_ship_name": _normalize_text(data.get("b_ship_name")),
                "b_ship_num": _normalize_text(data.get("b_ship_num")),
                "b_ship_company": _normalize_text(data.get("b_ship_company")),
                "factory_name": _normalize_text(data.get("factory_name")),
                "factory_bei": _normalize_text(data.get("factory_bei")),
                "b_factory_not": _normalize_text(data.get("b_factory_not")),
                "b_start_dock": _normalize_text(data.get("b_start_dock")),
                "b_end_port": _strip_port_label_token(
                    _normalize_text(data.get("b_end_port"))
                ),
                "b_end_dock": _strip_port_label_token(
                    _normalize_text(data.get("b_end_dock"))
                ),
                "b_wharf": _normalize_text(data.get("b_wharf")),
                "b_open_ship_time": _normalize_date(data.get("b_open_ship_time")),
                "b_date": _normalize_date(data.get("b_date")),
                "b_date_time_start": _normalize_b_date_time_start(
                    data.get("b_date_time_start")
                ),
                "c_sn": _normalize_text(data.get("c_sn")),
                "c_note": _normalize_text(data.get("c_note")),
                "packages": packages,
                "gross_weight": gross_weight,
                "volume": volume,
                "data": data_items or None,
                "box": _normalize_boxes(data.get("box")),
            }
        )
    except ValidationError as exc:
        raise ParseError(
            "document extraction failed schema validation",
            details={"errors": exc.errors(include_input=False)},
        ) from exc


# ---- 必填校验 + 组装 ----

# 缺失原因：原文中未抽取到 / 值格式不合法被归一化清洗 / 单值已提取但明细行不完整
_MISSING_REASON_NOT_FOUND = "原文未找到，请人工确认"
_MISSING_REASON_INVALID = "格式不合法"
_MISSING_REASON_NOT_IN_ROW = "已提取但明细行不完整，请人工确认"

# 件数/毛重/体积：单值字段格式合法且已归一，但明细行三项不全被跳过时，
# 缺失原因不是"格式不合法"（值本身合法），而是"未计入明细行"
_MEASUREMENT_FIELDS = ("packages", "gross_weight", "volume")


def _missing_fields(extracted: OrderDocumentExtraction) -> list[str]:
    missing: list[str] = []
    if not extracted.order_num1:
        missing.append("order_num1")
    if not extracted.box:
        missing.append("box")
    if not extracted.c_title:
        missing.append("c_title")
    if not extracted.factory_bei:
        missing.append("factory_bei")
    if not extracted.b_date:
        missing.append("b_date")
    # 件数/毛重/体积以 data 明细行为准（含单值回退条目）：无完整明细行时
    # 一律标记缺失——build_document_order_data 只从 data 输出这三项，单值字段
    # 存在但明细行缺失/被跳过时，宁可人工确认也不静默丢数据（避免 data=[]
    # 且确认标记为绿直接导致下游漏数据）
    has_complete_row = any(
        item.j and item.m and item.t for item in (extracted.data or [])
    )
    if not has_complete_row:
        missing += ["packages", "gross_weight", "volume"]
    return missing


def _missing_field_reasons(
    raw: dict[str, Any], extracted: OrderDocumentExtraction
) -> dict[str, str]:
    """对每个缺失字段标注原因：原文未抽取到，或 LLM 有值但格式非法被清洗。"""
    reasons: dict[str, str] = {}
    for field in _missing_fields(extracted):
        value = raw.get(field)
        if value is None:
            reasons[field] = _MISSING_REASON_NOT_FOUND
        elif isinstance(value, str) and not value.strip():
            reasons[field] = _MISSING_REASON_NOT_FOUND
        elif isinstance(value, (list, dict)) and not value:
            reasons[field] = _MISSING_REASON_NOT_FOUND
        elif field in _MEASUREMENT_FIELDS and getattr(extracted, field):
            # 单值格式合法且已归一（如毛重 8000），但明细行件数/体积缺失
            # 被整体跳过 → 未计入订单 data，原因是行不完整而非值不合法
            reasons[field] = _MISSING_REASON_NOT_IN_ROW
        else:
            reasons[field] = _MISSING_REASON_INVALID
    return reasons


def build_document_order_data(
    extracted: OrderDocumentExtraction,
    *,
    customer_id: str = "",
) -> dict[str, Any]:
    """组装下单接口 data 参数（不调用上游）。

    字段结构对齐标准订单格式（与 /orders 自由文本下单的 order_data 一致）；
    缺失的字段保持 null，做箱日期缺失时 driver 显示空对象 [{}]（对齐标准格式）。

    注意：本函数不做必填校验，缺失字段保持空值；由调用方通过
    `_missing_fields` 判断是否需要人工确认。
    """
    data = {
        "order_num1": extracted.order_num1,
        "type": 1,
        "c_title": extracted.c_title,
        "c_name": extracted.c_name,
        "c_phone": extracted.c_phone,
        "b_ship_name": extracted.b_ship_name,
        "b_ship_num": extracted.b_ship_num,
        "b_ship_company": extracted.b_ship_company,
        "factory_name": extracted.factory_name,
        "factory_bei": extracted.factory_bei,
        "b_factory_not": extracted.b_factory_not,
        "b_start_dock": extracted.b_start_dock,
        "b_end_port": extracted.b_end_port,
        "b_end_dock": extracted.b_end_dock,
        "b_wharf": extracted.b_wharf,
        "b_open_ship_time": extracted.b_open_ship_time,
        "c_sn": extracted.c_sn,
        "c_note": extracted.c_note,
        "data": (
            [
                {
                    # 行内无提单号时回填主提单号（对齐自由文本 mapper 契约：下游每行非空）
                    "b_order_num": item.b_order_num or extracted.order_num1,
                    "j": item.j,
                    "m": item.m,
                    "t": item.t,
                    "hh": item.hh,
                    "mt": item.mt,
                }
                for item in extracted.data
            ]
            if extracted.data
            else None
        ),
        "box": [
            {"b_type": item.b_type, "box_num": item.box_num}
            for item in extracted.box
        ],
        "driver": (
            _build_driver_entries(extracted)
        ),
    }
    # 客户 ID 已无配置来源，仅在显式传入时注入（缺省不发送）
    if customer_id:
        data["c_id"] = customer_id
    return data


def _build_driver_entries(extracted: OrderDocumentExtraction) -> list[dict[str, str]]:
    """组装 driver 行：装箱日期 b_date 与装箱时间 b_date_time_start 都有则
    合并为一条；都无则返回 [{}]（对齐标准订单格式）。"""
    entry: dict[str, str] = {}
    if extracted.b_date:
        entry["b_date"] = extracted.b_date
    if extracted.b_date_time_start:
        entry["b_date_time_start"] = extracted.b_date_time_start
    return [entry] if entry else [{}]


# ---- 主流程 ----

def parse_document_to_order(
    file_bytes: bytes,
    filename: str,
    *,
    customer_id: str = "",
) -> dict[str, Any]:
    """上传附件 → 转换 → LLM 抽取 → 校验 → 组装 order_data（不下单）。

    order_data 始终组装并返回（缺字段时缺失项为 null，driver 为 [{}]），
    是否人工确认由 missing_fields / needs_manual_confirmation 标记。
    """
    source_text, doc_format, conversion_meta, user_content = _convert_file(
        file_bytes, filename
    )
    extracted_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    system = _build_system_prompt()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    output_schema = _clean_json_schema(OrderDocumentExtraction.model_json_schema())
    raw, llm_meta = chat_json(messages, temperature=0.0, json_schema=output_schema)
    if not isinstance(raw, dict):
        raise ParseError("LLM output must be a JSON object")

    # 兜底修复：c_title 误取 TO 收件方（做箱通知类单据无 FM 字段时易发生）；
    # LLM 未提取到客户时，从文档抬头主动补全（文档抬头公司即客户）
    if raw_value := raw.get("c_title"):
        raw["c_title"] = _revise_c_title_to_value(raw_value, source_text)
    elif source_text:
        raw["c_title"] = _extract_header_company(source_text)
    # 兜底修复：b_end_port（中转港）与 b_end_dock（目的港）互不混淆，
    # 字段语义对齐订单创建接口文档：b_end_port=中转港、b_end_dock=目的港
    raw["b_end_port"], raw["b_end_dock"] = _revise_port_fields(
        raw.get("b_end_port"), raw.get("b_end_dock"), source_text
    )
    # 兜底修复：关单号即提单号——文档同时存在运编号与关单号时，
    # LLM 可能误取运编号作 order_num1，原文有关单号则一律以关单号为准
    raw["order_num1"] = _revise_bill_no(raw.get("order_num1"), source_text)
    # 兜底修复：截单时间不得作为装箱时间（b_date_time_start 只认做箱/装箱时间）
    if raw.get("b_date_time_start"):
        raw["b_date_time_start"] = _revise_loading_time(
            raw.get("b_date_time_start"), source_text
        )

    extracted = normalize_document_extraction(raw)
    missing = _missing_fields(extracted)
    missing_reasons = _missing_field_reasons(raw, extracted)
    order_data = build_document_order_data(extracted, customer_id=customer_id)

    safe_meta = {key: llm_meta.get(key) for key in ("model", "usage") if key in llm_meta}
    safe_meta.update(conversion_meta)
    safe_meta.update(
        {
            "extracted_at": extracted_at,
            "doc_format": doc_format,
            "source_sha256": hashlib.sha256(file_bytes).hexdigest(),
            "source_bytes": len(file_bytes),
            "order_created": False,
        }
    )
    # vision 交叉核验被跳过（图片超限降级）时同样要求人工确认，
    # 与 skill 端 blocking issue 的口径保持一致
    vision_degraded = "vision_skipped_reason" in conversion_meta
    return {
        "file": filename,
        "extracted": extracted.model_dump(),
        "order_data": order_data,
        "needs_manual_confirmation": bool(missing) or vision_degraded,
        "missing_fields": missing,
        "missing_reasons": missing_reasons,
        "meta": safe_meta,
    }
