"""舱单解析层测试：家族识别、双家族字段提取、列位移变体、缺失标记、坏文件。

对齐《舱单导入需求文档》v1.0 §4（模版实况）与 §6（提取规则）。
"""

from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook

from app.core.errors import BadRequestError, ConvertError, UnknownManifestFamilyError
from app.orders.manifest import parse_manifest


class TestFamilyDetection:
    """家族识别：sheet 名/A1 标题锚点；未识别 400。"""

    def test_authorization_by_sheet_name(self, auth_bytes):
        out = parse_manifest(auth_bytes)
        assert out.family == "authorization"
        assert out.order.family == "authorization"

    def test_si_by_title_anchor(self, si_bytes):
        out = parse_manifest(si_bytes)
        assert out.family == "si"

    def test_unknown_family_rejected(self, unknown_bytes):
        with pytest.raises(UnknownManifestFamilyError) as ei:
            parse_manifest(unknown_bytes)
        assert ei.value.code == "unknown_manifest_family"
        assert ei.value.http_status == 400
        assert "sheets" in ei.value.details


class TestMultiBlNo:
    """v1.8 多提单号候选收集：全工作簿所有舱单 sheet 的提单号标签值。"""

    def test_two_sheets_two_bl_nos_collected(self, si_bytes):
        """双舱单 sheet 各一提单号 → 收集 2 个。"""
        wb = load_workbook(io.BytesIO(si_bytes))
        ws2 = wb.create_sheet("Shipping Instruction 2")
        ws2["C1"] = "Booking / BL Number : SITGBAQI005920"
        buf = io.BytesIO()
        wb.save(buf)
        out = parse_manifest(buf.getvalue())
        assert out.bl_nos == ["SITGBAYP006017", "SITGBAQI005920"]

    def test_same_bl_no_duplicated_deduplicated(self, si_bytes):
        """同一提单号在多个 sheet 重复出现 → 去重后 1 个（不算多票）。"""
        wb = load_workbook(io.BytesIO(si_bytes))
        ws2 = wb.create_sheet("Shipping Instruction 2")
        ws2["C1"] = "Booking / BL Number : SITGBAYP006017"
        buf = io.BytesIO()
        wb.save(buf)
        out = parse_manifest(buf.getvalue())
        assert out.bl_nos == ["SITGBAYP006017"]

    def test_slash_double_no_collected_as_two(self, auth_bytes):
        """MBL NO 斜杠双号（参考号/船司号）→ 按两个提单号拆分收集（v1.8 用户拍板）。"""
        wb = load_workbook(io.BytesIO(auth_bytes))
        wb.active.cell(3, 8, "SIT0807BASH591/SITGBASH006434")
        buf = io.BytesIO()
        wb.save(buf)
        out = parse_manifest(buf.getvalue())
        assert out.bl_nos == ["SIT0807BASH591", "SITGBASH006434"]

    def test_single_bl_no_collected(self, auth_bytes):
        """单提单号（无斜杠）→ 收集 1 个。"""
        out = parse_manifest(auth_bytes)
        assert out.bl_nos == ["SITGBASH006434"]

    def test_hbl_no_not_counted(self, auth_bytes):
        """HBL NO 分单不参与主提单号计数（MBL+HBL 并存不算多提单号）。"""
        wb = load_workbook(io.BytesIO(auth_bytes))
        wb.active.cell(4, 8, "HBL99999999")
        buf = io.BytesIO()
        wb.save(buf)
        out = parse_manifest(buf.getvalue())
        assert out.bl_nos == ["SITGBASH006434"]

    def test_single_sheet_single_bl_no(self, si_bytes):
        out = parse_manifest(si_bytes)
        assert out.bl_nos == ["SITGBAYP006017"]


class TestBadFile:
    """坏文件：空文件 / 非 xlsx / 损坏 zip。"""

    def test_empty_file(self):
        with pytest.raises(BadRequestError) as ei:
            parse_manifest(b"")
        assert ei.value.code == "empty_file"

    def test_not_xlsx_magic(self):
        with pytest.raises(BadRequestError) as ei:
            parse_manifest(b"\xd0\xcf\x11\xe0fake-xls-content")  # OLE2 头
        assert ei.value.code == "file_format_mismatch"

    def test_corrupted_zip(self):
        with pytest.raises(ConvertError):
            parse_manifest(b"PK\x03\x04" + b"corrupted-tail-not-a-zip")


class TestSplitPartyBlock:
    """三栏合并大格拆分：名称/地址/电话归位（TMS 三栏空值核查）。"""

    def test_si_v2_shipper_labeled_lines(self):
        from app.orders.manifest.parsing.parser import _split_party_block

        name, addr, tel = _split_party_block(
            " PT BINTAN CELLULAR INDONESIA                      \n"
            "Registered Address：KAWASAN EKONOMI KHUSUS (KEK) GALANG BATANG RT006 RW003.GUNUNG KIJANG, BINTAN\n"
            "VAT: 0197 0909 2122 4000  \n"
            "Tel:6285376250031                     PIC: HU JIANG   \n"
        )
        assert name == "PT BINTAN CELLULAR INDONESIA"
        assert addr == "KAWASAN EKONOMI KHUSUS (KEK) GALANG BATANG RT006 RW003.GUNUNG KIJANG, BINTAN"
        assert tel == "6285376250031"

    def test_si_v2_consignee_inline_phone_and_no(self):
        from app.orders.manifest.parsing.parser import _split_party_block

        name, addr, tel = _split_party_block(
            "Mianyang Yingfa Ruiyang New Energy Technology Co.,Ltd. No. 202 Liaoning Avenue, "
            "Mianyang City, Sichuan Province, China Guo daijun 18281968110\nguodaijun@yingfaruineng.com"
        )
        assert name == "Mianyang Yingfa Ruiyang New Energy Technology Co.,Ltd."
        assert addr.startswith("No. 202 Liaoning Avenue") and addr.endswith("China Guo daijun")
        assert tel == "18281968110"  # 11 位手机号形态；残留联系人名进地址（不猜切）

    def test_si_v2_notify_add_prefix(self):
        from app.orders.manifest.parsing.parser import _split_party_block

        name, addr, tel = _split_party_block(
            "NINGBO HETIAN SUPPLY CHAIN MANAGEMENT CO.,LTD.\n"
            "ADD:NO. ROOM 16-9, NO. 565, JINGJIA ROAD, YINZHOU DISTRICT, NINGBO CITY"
        )
        assert name == "NINGBO HETIAN SUPPLY CHAIN MANAGEMENT CO.,LTD."
        assert addr == "NO. ROOM 16-9, NO. 565, JINGJIA ROAD, YINZHOU DISTRICT, NINGBO CITY"
        assert tel is None

    def test_authorization_consignee_phone_label(self):
        from app.orders.manifest.parsing.parser import _split_party_block

        name, addr, tel = _split_party_block(
            "INTERPLEX (SUZHOU) PRECISION ENGINEERING LTD. No. 36, Xing Ming Street, Suzhou Industrial Park, "
            "PRChina Postal Code: 215021 Receiver: Guoqiang Gu guoqiang.gu@cn.interplex.com Phone number: +86 13915509846"
        )
        assert name == "INTERPLEX (SUZHOU) PRECISION ENGINEERING LTD."
        assert addr == "No. 36, Xing Ming Street, Suzhou Industrial Park, PRChina Postal Code: 215021"
        assert tel == "+86 13915509846"  # Receiver/邮箱段截断丢弃，电话标签取号

    def test_si_v1_gap_layout(self):
        """SI v1 排版：名称␣␣␣␣地址（≥4 空隙 + 后半含数字）。"""
        from app.orders.manifest.parsing.parser import _split_party_block

        name, addr, tel = _split_party_block(
            "CV VANYA COCO UNIVERSAL                                                                                                                                                                                                                                                                                                       "
            "MUTIARA HIJAU BLOK A1 NO 01 RT. 001 RW.006 KEL  DURIANGKANG KEC SEI BEDUK KOTA BATAM KEPULAUAN RIAU 29437, INDONESIA"
        )
        assert name == "CV VANYA COCO UNIVERSAL"
        assert addr.startswith("MUTIARA HIJAU BLOK A1")
        assert tel is None

    def test_no_marker_stays_whole(self):
        """无任何标记（托书发货人格）→ 整块进名称，不猜切分。"""
        from app.orders.manifest.parsing.parser import _split_party_block

        blob = "PT ENNOVI METAL CAST ENGINEERING SERVICES BATAM TUNAS INDUSTRIAL ESTATE (BIZ PARK) BLOK 9B"
        name, addr, tel = _split_party_block(blob)
        assert (name, addr, tel) == (blob, None, None)

    def test_empty(self):
        from app.orders.manifest.parsing.parser import _split_party_block

        assert _split_party_block("") == (None, None, None)
        assert _split_party_block(None) == (None, None, None)


class TestAuthorization:
    """托书家族：label 定位 + 合并大格区块 + 括号箱型箱号。"""

    def test_full_fields(self, auth_bytes):
        o = parse_manifest(auth_bytes).order
        # 双号提单号取斜杠后段（船司 MBL；TMS bOrderNum 不支持斜杠）
        assert o.bl_no == "SITGBASH006434"
        # 船名航次拆分（去日期括号；末尾 2617N 为航次）
        assert o.vessel == "SITC HAODE"
        assert o.voyage == "2617N"
        assert o.etd_text == "SITC HAODE 2617N (ETD: 15/08)"
        # 港口：POL/目的地原文；Port of Discharge 无值 → None（不取同行右侧其它 label）
        assert o.pol == "BATAM, INDONESIA"
        assert o.pod is None
        assert o.final_destination == "SHANGHAI, CHINA"
        # 三栏：区块下方合并大格；SAME AS CONSIGNEE 展开（名/地址/电话连带）
        assert o.shipper_name == "PT ENNOVI METAL CAST ENGINEERING SERVICE"
        assert o.shipper_address is None and o.shipper_tel is None
        assert o.consignee_name == "INTERPLEX (SUZHOU) PRECISION ENGINEERING"
        assert o.consignee_address is None and o.consignee_tel is None
        assert o.notifier_name == o.consignee_name
        assert o.notifier_address == o.consignee_address
        assert o.notifier_tel == o.consignee_tel
        # 箱型箱量 + 箱号封号（括号内斜杠拆）
        assert [(g.b_type, g.ctn_num) for g in o.box_groups] == [("40HQ", 1)]
        assert [(c.container_no, c.seal_no) for c in o.containers] == [
            ("FFAU7731669", "SITR853037")
        ]
        # 件数（前导数字）/品名/毛重
        assert o.pieces == 14
        assert o.goods_ename == "8480411000 Die casting mold (used)"
        assert o.gross_weight == 16000.0
        # 唛头区为空 → None（不误取 Quantity 列值）
        assert o.shipping_mark is None
        # 必填齐全
        assert o.missing_fields == []

    def test_missing_bl_marks(self, auth_bytes):
        """无 MBL NO 值 → bl_no 缺失登记（合成：删除 MBL 值格）。"""
        import io

        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(auth_bytes))
        wb.active.cell(3, 8).value = None  # MBL 值清空（value=None 赋值式才生效）
        buf = io.BytesIO()
        wb.save(buf)
        o = parse_manifest(buf.getvalue()).order
        assert o.bl_no is None
        assert "bl_no" in o.missing_fields


class TestSI:
    """SI 家族：label:value 同格剥离 + 明细表内容模式识别。"""

    def test_variant1_fields(self, si_bytes):
        """变体 1：& 同格箱号封条；TOTAL 无独立合计来源（明细兜底）；无箱型 → missing。"""
        o = parse_manifest(si_bytes).order
        assert o.bl_no == "SITGBAYP006017"  # 同格冒号剥离
        assert o.vessel == "SITC INCHON"
        assert o.voyage == "2618N"
        assert o.pol == "BATAM BATU AMPAR, INDONESIA"
        assert o.pod == "YANGPUGANG,CHINA"
        assert o.hs_code == "12030000"
        assert o.shipper_name == "CV VANYA COCO UNIVERSAL"
        assert o.consignee_name == "HAI NAN YINGHE TRADING CO.,LTD."
        assert o.notifier_name == o.consignee_name  # SAME AS CONSIGNEE
        assert o.goods_ename == "DRIED COCONUT SKIN"
        # 明细：& 同格拆箱号封条
        assert [(c.container_no, c.seal_no) for c in o.containers] == [
            ("FFAU8106619", "SITR825483")
        ]
        # TOTAL 行合计优先
        assert o.net_weight == 24326.0
        assert o.gross_weight == 28026.0
        assert o.pieces == 875.0
        # 无箱型来源 → 必填缺失（不猜测）
        assert o.box_groups == []
        assert "box_groups" in o.missing_fields
        assert o.missing_reasons["box_groups"] == "原文未找到"

    def test_variant2_shifted_columns(self, si_shifted_bytes):
        """变体 2：封条占 Quantity 位（列位移）→ 数量走底部汇总；箱型从底部汇总取。"""
        o = parse_manifest(si_shifted_bytes).order
        assert o.bl_no == "SITGBAQI005920"
        assert o.vessel == "ORIENTAL BRIGHT"
        # 封条占位后仍按形态识别箱号/封条
        assert [(c.container_no, c.seal_no) for c in o.containers] == [
            ("TIIU6545430", "SITR814101"),
            ("HPCU5314463", "SITR814102"),
        ]
        # TOTAL 行合计（NW/GW/CBM 列身份正确归类，不受封条占位影响）
        assert o.net_weight == 34028.0
        assert o.gross_weight == 34864.0
        assert o.volume_cbm == 96.24384
        # 底部汇总：箱型箱量 + 件数（箱型格的 40 不污染件数）
        assert [(g.b_type, g.ctn_num) for g in o.box_groups] == [("40HQ", 2)]
        assert o.pieces == 770.0
        # 品名块剔除 INVOICE No./HS CODE 元数据行（TMS 限 100 字符，不硬截断）
        assert o.goods_ename == "183 carton"
        assert o.missing_fields == []

    def test_empty_hs_returns_none(self, si_shifted_bytes):
        """HS CODE label 无值 → None（不回退 label 自身文本）。"""
        o = parse_manifest(si_shifted_bytes).order
        assert o.hs_code is None

    def test_dirty_vgm_normalized(self, si_shifted_bytes):
        """脏 VGM（20.652.00 多点号）：数字清洗最后一个点为小数点。"""
        from app.orders.manifest.parsing.parser import _clean_number

        assert _clean_number("20.652.00") == 20652.0
        assert _clean_number("16,000") == 16000.0
        assert _clean_number(16000) == 16000.0
        assert _clean_number("abc") is None

    def test_missing_pol_marks(self, si_bytes):
        """POL 无值 → pol 缺失登记（起运港必填）。"""
        import io

        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(si_bytes))
        wb.active.cell(9, 4).value = None  # POL 值清空（value=None 赋值式才生效）
        buf = io.BytesIO()
        wb.save(buf)
        o = parse_manifest(buf.getvalue()).order
        assert o.pol is None
        assert "pol" in o.missing_fields


class TestRealGolden:
    """真实模版 golden（样本不入库，按存在性跳过）。"""

    def test_real_authorization(self, real_auth_bytes):
        o = parse_manifest(real_auth_bytes).order
        assert o.family == "authorization"
        assert o.bl_no == "SITGBASH006434"  # 双号取后段（TMS 不支持斜杠）
        assert (o.vessel, o.voyage) == ("SITC HAODE", "2617N")
        assert o.pol == "BATAM, INDONESIA"
        assert o.final_destination == "SHANGHAI, CHINA"
        assert o.shipper_name.startswith("PT ENNOVI")
        assert o.shipper_address is None  # 托书发货人格无标记 → 整块进名称（不猜切）
        assert o.shipper_tel is None
        assert o.consignee_name == "INTERPLEX (SUZHOU) PRECISION ENGINEERING LTD."
        assert o.consignee_address == (
            "No. 36, Xing Ming Street, Suzhou Industrial Park, Suzhou, Jiangsu Province, "
            "PRChina Postal Code: 215021"
        )
        assert o.consignee_tel == "+86 13915509846"
        assert o.notifier_name == o.consignee_name
        assert o.notifier_address == o.consignee_address
        assert o.notifier_tel == o.consignee_tel
        assert [(g.b_type, g.ctn_num) for g in o.box_groups] == [("40HQ", 1)]
        assert [(c.container_no, c.seal_no) for c in o.containers] == [
            ("FFAU7731669", "SITR853037")
        ]
        assert o.pieces == 14
        assert o.gross_weight == 16000.0
        assert o.missing_fields == []

    def test_real_si_v1(self, real_si_v1_bytes):
        o = parse_manifest(real_si_v1_bytes).order
        assert o.family == "si"
        assert o.bl_no == "SITGBAYP006017"
        assert (o.vessel, o.voyage) == ("SITC INCHON", "2618N")
        assert o.pol == "BATAM BATU AMPAR, INDONESIA"
        assert o.pod == "YANGPUGANG,CHINA"
        assert o.hs_code == "12030000"
        # 三栏拆分（SI v1 大空隙排版：名称␣␣␣␣地址）
        assert o.shipper_name == "CV VANYA COCO UNIVERSAL"
        assert o.shipper_address.startswith("MUTIARA HIJAU BLOK A1")
        assert o.consignee_name == "HAI NAN YINGHE TRADING CO.,LTD."
        assert o.consignee_address.startswith("NO .05B2,15 BUILDING")
        assert [(c.container_no, c.seal_no) for c in o.containers] == [
            ("FFAU8106619", "SITR825483")
        ]
        assert o.pieces == 875.0
        assert o.net_weight == 24326.0
        assert o.gross_weight == 28026.0
        assert o.box_groups == []
        assert "box_groups" in o.missing_fields

    def test_real_si_v2(self, real_si_v2_bytes):
        o = parse_manifest(real_si_v2_bytes).order
        assert o.family == "si"
        assert o.bl_no == "SITGBAQI005920"
        assert (o.vessel, o.voyage) == ("ORIENTAL BRIGHT", "2618N")
        assert o.pol == "BATAM, Indoneisa"  # 拼写保真
        assert o.pod == "QINZHOU, China"
        assert o.hs_code is None
        # 三栏拆分（名称/地址/电话归位）
        assert o.shipper_name == "PT BINTAN CELLULAR INDONESIA"
        assert o.shipper_address == (
            "KAWASAN EKONOMI KHUSUS (KEK) GALANG BATANG RT006 RW003.GUNUNG KIJANG, "
            "GUNUNG KIJANG, BINTAN, KEPULAUAN RIAU INDONESIA"
        )
        assert o.shipper_tel == "6285376250031"
        assert o.consignee_name == "Mianyang Yingfa Ruiyang New Energy Technology Co.,Ltd."
        assert o.consignee_tel == "18281968110"
        assert o.notifier_name.startswith("NINGBO HETIAN")
        assert o.notifier_address.startswith("NO. ROOM 16-9")
        assert o.notifier_tel is None
        assert len(o.containers) == 10
        assert o.containers[0].container_no == "TIIU6545430"
        assert o.containers[0].seal_no == "SITR814101"
        assert [(g.b_type, g.ctn_num) for g in o.box_groups] == [("40HQ", 10)]
        assert o.pieces == 385.0
        # 品名块剔除 INVOICE No./HS CODE 行后 24 字符（原整块 109 超出 TMS 100 上限）
        assert o.goods_ename == "183 carton\nsilicon wafer"
        assert o.net_weight == 165218.0
        assert o.gross_weight == 168981.0
        assert o.volume_cbm == pytest.approx(446.16232)
        assert o.missing_fields == []
