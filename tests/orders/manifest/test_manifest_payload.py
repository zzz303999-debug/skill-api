"""payload 层测试：展示/提交双口径、类型、常量、orderInfos 组装、运价恒 0。"""

from __future__ import annotations

from app.orders.manifest import parse_manifest
from app.orders.manifest.payload import build_order_data, to_submit_payload


class TestBuildOrderData:
    """展示口径：必填缺失置 None；常量/类型/默认值冻结。"""

    def test_full_authorization(self, auth_bytes):
        """托书全字段：常量、港口默认值、orderInfos 组装、类型口径。"""
        o = parse_manifest(auth_bytes).order
        data = build_order_data(o)
        # 常量（v1.0 冻结）
        assert data["type"] == 2
        assert data["ieFlag"] == 1
        # 单头
        assert data["bOrderNum"] == "SITGBASH006434"  # 双号取后段（TMS 无斜杠）
        assert data["bStartPort"] == "BATAM, INDONESIA"  # POL 原文自由输入
        assert data["bEndPort"] == "SHANGHAI, CHINA"  # Final Destination 优先
        assert data["bMidPort"] == ""
        assert data["bPort"] == ""
        assert data["bCompany"] == ""
        assert data["bShipName"] == "SITC HAODE"
        assert data["bShipNum"] == "2617N"
        assert data["bTotleNum"] == "14"
        assert data["bTotleWeight"] == "16000"
        # 客户/三栏/费用（v1 恒空）
        assert data["cTitle"] == "" and data["cId"] == ""
        assert data["shipperName"] == "PT ENNOVI METAL CAST ENGINEERING SERVICE"
        assert data["shipperTel"] is None and data["shipperAddress"] is None  # 合成模版无电话/地址
        assert data["consigneeName"] == "INTERPLEX (SUZHOU) PRECISION ENGINEERING"
        assert data["notifierName"] == data["consigneeName"]
        assert data["yfFees"] == [] and data["ysFees"] == []
        # orderInfos：每箱型一组；unitPrice 字符串 / totalPrice 数字 / bDate 留空；
        # 明细行件毛体：单箱型 → 组合计=单头件毛体（volume 缺失 → None 展示/提交空串）
        assert len(data["orderInfos"]) == 1
        info = data["orderInfos"][0]
        assert info == {
            "bType": "40HQ",
            "ctnNum": 1,
            "unitPrice": "0",  # 模版无运价字段，恒 0（TMS 误设必填）
            "totalPrice": 0,
            "bDate": None,
            "bTotleNum": "14",
            "bTotleWeight": "16000",
            "bTotleBulk": None,
        }
        # 门点地址 ← 三栏地址（无则 None 展示、提交空串，不猜不编）
        assert data["shipperFactoryAddressMsg"] is None
        assert data["consigneeFactoryAddressMsg"] is None

    def test_multi_box_types_one_entry_each(self, si_shifted_bytes):
        """多箱型 → orderInfos 每组一条（ctnNum 聚合、总价仍恒 0）。"""
        o = parse_manifest(si_shifted_bytes).order
        data = build_order_data(o)
        assert len(data["orderInfos"]) == 1
        info = data["orderInfos"][0]
        assert info["ctnNum"] == 2
        assert info["unitPrice"] == "0"
        assert info["totalPrice"] == 0

    def test_multi_groups_detail_totals_left_empty(self):
        """多箱型：明细行无箱型归属、件毛体无法按型拆 → 留空（不猜不编）。"""
        from app.orders.manifest.schema import ManifestBoxGroup, ManifestOrder

        o = ManifestOrder(
            family="si",
            box_groups=[ManifestBoxGroup(b_type="20GP", ctn_num=2), ManifestBoxGroup(b_type="40HQ", ctn_num=3)],
            pieces=100,
            gross_weight=1000,
            volume_cbm=10,
        )
        for info in build_order_data(o)["orderInfos"]:
            assert info["bTotleNum"] == "" and info["bTotleWeight"] == "" and info["bTotleBulk"] == ""
        # 单头件毛体照常提交
        data = build_order_data(o)
        assert data["bTotleNum"] == "100" and data["bTotleWeight"] == "1000"

    def test_factory_address_from_party(self, si_bytes):
        """门点地址 = 三栏拆分地址（双写三栏地址与门点地址，数据同源）。"""
        o = parse_manifest(si_bytes).order
        o.consignee_address = "No. 1 Test Road"
        data = build_order_data(o)
        assert data["consigneeFactoryAddressMsg"] == "No. 1 Test Road"
        submit = to_submit_payload(data)
        assert submit["consigneeFactoryAddressMsg"] == "No. 1 Test Road"
        assert submit["shipperFactoryAddressMsg"] == ""  # 无地址 → 空串

    def test_bendport_fallback_to_pod(self, si_bytes):
        """SI 无 Final Destination → bEndPort = POD 原文。"""
        o = parse_manifest(si_bytes).order
        assert o.final_destination is None
        assert build_order_data(o)["bEndPort"] == "YANGPUGANG,CHINA"


class TestToSubmitPayload:
    """提交口径：None → 空串；数字键保持；不改动展示版。"""

    def test_none_to_empty_string(self, auth_bytes):
        """必填齐全：展示版 None（bDate）提交变空串；数字键保持。"""
        o = parse_manifest(auth_bytes).order
        display = build_order_data(o)
        submit = to_submit_payload(display)
        assert submit is not display  # 原样复制不改动展示版
        assert display["orderInfos"][0]["bDate"] is None
        assert submit["orderInfos"][0]["bDate"] == ""
        assert submit["type"] == 2
        assert submit["orderInfos"][0]["ctnNum"] == 1
        assert submit["orderInfos"][0]["totalPrice"] == 0
        assert submit["orderInfos"][0]["unitPrice"] == "0"
        # 三栏电话/地址：展示 None → 提交空串
        assert submit["shipperTel"] == "" and submit["shipperAddress"] == ""
        assert submit["consigneeTel"] == "" and submit["notifierAddress"] == ""

    def test_missing_required_becomes_empty(self, auth_bytes):
        """必填缺失（防御性兜底）：bOrderNum/bStartPort None → 空串。"""
        import io

        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(auth_bytes))
        wb.active.cell(3, 8).value = None  # 清 MBL
        wb.active.cell(14, 4).value = None  # 清 POL
        buf = io.BytesIO()
        wb.save(buf)
        o = parse_manifest(buf.getvalue()).order
        submit = to_submit_payload(build_order_data(o))
        assert submit["bOrderNum"] == ""
        assert submit["bStartPort"] == ""
