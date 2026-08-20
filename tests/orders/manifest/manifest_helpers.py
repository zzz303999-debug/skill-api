"""舱单导入测试共享工具与 golden 常量（pytest rootdir 模式：目录在 sys.path）。

合成模版构造器用 openpyxl 内存生成 xlsx，复刻两家族真实布局特征：
- 托书：合并大格三栏区 + label 右值/下值 + Container volume 括号箱型箱号
- SI：label:value 同格 + 明细表（表头列位）+ TOTAL 行 + 底部汇总；
  shifted=True 复刻变体 2 列位移（Quantity 位放封条号）与脏 VGM
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from openpyxl import Workbook

GOLDEN_DIR = Path(__file__).resolve().parent.parent.parent / "golden" / "manifest"
REAL_AUTHORIZATION = GOLDEN_DIR / "EN-海运进口托书.xlsx"
REAL_SI_V1 = GOLDEN_DIR / "TEMPLATE FINAL SI SITC (1).xlsx"
REAL_SI_V2 = GOLDEN_DIR / "TEMPLATE FINAL SI SITC(SITGBAQI005920) - - 副本(3)(1)(1).xlsx"

REAL_MISSING = "golden 样本未入库（表格文件不入库），本地放置后自动启用"


class FakeResponse:
    """下游 HTTP 响应替身：payload 序列化为 text；json() 返回 payload。"""

    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        import json

        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _to_bytes(wb: Workbook) -> bytes:
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_authorization_bytes() -> bytes:
    """合成托书（家族 A）：label 布局对齐真实模版（合并大格 + 右值/下值）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = " Entrusting books"
    ws.merge_cells("A1:J1")
    ws["A1"] = "Sea Freight Import Authorization Form"
    # 三栏公司名：区块头 + 下方合并大格
    ws["A2"] = "shipper"
    ws["B2"] = "Shipper:"
    ws.merge_cells("A3:E5")
    ws["A3"] = "PT ENNOVI METAL CAST ENGINEERING SERVICE"
    ws["A6"] = "consignee"
    ws["B6"] = "Consignee:"
    ws.merge_cells("A7:E9")
    ws["A7"] = "INTERPLEX (SUZHOU) PRECISION ENGINEERING"
    ws["A10"] = "Notifier"
    ws["B10"] = "Notify Party:"
    ws.merge_cells("A11:E13")
    ws["A11"] = "SAME AS CONSIGNEE"
    # 单头字段：label 右值（F 列 label，H 列值）
    for row, label, value in (
        (2, "Business type:", "FCL Sea Freight Export"),
        (3, "MBL NO:", "SIT0807BASH591/SITGBASH006434"),
        (4, "HBL NO:", None),
        (5, "Transaction method:", "EXW"),
        (7, "ETD: Launch Date", "SITC HAODE 2617N (ETD: 15/08)"),
    ):
        ws.cell(row, 6, label)
        if value is not None:
            ws.cell(row, 8, value)
    # 港口三件：A 列 label（A:C 合并延续）+ D 列值
    for row, label, value in (
        (14, "Port of Loading: (Port of Loading)", "BATAM, INDONESIA"),
        (15, "Port of Discharge:", None),
        (16, "Final Destination: Port of Destination", "SHANGHAI, CHINA"),
    ):
        ws.cell(row, 1, label)
        if value is not None:
            ws.cell(row, 4, value)
    # 箱型箱量 + 箱号封号
    ws.cell(15, 6, "Container volume")
    ws.cell(15, 8, "1*40HQ (FFAU7731669/SITR853037)")
    # 明细表头 + 数据行
    ws.cell(17, 1, "Mark & numbers")
    ws.cell(17, 3, "Quantity and Packaging Unit")
    ws.cell(17, 5, "Product Name (in both Chinese and English)")
    ws.cell(17, 8, "Gross weight (GW, KGS)")
    ws.cell(17, 10, "Volume CBM")
    ws.cell(18, 3, "14 wooden crates")
    ws.cell(18, 5, "8480411000 Die casting mold (used)")
    ws.cell(18, 8, 16000)
    return _to_bytes(wb)


def build_si_bytes(shifted: bool = False) -> bytes:
    """合成 SI（家族 B）：label:value 同格 + 明细表 + TOTAL + 底部汇总。

    shifted=False 复刻变体 1（& 同格箱号封条、无箱型来源）；
    shifted=True 复刻变体 2（Quantity 位放封条 → 列位移、10 箱、底部汇总）。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Shipping Instruction"
    ws.merge_cells("A1:G1")
    ws["A1"] = "Template BL / Actual Data SI"
    # 表单区（C 列 label，D 列值；Booking 同格 label:value）
    ws.merge_cells("C2:G5")
    ws["C2"] = "Booking / BL Number : SITGBAYP006017" if not shifted else "Booking / BL Number : SITGBAQI005920"
    ws["C6"] = "Vessel :"
    ws["D6"] = "SITC INCHON 2618N" if not shifted else "ORIENTAL BRIGHT 2618N"
    ws["C7"] = "Term Payment"
    ws["D7"] = "TT"
    ws["C8"] = "HS CODE"
    ws["D8"] = "12030000" if not shifted else None
    ws["C9"] = "POL :"
    ws["D9"] = "BATAM BATU AMPAR, INDONESIA" if not shifted else "BATAM, Indoneisa"
    ws["C10"] = "POD : "
    ws["D10"] = "YANGPUGANG,CHINA" if not shifted else "QINZHOU, China"
    # 三栏（A 列区块头 + 下方合并）
    ws.merge_cells("A2:B2")
    ws["A2"] = "Shipper "
    ws.merge_cells("A3:B8")
    ws["A3"] = "CV VANYA COCO UNIVERSAL"
    ws.merge_cells("A9:B9")
    ws["A9"] = "Consignee "
    ws.merge_cells("A10:B19")
    ws["A10"] = "HAI NAN YINGHE TRADING CO.,LTD."
    ws.merge_cells("A20:B20")
    ws["A20"] = "Notify Party "
    ws.merge_cells("A21:B22")
    ws["A21"] = "SAME AS CONSIGNEE" if not shifted else "NINGBO HETIAN SUPPLY CHAIN CO"
    # 品名块
    ws.merge_cells("C13:G13")
    ws["C13"] = "Description of Goods "
    ws.merge_cells("C14:G22")
    ws["C14"] = "DRIED COCONUT SKIN" if not shifted else "183 carton\nINVOICE No.: BCI26062502"
    # 明细表
    ws.merge_cells("A23:B23")
    ws["A23"] = "Container & Seal No."
    ws["C23"] = "Quantity & Package"
    ws["D23"] = "NW"
    ws["E23"] = "GW"
    ws["F23"] = "VGM"
    ws["G23"] = "CBM"
    if not shifted:
        # 变体 1：& 同格箱号封条；qty 列位是数字
        ws.append([])
        ws["A24"] = 1
        ws["B24"] = "FFAU8106619 & SITR825483"
        ws["C24"] = 875
        ws["D24"] = 24326
        ws["E24"] = 28026
        ws["F24"] = 60526
        for r in range(25, 29):
            ws.cell(r, 1, r - 23)  # 空序号行
        ws["B29"] = "TOTAL"
        ws["D29"] = 24326
        ws["E29"] = 28026
        ws["F29"] = 60526
    else:
        # 变体 2：封条占 Quantity 位（列位移）；脏 VGM 多点号
        rows = [
            ("TIIU6545430", "SITR814101", 17030, 17448, 21148, 48.24288),
            ("HPCU5314463", "SITR814102", 16998, 17416, 21116, 48.00096),
        ]
        for i, (ctn, seal, nw, gw, vgm, cbm) in enumerate(rows, start=24):
            ws.cell(i, 1, i - 23)
            ws.cell(i, 2, ctn)
            ws.cell(i, 3, seal)  # 封条占 Quantity 位
            ws.cell(i, 4, nw)
            ws.cell(i, 5, gw)
            ws.cell(i, 6, "20.652.00" if i == 25 else vgm)  # 脏 VGM
            ws.cell(i, 7, cbm)
        ws["B26"] = "TOTAL"
        ws["D26"] = 34028
        ws["E26"] = 34864
        ws["F26"] = 42264
        ws["G26"] = 96.24384
        ws["B28"] = "2*40HQ"
        ws["E28"] = 770
        ws["F28"] = "pallets"
    return _to_bytes(wb)


def build_unknown_bytes() -> bytes:
    """未识别布局：普通表格（无家族锚点）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "随便一个表"
    ws["B2"] = "客户编号"
    return _to_bytes(wb)
