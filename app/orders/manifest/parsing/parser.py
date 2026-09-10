"""舱单 xlsx 解析：家族识别 + label 定位提取 + 箱明细内容模式识别。

契约见《舱单导入需求文档》v1.0 §4/§6：
- 仅 .xlsx（magic bytes PK 头；扩展名/内容不符 → 400 file_format_mismatch）
- 家族识别：sheet 名 + 标题锚点（托书 Entrusting books / SI Shipping Instruction）；
  未识别 → 400 unknown_manifest_family
- 家族 A（托书）：label 定位 + 合并大格区块（Shipper/Consignee/Notify Party），
  不依赖固定行列号（容忍布局漂移）；label 右侧值跳过合并延续格（同文本）
- 家族 B（SI）：表单区 label 定位（label:value 同格冒号剥离）+ 箱明细表内容
  模式识别——箱号/封条按正则形态识别（防列位移：变体实测 Quantity 表头位
  放的是封条号），数字列按表头身份归类；TOTAL 行合计优先（NW/GW/CBM）；
  底部汇总行给箱型箱量（10*40HQ）与件数（385 pallets）；变体 1 无箱型来源
  → box_groups 缺失标记。SI 毛重/体积/件数只从明细表取（表单区无此 label，
  避免表头 "GW"/"CBM" 字样误取单箱值）
- 脏数据：多点号数字（20.652.00 → 20652.00，最后一个点为小数点）、千分位、
  浮点尾巴
- 原文保真：公司名/品名/提单号逐字复制（仅 strip 首尾空白）
- 多提单号（v1.8）：全工作簿所有舱单 sheet 的提单号标签值收集进
  ManifestParseOutput.bl_nos，服务层去重后 ≥2 → 文件级拒绝；MBL NO 斜杠
  双号（参考号/船司号）视为两个提单号计入（用户拍板）

一文件一票：parse_manifest 返回 ManifestParseOutput（order + engine + family）。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.core.errors import BadRequestError, ConvertError, UnknownManifestFamilyError

from ..schema import (
    ManifestBoxGroup,
    ManifestContainer,
    ManifestOrder,
)

# 内容格式 magic bytes：xlsx 为 zip（PK\x03\x04）
_XLSX_MAGIC = b"PK\x03\x04"

# 家族识别锚点（sheet 名 / A1 标题，归一后包含匹配）
_FAMILY_AUTHORIZATION_ANCHORS = ("entrustingbooks", "seafreightimportauthorization")
_FAMILY_SI_ANCHORS = ("shippinginstruction", "templatebl/actualdatasi")

# 船名航次拆分：末尾独立 token 形如 2617N / 2607N（3+ 位数字 + 字母结尾）
_VOYAGE_TOKEN_RE = re.compile(r"^\d{2,}[A-Z]$")
# 船名航次串中的日期括号段：SITC HAODE 2617N (ETD: 15/08) → 去括号段
_PARENTHESES_RE = re.compile(r"\([^)]*\)")

# 箱号形态：4 大写字母 + 7 数字（FFAU7731669 / TIIU6545430）
_CONTAINER_NO_RE = re.compile(r"^[A-Z]{4}\d{7}$")
# 封条号形态：SITR + 5~8 数字（SITR814101）
_SEAL_NO_RE = re.compile(r"^SITR\d{5,8}$", re.IGNORECASE)
# 明细格内「箱号 & 封条」同格分隔（FFAU8106619 & SITR825483）与括号内斜杠
_CTN_SEAL_SPLIT_RE = re.compile(r"\s*[&/]\s*")

# 箱型箱量汇总：10*40HQ / 1*40HQ / 40HQ*2（两种乘号顺序）
_BOX_QTY_RE = re.compile(
    r"(\d{1,3})\s*\*\s*(\d{2}[A-Z]{2,3})|(\d{2}[A-Z]{2,3})\s*\*\s*(\d{1,3})"
)
# 件数汇总：385 pallets / 14 wooden crates（数字后跟字母）；纯数字兜底
_PIECES_RE = re.compile(r"(\d{1,7})\s*[A-Za-z]")

# 明细表数字列身份（表头归一后包含匹配）
_QTY_HEADERS = ("quantity", "package")
_NW_HEADERS = ("nw",)
_GW_HEADERS = ("gw",)
_VGM_HEADERS = ("vgm",)
_CBM_HEADERS = ("cbm", "volume")

# 家族识别锚点扫描上限（明细表头行可达 R23+，识别只看 sheet 名/A1 不受限）
_LABEL_SCAN_ROWS = 60


@dataclass
class ManifestParseOutput:
    """解析输出：一票订单 + 引擎与家族元信息（服务层组装响应用）。"""

    order: ManifestOrder
    engine: str
    family: str
    bl_nos: list[str] = field(default_factory=list)


class _SheetView:
    """统一工作表视图：1-based 行列访问，合并区域填充左上角值（label 区必需）。"""

    def __init__(self, ws):
        self.nrows = ws.max_row or 0
        self.ncols = ws.max_column or 0
        self._ws = ws
        self._merged: dict[tuple[int, int], Any] = {}
        for rng in ws.merged_cells.ranges:
            top_left = ws.cell(rng.min_row, rng.min_col).value
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    self._merged[(r, c)] = top_left

    def cell(self, row: int, col: int) -> Any:
        """合并填充值：合并区域内返回左上角值（label 区读取口径）。"""
        if (row, col) in self._merged:
            return self._merged[(row, col)]
        return self._ws.cell(row, col).value

    def text(self, row: int, col: int) -> str:
        """单元格文本（strip 后；空值返回空串）。"""
        value = self.cell(row, col)
        return str(value).strip() if value is not None and str(value).strip() else ""


def _norm(text: str) -> str:
    """label 归一：去全部空白、转小写（锚点与 label 匹配用）。"""
    return re.sub(r"\s+", "", text).lower()


def _clean_number(value: Any) -> float | None:
    """数字清洗 → float；空值/非数字返回 None（缺失语义）。

    兼容形态：数字单元格（16000）、浮点尾巴（16000.0）、千分位（16,000）、
    多点号（20.652.00 → 20652.00：≥2 个点时最后一个为小数点，其余删除）。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    if text.count(".") >= 2:
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        return float(text)
    except ValueError:
        return None


def _split_vessel_voyage(text: str) -> tuple[str | None, str | None]:
    """船名航次串拆分 → (船名, 航次)。

    去日期括号段后按空白切 token，末尾 token 匹配航次形态（2617N）归航次，
    其余归船名；无航次 token 时整体为船名。
    """
    cleaned = _PARENTHESES_RE.sub("", str(text)).strip()
    tokens = cleaned.split()
    if not tokens:
        return None, None
    if len(tokens) >= 2 and _VOYAGE_TOKEN_RE.match(tokens[-1]):
        vessel = " ".join(tokens[:-1]).strip()
        return (vessel or None), tokens[-1]
    return (cleaned or None), None


def _find_label(
    sheet: _SheetView, patterns: tuple[str, ...], max_row: int = _LABEL_SCAN_ROWS
) -> tuple[int, int] | None:
    """扫描区内定位 label 单元格（归一后前缀或包含匹配）→ (row, col)。"""
    for r in range(1, min(max_row, sheet.nrows) + 1):
        for c in range(1, sheet.ncols + 1):
            norm = _norm(sheet.text(r, c))
            if norm and any(norm.startswith(p) or p in norm for p in patterns):
                return r, c
    return None


def _label_value_right(sheet: _SheetView, row: int, col: int, span: int = 8) -> str:
    """label 右侧同行第一个非空值；跳过合并延续格（与 label 同文本）。"""
    label_text = sheet.text(row, col)
    for c in range(col + 1, min(col + span, sheet.ncols) + 1):
        text = sheet.text(row, c)
        if text and text != label_text:
            return text
    return ""


def _label_value_below(
    sheet: _SheetView, row: int, col: int, row_span: int = 4, col_span: int = 6
) -> str:
    """label 下方区块内第一个非空值（托书/SI 三栏公司名合并大格口径）。

    扫描窗口 [col, col+col_span-1]；跳过疑似 label（短文本含冒号）。
    """
    for r in range(row + 1, min(row + row_span, sheet.nrows) + 1):
        for c in range(col, min(col + col_span - 1, sheet.ncols) + 1):
            text = sheet.text(r, c)
            if text and not (len(text) <= 40 and ":" in text):
                return text
    return ""


def _parse_box_qty(text: str) -> list[ManifestBoxGroup]:
    """箱型箱量文本 → 聚合条目（10*40HQ / 1*40HQ / 40HQ*2）。"""
    groups: list[ManifestBoxGroup] = []
    for m in _BOX_QTY_RE.finditer(str(text)):
        if m.group(1) and m.group(2):
            groups.append(ManifestBoxGroup(b_type=m.group(2), ctn_num=int(m.group(1))))
        elif m.group(3) and m.group(4):
            groups.append(ManifestBoxGroup(b_type=m.group(3), ctn_num=int(m.group(4))))
    return groups


def _parse_pieces(text: str) -> float | None:
    """件数：数字+字母形态取数字（385 pallets / 14 wooden crates）；纯数字兜底。"""
    m = _PIECES_RE.search(str(text))
    if m:
        return float(m.group(1))
    return _clean_number(text)


# 品名块内可识别的元数据行（INVOICE No. / HS CODE : …；剔除后余下即品名，
# TMS 实测 goodsEname ≤ 100 字符，剔除元数据行替代硬截断）
_GOODS_METADATA_LINE_RE = re.compile(
    r"^\s*(invoice\s*(no|number)?\.?|hs\s*code)\s*[:：]", re.IGNORECASE
)


def _clean_goods_name(text: str | None) -> str | None:
    """品名块 → 品名：按行剔除 INVOICE No./HS CODE 元数据行，其余原文保真。

    全部行被剔除（极端：纯元数据块）→ 回退原文，不产出空品名。
    """
    if not text:
        return text
    lines = text.splitlines()
    kept = [ln for ln in lines if not _GOODS_METADATA_LINE_RE.match(ln)]
    cleaned = "\n".join(kept).strip()
    return cleaned or text.strip()


def _mbl_no_slash(text: str | None) -> str | None:
    """托书 MBL 双号（参考号/船司号）→ 取斜杠后段（船司 MBL）。

    TMS 实测 bOrderNum 含斜杠 → 下游 500；后段与其余舱单
    SITGB 号段一致（SITGBASH006434 vs SITGBAYP006017/SITGBAQI005920）。
    仅用于主提取 bl_no 的展示口径；v1.8 起斜杠双号已按两个提单号计入
    多提单号拒绝（被拒文件不提交，TMS 无斜杠约束不再可达）。
    无斜杠/后段为空 → 原样返回。
    """
    if not text or "/" not in text:
        return text
    tail = text.rsplit("/", 1)[-1].strip()
    return tail or text


# 三栏合并大格拆分标记（TMS 实测三栏 Tel/Address 空值核查：
# 名称/地址/电话糊在同一格 → 按标记拆分归位，未匹配标记的部分原文保真）
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TEL_LABEL_RE = re.compile(
    r"\b(?:tel|phone\s*number|phone|mobile)\s*[:：]?\s*([+\d][\d\s\-()]{5,}\d)",
    re.IGNORECASE,
)
_PHONE_11_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")  # 大陆手机号形态
_ADDR_LINE_RE = re.compile(r"^\s*(?:registered\s*)?address\s*[:：]\s*(.+)$", re.IGNORECASE)
_ADD_LINE_RE = re.compile(r"^\s*add\s*[:：]\s*(.+)$", re.IGNORECASE)
_PARTY_DROP_LINE_RE = re.compile(r"^\s*(vat|pic)\s*[:：]", re.IGNORECASE)
_CONTACT_CUT_RE = re.compile(r"\s+(?:receiver|pic|contact)\s*[:：]", re.IGNORECASE)
_ADDR_INLINE_RE = re.compile(r"\s(No\.?\s*\d)", re.IGNORECASE)
_GAP_SPLIT_RE = re.compile(r"\s{4,}")


def _split_party_block(text: str | None) -> tuple[str | None, str | None, str | None]:
    """三栏合并大格 → (公司名, 地址, 电话)。

    规则（先取走再归类，未匹配部分原文保真进名称）：
    - 电话：Tel/Phone 标签后取号；无标签时找 11 位手机号形态；取走后从原文移除；
    - 邮箱/VAT/PIC 行：无 TMS 落点，剔除；
    - 地址：Address：/ADD: 行取冒号后内容；单行连排时 ' No. ' 编号地址起点切分，
      Receiver/PIC/Contact 起截断联系人段；
    - 无任何标记（如托书发货人格）→ 整块进名称（不猜切分）。
    """
    if not text or not text.strip():
        return None, None, None
    raw = text.strip()
    tel = None
    m = _TEL_LABEL_RE.search(raw)
    if m:
        tel = re.sub(r"\s+", " ", m.group(1)).strip()
        raw = (raw[: m.start()] + raw[m.end() :]).strip()
    else:
        m = _PHONE_11_RE.search(raw)
        if m:
            tel = m.group(0)
            raw = (raw[: m.start()] + raw[m.end() :]).strip()
    raw = _EMAIL_RE.sub(" ", raw)
    address = None
    name_lines: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _ADDR_LINE_RE.match(s) or _ADD_LINE_RE.match(s)
        if m:
            address = f"{address} {m.group(1).strip()}" if address else m.group(1).strip()
            continue
        if _PARTY_DROP_LINE_RE.match(s):
            continue
        name_lines.append(s)
    name = " ".join(name_lines).strip() or None
    if address is None and name:
        # 单行连排：联系人段截断后切名称/地址
        cut = _CONTACT_CUT_RE.search(name)
        base = name[: cut.start()].rstrip() if cut else name
        # ① ≥4 连续空隙且后半含数字（SI v1 排版：名称␣␣␣␣地址，无任何标记）
        for gap in _GAP_SPLIT_RE.finditer(base):
            tail = base[gap.end() :]
            if re.search(r"\d", tail):
                return base[: gap.start()].strip() or None, tail.strip(), tel
        # ② ' No. ' 编号地址起点（含 NO .05B2 等变体）
        m = _ADDR_INLINE_RE.search(base)
        if m:
            address = base[m.start() :].strip()
            name = base[: m.start()].strip() or None
    return name, address, tel


def _expand_same_as_consignee(order: ManifestOrder) -> None:
    """SAME AS CONSIGNEE 展开：通知人名/地址/电话 = 收货人对应值。"""
    if order.notifier_name and _norm(order.notifier_name) == "sameasconsignee":
        order.notifier_name = order.consignee_name
        order.notifier_address = order.consignee_address
        order.notifier_tel = order.consignee_tel


# ---- 家族 A：托书（Entrusting books）----


def _parse_authorization(sheet: _SheetView) -> ManifestOrder:
    """托书家族提取（label 定位；区块值在 label 下方合并大格）。"""
    order = ManifestOrder(family="authorization")

    # 提单号：MBL NO（双号斜杠 → 取后段船司 MBL，TMS 实测口径）
    pos = _find_label(sheet, ("mblno",))
    if pos:
        order.bl_no = _mbl_no_slash(_label_value_right(sheet, *pos)) or None

    # ETD 行（含船名航次 + 日期括号）：SITC HAODE 2617N (ETD: 15/08)
    pos = _find_label(sheet, ("etd",))
    if pos:
        raw = _label_value_right(sheet, *pos)
        if raw:
            order.etd_text = raw
            order.vessel, order.voyage = _split_vessel_voyage(raw)

    # 三栏：区块头（shipper/consignee/notifier）下方合并大格 → 名称/地址/电话拆分
    for label_keys, prefix in (
        (("shipper",), "shipper"),
        (("consignee",), "consignee"),
        (("notifyparty", "notifier"), "notifier"),
    ):
        pos = _find_label(sheet, label_keys)
        if pos:
            name, address, tel = _split_party_block(_label_value_below(sheet, pos[0], 1))
            setattr(order, f"{prefix}_name", name)
            setattr(order, f"{prefix}_address", address)
            setattr(order, f"{prefix}_tel", tel)
    _expand_same_as_consignee(order)

    # 港口三件（原文保真；span=4 防越区误取同行右侧其它 label 的值）
    pos = _find_label(sheet, ("portofloading",))
    if pos:
        order.pol = _label_value_right(sheet, *pos, span=4) or None
    pos = _find_label(sheet, ("portofdischarge",))
    if pos:
        order.pod = _label_value_right(sheet, *pos, span=4) or None
    pos = _find_label(sheet, ("finaldestination",))
    if pos:
        order.final_destination = _label_value_right(sheet, *pos, span=4) or None

    # 箱型箱量 + 箱号封号：Container volume 行「1*40HQ (FFAU7731669/SITR853037)」
    pos = _find_label(sheet, ("containervolume",))
    if pos:
        raw = _label_value_right(sheet, *pos)
        if raw:
            order.box_groups = _parse_box_qty(raw)
            inner = re.search(r"\(([^)]*)\)", raw)
            if inner:
                for part in _CTN_SEAL_SPLIT_RE.split(inner.group(1)):
                    part = part.strip()
                    if not part:
                        continue
                    if _CONTAINER_NO_RE.match(part):
                        order.containers.append(ManifestContainer(container_no=part))
                    elif _SEAL_NO_RE.match(part) and order.containers:
                        order.containers[-1].seal_no = part

    # 明细行（表头行下第一数据行）：件数/品名/毛重/体积/唛头
    pos = _find_label(sheet, ("quantityandpackagingunit", "quantity&package"))
    if pos:
        order.pieces = _parse_pieces(_label_value_below(sheet, *pos, row_span=3))
    pos = _find_label(sheet, ("productname", "descriptionofgoods"))
    if pos:
        order.goods_ename = _clean_goods_name(
            _label_value_below(sheet, *pos, row_span=4)
        ) or None
    pos = _find_label(sheet, ("grossweight",))
    if pos:
        order.gross_weight = _clean_number(
            _label_value_below(sheet, *pos, row_span=3, col_span=2)
        )
    pos = _find_label(sheet, ("volumecbm",))
    if pos:
        order.volume_cbm = _clean_number(
            _label_value_below(sheet, *pos, row_span=3, col_span=2)
        )
    # 唛头：Mark & numbers 合并区（A18:B19 形态，仅扫自身列）
    pos = _find_label(sheet, ("mark&numbers", "shippingmark"))
    if pos:
        order.shipping_mark = _label_value_below(sheet, *pos, row_span=4, col_span=2) or None

    _mark_missing(order)
    return order


# ---- 家族 B：SI（Shipping Instruction）----


def _si_form_value(sheet: _SheetView, label_keys: tuple[str, ...]) -> str:
    """SI 表单区取值：label 右侧值；「label : value」同格（含合并延续）冒号剥离。

    右侧无值且同格无冒号可剥离（如空值的 HS CODE label）→ 返回空串，
    不回退 label 自身文本。
    """
    pos = _find_label(sheet, label_keys)
    if not pos:
        return ""
    label_text = sheet.text(*pos)
    raw = _label_value_right(sheet, *pos)
    if not raw:
        raw = label_text
    # 同格/合并延续：值文本以 label 开头 → 剥离冒号前段
    if raw.startswith(label_text.rstrip(":")):
        _, sep, tail = raw.partition(":")
        if sep:
            return tail.strip()
        return ""
    return raw


def _si_block_value(sheet: _SheetView, label_keys: tuple[str, ...]) -> str:
    """SI 三栏公司名：区块头下方（A 列合并区块，如 A3:B8）。"""
    pos = _find_label(sheet, label_keys)
    if not pos:
        return ""
    return _label_value_below(sheet, pos[0], pos[1], row_span=6, col_span=3)


def _parse_si(sheet: _SheetView) -> ManifestOrder:
    """SI 家族提取：表单区 label 定位 + 箱明细表内容模式识别（防列位移）。

    毛重/体积/件数只从明细表（TOTAL/底部汇总）取——表单区无对应 label，
    避免明细表头 "GW"/"CBM" 字样被 _find_label 误命中取到单箱值。
    """
    order = ManifestOrder(family="si")

    order.bl_no = _si_form_value(sheet, ("booking/blnumber", "blnumber")) or None
    vessel_raw = _si_form_value(sheet, ("vessel",))
    if vessel_raw:
        order.vessel, order.voyage = _split_vessel_voyage(vessel_raw)
    order.pol = _si_form_value(sheet, ("pol", "portofloading")) or None
    order.pod = _si_form_value(sheet, ("pod", "portofdischarge")) or None
    order.hs_code = _si_form_value(sheet, ("hscode",)) or None
    for label_keys, prefix in (
        (("shipper",), "shipper"),
        (("consignee",), "consignee"),
        (("notifyparty", "notifier"), "notifier"),
    ):
        name, address, tel = _split_party_block(_si_block_value(sheet, label_keys))
        setattr(order, f"{prefix}_name", name or None)
        setattr(order, f"{prefix}_address", address)
        setattr(order, f"{prefix}_tel", tel)
    _expand_same_as_consignee(order)
    pos = _find_label(sheet, ("descriptionofgoods",))
    if pos:
        order.goods_ename = _clean_goods_name(sheet.text(pos[0] + 1, pos[1])) or None

    _parse_si_detail(sheet, order)
    _mark_missing(order)
    return order


def _parse_si_detail(sheet: _SheetView, order: ManifestOrder) -> None:
    """SI 箱明细表：表头行（Container & Seal No）定位 + 逐行内容模式识别。

    列位移防线（实测变体 2）：Quantity 表头位实放封条号（SITR814101）——
    箱号/封条按正则形态识别（不盲信列位）；数字列按表头身份归类；
    TOTAL 行合计优先（NW/GW/CBM）；底部汇总给箱型箱量与件数；
    变体 1 无箱型来源 → box_groups 留空由 _mark_missing 登记。
    """
    header_pos = _find_label(sheet, ("container&sealno", "container&seal"))
    if not header_pos:
        return
    header_row, header_col = header_pos

    # 数字列身份：表头行内各列 → qty/nw/gw/vgm/cbm
    col_roles: dict[int, str] = {}
    for c in range(header_col + 1, sheet.ncols + 1):
        head = _norm(sheet.text(header_row, c))
        if not head:
            continue
        if any(k in head for k in _QTY_HEADERS):
            col_roles[c] = "qty"
        elif any(k in head for k in _NW_HEADERS):
            col_roles[c] = "nw"
        elif any(k in head for k in _GW_HEADERS):
            col_roles[c] = "gw"
        elif any(k in head for k in _VGM_HEADERS):
            col_roles[c] = "vgm"
        elif any(k in head for k in _CBM_HEADERS):
            col_roles[c] = "cbm"

    detail_qtys: list[float] = []
    # 明细行数字兑底（TOTAL 行无合计时用，如变体 1 单箱明细即合计）
    detail_nw: list[float] = []
    detail_gw: list[float] = []
    detail_cbm: list[float] = []
    for r in range(header_row + 1, sheet.nrows + 1):
        whole_row = [sheet.text(r, c) for c in range(1, sheet.ncols + 1)]
        nonempty = [t for t in whole_row if t]
        if not nonempty:
            continue
        # TOTAL 行判定：行首两格含 "total"
        if any("total" in _norm(t) for t in nonempty[:2]):
            _parse_si_total_row(sheet, r, col_roles, order)
            continue
        # 明细行：内容识别（箱号/封条形态优先；数字按列身份归类兼收集）
        row_qty: float | None = None
        for c, text in enumerate(whole_row, start=1):
            if not text:
                continue
            for part in _CTN_SEAL_SPLIT_RE.split(text):
                part = part.strip()
                if _CONTAINER_NO_RE.match(part):
                    order.containers.append(ManifestContainer(container_no=part))
                elif _SEAL_NO_RE.match(part) and order.containers:
                    if order.containers[-1].seal_no is None:
                        order.containers[-1].seal_no = part
            role = col_roles.get(c)
            value = _clean_number(text)
            if value is None:
                continue
            if role == "qty":
                row_qty = value
            elif role == "nw":
                detail_nw.append(value)
            elif role == "gw":
                detail_gw.append(value)
            elif role == "cbm":
                detail_cbm.append(value)
        if row_qty is not None:
            detail_qtys.append(row_qty)

    # TOTAL 行无合计时（如变体 1）明细兑底；件数兑底：明细 qty 合计
    if order.net_weight is None and detail_nw:
        order.net_weight = sum(detail_nw)
    if order.gross_weight is None and detail_gw:
        order.gross_weight = sum(detail_gw)
    if order.volume_cbm is None and detail_cbm:
        order.volume_cbm = sum(detail_cbm)
    if order.pieces is None and detail_qtys:
        order.pieces = sum(detail_qtys)


def _parse_si_total_row(
    sheet: _SheetView, row: int, col_roles: dict[int, str], order: ManifestOrder
) -> None:
    """TOTAL 行合计（单次取值）+ 其后底部汇总行（箱型箱量 / 件数）。

    底部汇总在 TOTAL 行下方 1~4 行内扫描：「10*40HQ」「385 pallets」。
    """
    has_gw_col = any(role == "gw" for role in col_roles.values())
    for c, role in col_roles.items():
        value = _clean_number(sheet.cell(row, c))
        if value is None:
            continue
        if role == "gw" and order.gross_weight is None:
            order.gross_weight = value
        elif role == "cbm" and order.volume_cbm is None:
            order.volume_cbm = value
        elif role == "nw" and order.net_weight is None:
            order.net_weight = value
        elif role == "vgm" and order.gross_weight is None and not has_gw_col:
            # 无 GW 列身份时 VGM 合计兜底（保守：仅当 GW 缺位）
            order.gross_weight = value

    for r in range(row + 1, min(row + 4, sheet.nrows) + 1):
        for c in range(1, sheet.ncols + 1):
            text = sheet.text(r, c)
            if not text:
                continue
            if not order.box_groups:
                groups = _parse_box_qty(text)
                if groups:
                    order.box_groups = groups
                    continue  # 箱型汇总格不作件数来源（10*40HQ 的 40 不是件数）
            if order.pieces is None:
                order.pieces = _parse_pieces(text)


def _mark_missing(order: ManifestOrder) -> None:
    """必填缺失登记（预览报告；create 拦截由服务层按 MANIFEST_REQUIRED 执行）。"""
    if not order.bl_no:
        order.add_missing("bl_no")
    if not order.box_groups:
        order.add_missing("box_groups")
    if not order.pol:
        order.add_missing("pol")


def _detect_manifest_sheets(wb) -> list[tuple[Any, str]]:
    """全部可识别为舱单票的 sheet → [(worksheet, family)]。

    v1.8 多提单号检测需要全量：跳过空名/SheetN 命名的工作表（SI 模版的
    Sheet2/Sheet3 空表）；主识别未命中时兜底扫描任意 sheet 的 A1。
    同一 sheet 按 sheet 名命中后不再按 A1 重复计入。
    """
    results: list[tuple[Any, str]] = []
    seen: set[int] = set()
    for pass_a1 in (False, True):
        for ws in wb.worksheets:
            hints = [] if pass_a1 else [_norm(ws.title or "")]
            a1 = ws.cell(1, 1).value
            if a1:
                hints.append(_norm(str(a1)))
            for hint in hints:
                family = None
                if any(anchor in hint for anchor in _FAMILY_AUTHORIZATION_ANCHORS):
                    family = "authorization"
                elif any(anchor in hint for anchor in _FAMILY_SI_ANCHORS):
                    family = "si"
                if family and id(ws) not in seen:
                    seen.add(id(ws))
                    results.append((ws, family))
                    break
    return results


def _detect_manifest_sheet(wb) -> tuple[Any, str] | None:
    """主识别 sheet（首个命中）；多 sheet 全量见 _detect_manifest_sheets。"""
    sheets = _detect_manifest_sheets(wb)
    return sheets[0] if sheets else None


# 提单号标签锚点（norm 后包含匹配；HBL NO 分单天然不匹配，不计入主提单号）
_MBL_NO_LABELS = ("mblno",)
_SI_BL_NO_LABELS = ("booking/blnumber", "blnumber", "mblno")


def _collect_sheet_bl_nos(sheet: _SheetView, family: str) -> list[str]:
    """sheet 内全部提单号标签值（多 label 并存全收集；斜杠双号按两号拆分）。

    与 _find_label 同口径（norm 前缀/包含匹配），但收集全部命中位置而非首个：
    表单区同时存在多个提单号 label（如 Booking/BL Number 与 MBL NO）时
    各取其值；label:value 同格（SI 合并格）冒号剥离；HBL NO 不匹配锚点；
    MBL NO 斜杠双号（参考号/船司号）按 `/` 拆分为两个候选（v1.8 用户拍板：
    视为两个提单号计入多提单号拒绝，不再取后段计 1 个）。
    """
    labels = _MBL_NO_LABELS if family == "authorization" else _SI_BL_NO_LABELS
    values: list[str] = []
    for r in range(1, min(_LABEL_SCAN_ROWS, sheet.nrows) + 1):
        for c in range(1, sheet.ncols + 1):
            norm = _norm(sheet.text(r, c))
            if not norm or norm.startswith("hbl") or not any(
                norm.startswith(p) or p in norm for p in labels
            ):
                continue
            label_text = sheet.text(r, c)
            raw = _label_value_right(sheet, r, c)
            if not raw:
                raw = label_text
            # 同格/合并延续：值文本以 label 开头 → 剥离冒号前段（对齐 _si_form_value）
            if raw.startswith(label_text.rstrip(":")):
                _, sep, tail = raw.partition(":")
                value = tail.strip() if sep else ""
            else:
                value = raw
            for part in value.split("/"):
                part = part.strip()
                if part and part not in values:
                    values.append(part)
    return values


def _collect_manifest_bl_nos(wb) -> list[str]:
    """全工作簿提单号候选（多票检测）：所有舱单 sheet 的提单号标签值。"""
    bl_nos: list[str] = []
    for ws, family in _detect_manifest_sheets(wb):
        for value in _collect_sheet_bl_nos(_SheetView(ws), family):
            if value not in bl_nos:
                bl_nos.append(value)
    return bl_nos


def parse_manifest(file_bytes: bytes) -> ManifestParseOutput:
    """解析舱单 xlsx → 一票 ManifestOrder（入口；格式/家族校验）。"""
    if not file_bytes:
        raise BadRequestError("empty manifest file", code="empty_file")
    if not file_bytes.startswith(_XLSX_MAGIC):
        raise BadRequestError(
            "manifest is not a .xlsx file (magic bytes mismatch)",
            code="file_format_mismatch",
            # 显式 description（审查修正）：覆盖全局码的账单专属文案
            description="附件格式不匹配，请上传 .xlsx 格式的舱单文件",
            details={"expected": ".xlsx", "magic": file_bytes[:4].hex()},
        )
    try:
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ConvertError(f"manifest workbook cannot be opened: {exc}") from exc

    detected = _detect_manifest_sheet(wb)
    if not detected:
        raise UnknownManifestFamilyError(
            "manifest family not recognized (neither authorization form nor SI)",
            details={"sheets": [ws.title for ws in wb.worksheets]},
        )
    ws, family = detected
    sheet = _SheetView(ws)
    order = _parse_authorization(sheet) if family == "authorization" else _parse_si(sheet)
    return ManifestParseOutput(
        order=order,
        engine="openpyxl",
        family=family,
        bl_nos=_collect_manifest_bl_nos(wb),
    )
