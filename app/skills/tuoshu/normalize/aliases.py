"""tuoshu 归一子包：aliases 簇（P4-1 自 normalizer.py 拆分，行为零变更）。"""

from __future__ import annotations

from app.core.text_normalize import STANDARD_CONTAINER_LENGTHS, STANDARD_CONTAINER_SUFFIXES

KNOWN_CONTAINER_TYPES = frozenset(
    {
        "20GP",
        "20DV",
        "20DC",
        "20GENERAL",
        "40GP",
        "40DV",
        "40DC",
        "40HC",
        "40HQ",
        "40H",
        "45HC",
        "45HQ",
        "20RF",
        "20REF",
        "40RF",
        "40REF",
        "40RH",
        "20OT",
        "20OPENTOP",
        "40OT",
        "40OPENTOP",
        "20FR",
        "20FLAT",
        "40FR",
        "40FLAT",
        "20TK",
        "20TANK",
    }
    | {
        f"{length}{suffix}"
        for length in STANDARD_CONTAINER_LENGTHS
        for suffix in STANDARD_CONTAINER_SUFFIXES
    }
)


_TOP_LEVEL_ALIASES: dict[str, str] = {
    # doc_type
    "文档类型": "doc_type",
    "document_type": "doc_type",
    "docType": "doc_type",
    "type": "doc_type",
    # internal_ref
    "订舱号": "internal_ref",
    "订舱编号": "internal_ref",
    "业务编号": "internal_ref",
    "我司编号": "internal_ref",
    "我司业务编号": "internal_ref",
    "育海编号": "internal_ref",
    "倍联业务编号": "internal_ref",
    "internalRef": "internal_ref",
    "c_sn": "internal_ref",
    "bookingNo": "internal_ref",
    "booking_number": "internal_ref",
    "booking_no": "internal_ref",
    # customs_declaration_no（报关单号不是提单号；关单号即提单号归 mbl_no）
    "报关单号": "customs_declaration_no",
    "报关号": "customs_declaration_no",
    "海关报关单号": "customs_declaration_no",
    "customsDeclarationNo": "customs_declaration_no",
    # customer_ref
    "客户编号": "customer_ref",
    "客户参考号": "customer_ref",
    "客户订单号": "customer_ref",
    "customerRef": "customer_ref",
    "customer_reference": "customer_ref",
    # mbl_no（关单号即提单号）
    "主提单号": "mbl_no",
    "主单号": "mbl_no",
    "提单号": "mbl_no",
    "关单号": "mbl_no",
    "mblNo": "mbl_no",
    "master_bl": "mbl_no",
    "master_bl_no": "mbl_no",
    # combined vessel/voyage
    "船名航次": "vessel_voyage",
    "船名/航次": "vessel_voyage",
    # hbl_no
    "子提单号": "hbl_no",
    "子单号": "hbl_no",
    "分提单号": "hbl_no",
    "分单号": "hbl_no",
    "hblNo": "hbl_no",
    "house_bl": "hbl_no",
    "house_bl_no": "hbl_no",
    # vessel
    "船名": "vessel",
    "vessel_name": "vessel",
    "vesselName": "vessel",
    # voyage
    "航次": "voyage",
    "voyage_no": "voyage",
    "voyageNo": "voyage",
    # carrier
    "承运人": "carrier",
    "船公司": "carrier",
    "船 公 司": "carrier",
    "船东": "carrier",
    "carrier_code": "carrier",
    "carrierCode": "carrier",
    # pol
    "起运港": "pol",
    "装货港": "pol",
    "port_of_loading": "pol",
    # pod
    "目的港": "pod",
    "卸货港": "pod",
    "目的地": "pod",
    "port_of_discharge": "pod",
    "destination": "pod",
    # transit_port
    "中转港": "transit_port",
    "中转港（卸港）": "transit_port",
    "中转港(卸港)": "transit_port",
    "中转港代码": "transit_port",
    "卸港": "transit_port",
    "transitPort": "transit_port",
    "transshipment_port": "transit_port",
    # terminal
    "港区": "terminal",
    "码头": "terminal",
    # etd
    "船期": "etd",
    "船 期": "etd",
    "开航日": "etd",
    "开航时间": "etd",
    "开船时间": "etd",
    "预计开航": "etd",
    "sailing_date": "etd",
    # opening time is not ETD
    "开港时间": "port_opening_time",
    "开港日期": "port_opening_time",
    # si_cutoff
    "截单时间": "si_cutoff",
    "截SI": "si_cutoff",
    "si_cut_off": "si_cutoff",
    "siCutoff": "si_cutoff",
    # customs_cutoff
    "截关": "customs_cutoff",
    "截关时间": "customs_cutoff",
    "customs_cut_off": "customs_cutoff",
    "customsCutoff": "customs_cutoff",
    # loading_time
    "做箱时间": "loading_time",
    "做箱日期": "loading_time",
    "装箱时间": "loading_time",
    "装箱日期": "loading_time",
    "拆装箱日期": "loading_time",
    "loadingTime": "loading_time",
    "loading_date": "loading_time",
    "stuffing_date": "loading_time",
    # containers
    "箱信息": "containers",
    "集装箱": "containers",
    "container_list": "containers",
    "containerList": "containers",
    "container": "containers",
    "箱型箱量": "container_type_qty",
    # factory
    "工厂": "factory",
    "工厂信息": "factory",
    "做箱工厂": "factory",
    "factory_info": "factory",
    "门点地址": "factory_address",
    "工厂联系人": "factory_contact",
    "工厂电话": "factory_phone",
    # customer
    "客户": "customer",
    "客户名称": "customer",
    "客户简称": "customer",
    "customerName": "customer",
    "customer_name": "customer",
    # shipper_company
    "托运人公司": "shipper_company",
    "托运人": "shipper_company",
    "发货人公司": "shipper_company",
    "发货公司": "shipper_company",
    "shipperCompany": "shipper_company",
    "shipper": "shipper_company",
    # shipper_agent
    "发件方": "shipper_agent",
    "我方公司": "shipper_agent",
    "货代": "shipper_agent",
    "shipperAgent": "shipper_agent",
    # recipient
    "收件方": "recipient",
    "收件人": "recipient",
    "致": "recipient",
    "ATTN": "recipient",
    "attn": "recipient",
    "to": "recipient",
    "TO": "recipient",
    # doc_date
    "日期": "doc_date",
    "date": "doc_date",
    "DATE": "doc_date",
    "文档日期": "doc_date",
    # sender
    "发货方": "sender",
    "发货人": "sender",
    # sender_contact
    "发货联系人": "sender_contact",
    "发件人": "sender_contact",
    "from": "sender_contact",
    "FROM": "sender_contact",
    "FM": "sender_contact",
    "fm": "sender_contact",
    "senderContact": "sender_contact",
    "sender_contact": "sender_contact",
    # remark
    "备注": "remark",
    "要求进港时间": "remark",
    "要求进港": "remark",
    "注意事项": "remark",
    "remarks": "remark",
    "note": "remark",
    "notes": "remark",
    # order_mapping
    "订单映射": "order_mapping",
    # review_issues
    "复核问题": "review_issues",
    # ready_for_order
    "可下单": "ready_for_order",
    # shipper_agent
    "委托公司": "shipper_agent",
    # source
    "来源": "source",
    # raw_text_snippet
    "原文摘要": "raw_text_snippet",
    "rawTextSnippet": "raw_text_snippet",
    "raw_text": "raw_text_snippet",
}


_CONTAINER_ALIASES: dict[str, str] = {
    "箱型": "type",
    "container_type": "type",
    "containerType": "type",
    "箱型箱量": "container_type_qty",
    "箱量": "qty",
    "数量": "qty",
    "quantity": "qty",
    "箱号": "container_no",
    "集装箱号": "container_no",
    "containerNo": "container_no",
    "container_number": "container_no",
    "铅封号": "seal_no",
    "封号": "seal_no",
    "sealNo": "seal_no",
    "seal_number": "seal_no",
    "件数": "packages",
    "件": "packages",
    "件数单位": "packages_unit",
    "packagesUnit": "packages_unit",
    "毛重": "gross_weight_kg",
    "重量": "gross_weight_kg",
    "grossWeight": "gross_weight_kg",
    "gross_weight": "gross_weight_kg",
    "weight_kg": "gross_weight_kg",
    "体积": "volume_cbm",
    "volumeCbm": "volume_cbm",
    "volume": "volume_cbm",
    "measurement": "volume_cbm",
    "po号": "po_no",
    "PO号": "po_no",
    "poNo": "po_no",
    "po_number": "po_no",
    "提单号": "mbl_no",
    "mblNo": "mbl_no",
    "备注": "remark",
    "remarks": "remark",
}


_FACTORY_ALIASES: dict[str, str] = {
    "工厂名称": "name",
    "客户名称": "name",
    "factory_name": "name",
    "factoryName": "name",
    "公司名": "name",
    "地址": "address",
    "做箱地址": "address",
    "提货地址": "address",
    "门点地址": "address",
    "factory_address": "address",
    "联系人": "contact",
    "工厂联系人": "contact",
    "contact_person": "contact",
    "contactPerson": "contact",
    "电话": "phone",
    "联系电话": "phone",
    "工厂电话": "phone",
    "手机": "phone",
    "phone_number": "phone",
    "tel": "phone",
}


_ORDER_MAPPING_ALIASES: dict[str, str] = {
    "我司业务编号": "c_sn",
    "主提单号": "mbl_no",
    "子提单号": "hbl_no",
    "分提单号": "hbl_no",
    "托运人公司": "c_title",
    "工厂门点简称": "factory_name",
    "订单备注": "c_note",
}


_REVIEW_ISSUE_ALIASES: dict[str, str] = {
    "问题代码": "code",
    "问题码": "code",
    "代码": "code",
    "issue_code": "code",
    "字段": "field",
    "问题字段": "field",
    "涉及字段": "field",
    "field_name": "field",
    "描述": "message",
    "问题": "message",
    "问题描述": "message",
    "原因": "message",
    "内容": "message",
    "建议": "message",
    "description": "message",
    "reason": "message",
    "原文候选值": "source_values",
    "候选值": "source_values",
    "原始值": "source_values",
    "values": "source_values",
    "是否阻断": "blocking",
    "阻断": "blocking",
    "需要阻断": "blocking",
    "is_blocking": "blocking",
}


_SOURCE_ALIASES: dict[str, str] = {
    "文件名": "file",
    "filename": "file",
    "fileName": "file",
    "格式": "doc_format",
    "docFormat": "doc_format",
    "format": "doc_format",
    "模板": "template_hint",
    "templateHint": "template_hint",
    "template": "template_hint",
    "抽取时间": "extracted_at",
    "extractedAt": "extracted_at",
}
