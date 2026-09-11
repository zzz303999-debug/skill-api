"""将最终英文 JSON 转成中文 key 的纯展示适配器。

只能接收已通过 TuoshuOutput 校验的 JSON。API data 始终返回英文 schema；
本模块不得用于模型输出、数据入库或替代 API data。
"""

from __future__ import annotations

from .schema import TuoshuOutput

# 英文 key → 中文 key 映射表（严格按 schema.md 的"说明"列）
EN_TO_CN_TOP: dict[str, str] = {
    "doc_type": "文档类型",
    "internal_ref": "我司业务编号",
    "customs_declaration_no": "报关单号",
    "customer_ref": "客户编号",
    "mbl_no": "提单号",
    "hbl_no": "子提单号",
    "vessel": "船名",
    "voyage": "航次",
    "carrier": "船公司",
    "pol": "起运港",
    "pod": "目的港",
    "transit_port": "中转港",
    "terminal": "港区",
    "etd": "船期",
    "si_cutoff": "截单时间",
    "customs_cutoff": "截关时间",
    "loading_time": "做箱时间",
    "customer": "客户",
    "shipper_company": "托运人公司",
    "shipper_agent": "委托公司",
    "recipient": "收件方",
    "doc_date": "日期",
    "sender": "发货方",
    "sender_contact": "发货联系人",
    "remark": "备注",
    "containers": "集装箱",
    "factory": "工厂",
    "order_mapping": "订单映射",
    "review_issues": "复核问题",
    "ready_for_order": "可下单",
    "source": "来源",
    "raw_text_snippet": "原文摘要",
}

EN_TO_CN_CONTAINER: dict[str, str] = {
    "type": "箱型",
    "qty": "箱量",
    "container_no": "箱号",
    "seal_no": "铅封号",
    "packages": "件数",
    "packages_unit": "件数单位",
    "gross_weight_kg": "毛重",
    "volume_cbm": "体积",
    "po_no": "PO号",
    "mbl_no": "提单号",
    "remark": "备注",
}

EN_TO_CN_FACTORY: dict[str, str] = {
    "name": "工厂名称",
    "address": "地址",
    "contact": "联系人",
    "phone": "电话",
}

EN_TO_CN_ORDER_MAPPING: dict[str, str] = {
    "c_sn": "我司业务编号",
    "mbl_no": "主提单号",
    "hbl_no": "子提单号",
    "c_title": "托运人公司",
    "factory_name": "工厂门点简称",
    "c_note": "订单备注",
}

EN_TO_CN_REVIEW_ISSUE: dict[str, str] = {
    "code": "问题代码",
    "field": "字段",
    "message": "描述",
    "source_values": "原文候选值",
    "blocking": "是否阻断",
}

EN_TO_CN_SOURCE: dict[str, str] = {
    "file": "文件名",
    "doc_format": "格式",
    "template_hint": "模板",
    "extracted_at": "抽取时间",
}

# doc_type 枚举值中文映射
DOC_TYPE_CN: dict[str, str] = {
    "PACKING_NOTICE": "做箱通知",
    "TRANSPORT_ORDER": "运输委托书",
    "TRUCKING_ORDER": "派车托书",
    "BOOKING_NOTE": "订舱托书",
    "UNKNOWN": "未知",
}


def _convert_keys(data: dict, mapping: dict[str, str]) -> dict:
    """将 dict 的 key 按映射表转换为中文。"""
    result = {}
    for en_key, value in data.items():
        cn_key = mapping.get(en_key, en_key)  # 找不到映射则保留原 key
        result[cn_key] = value
    return result


def _convert_list(items: list, mapping: dict[str, str]) -> list:
    """将 list[dict] 的每个元素 key 转换。"""
    return [
        _convert_keys(item, mapping) if isinstance(item, dict) else item
        for item in items
    ]


def to_chinese(data: dict | TuoshuOutput) -> dict:
    """从最终英文 JSON 确定性渲染中文 key 展示对象。"""
    validated = data if isinstance(data, TuoshuOutput) else TuoshuOutput.model_validate(data)
    data = validated.model_dump()
    result = _convert_keys(data, EN_TO_CN_TOP)

    # carrier 保留接口返回的完整原值，展示层明确标注其未经中文名称映射。
    carrier = data.get("carrier")
    if isinstance(carrier, str) and carrier:
        result["船公司"] = f"{carrier}（接口原始值）"

    # doc_type 枚举值也转中文
    doc_type_en = data.get("doc_type")
    if doc_type_en in DOC_TYPE_CN:
        result["文档类型"] = DOC_TYPE_CN[doc_type_en]

    # containers
    containers = data.get("containers")
    if isinstance(containers, list):
        result["集装箱"] = _convert_list(containers, EN_TO_CN_CONTAINER)

    # factory
    factory = data.get("factory")
    if isinstance(factory, dict):
        result["工厂"] = _convert_keys(factory, EN_TO_CN_FACTORY)

    # order_mapping
    order_mapping = data.get("order_mapping")
    if isinstance(order_mapping, dict):
        result["订单映射"] = _convert_keys(order_mapping, EN_TO_CN_ORDER_MAPPING)

    # review_issues
    review_issues = data.get("review_issues")
    if isinstance(review_issues, list):
        result["复核问题"] = _convert_list(review_issues, EN_TO_CN_REVIEW_ISSUE)

    # source
    source = data.get("source")
    if isinstance(source, dict):
        result["来源"] = _convert_keys(source, EN_TO_CN_SOURCE)

    return result
