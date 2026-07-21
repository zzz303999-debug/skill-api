"""LLM 输出字段归一化。

LLM 有时返回中文字段名、不同命名风格、或结构差异。
本模块在 Pydantic 校验前把 LLM 的 raw dict 归一到 schema 定义的字段名。
"""

from __future__ import annotations

import re
from typing import Any

# 顶层字段：{LLM 可能输出的 key} → 正确的 schema key
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
    # customs_declaration_no
    "关单号": "customs_declaration_no",
    "报关单号": "customs_declaration_no",
    "海关报关单号": "customs_declaration_no",
    "customsDeclarationNo": "customs_declaration_no",
    # customer_ref
    "客户编号": "customer_ref",
    "客户参考号": "customer_ref",
    "客户订单号": "customer_ref",
    "customerRef": "customer_ref",
    "customer_reference": "customer_ref",
    # mbl_no
    "主提单号": "mbl_no",
    "主单号": "mbl_no",
    "提单号": "mbl_no",
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
    "transitPort": "transit_port",
    "transshipment_port": "transit_port",
    # terminal
    "港区": "terminal",
    "码头": "terminal",
    # etd
    "船期": "etd",
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
    "from": "sender",
    "FROM": "sender",
    # sender_contact
    "发货联系人": "sender_contact",
    "发件人": "sender_contact",
    "senderContact": "sender_contact",
    "sender_contact": "sender_contact",
    # remark
    "备注": "remark",
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

# containers[] 内部字段别名
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

# factory 内部字段别名
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

# order_mapping 内部字段别名
_ORDER_MAPPING_ALIASES: dict[str, str] = {
    "我司业务编号": "c_sn",
    "主提单号": "mbl_no",
    "子提单号": "hbl_no",
    "分提单号": "hbl_no",
    "托运人公司": "c_title",
    "工厂门点简称": "factory_name",
    "订单备注": "c_note",
}

# review_issue 内部字段别名
_REVIEW_ISSUE_ALIASES: dict[str, str] = {
    "问题代码": "code",
    "字段": "field",
    "描述": "message",
    "原文候选值": "source_values",
    "是否阻断": "blocking",
}

# source 内部字段别名
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


def _normalize_dict(data: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    """按别名映射表归一化 dict 的 keys。

    如果原始 key 已经是正确的 schema key，保留不动。
    如果有别名冲突（别名和正式 key 同时存在），正式 key 优先。
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        canonical = aliases.get(key, key)
        if canonical in result:
            # 正式 key 已存在，跳过别名值（除非正式 key 的值是 None）
            if result[canonical] is None:
                result[canonical] = value
        else:
            result[canonical] = value
    return result


def _normalize_container(item: dict[str, Any]) -> dict[str, Any]:
    result = _normalize_dict(item, _CONTAINER_ALIASES)
    container_type, container_qty = _split_container_type_qty(
        result.pop("container_type_qty", None)
    )
    if container_type and not result.get("type"):
        result["type"] = container_type
    if container_qty is not None and result.get("qty") is None:
        result["qty"] = container_qty

    for field, integral in (
        ("packages", True),
        ("gross_weight_kg", False),
        ("volume_cbm", False),
    ):
        cleaned, unit = _clean_number(result.get(field), integral=integral)
        result[field] = cleaned
        if field == "packages" and unit and not result.get("packages_unit"):
            result["packages_unit"] = unit
    return result


def _normalize_factory(data: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    if isinstance(data, dict):
        return _normalize_dict(data, _FACTORY_ALIASES)
    return data


def _normalize_source(data: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    if isinstance(data, dict):
        return _normalize_dict(data, _SOURCE_ALIASES)
    return data


def _split_vessel_voyage(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    if "/" in text:
        vessel, voyage = (part.strip() for part in text.rsplit("/", 1))
        voyage = re.sub(r"^V\.\s*", "", voyage, flags=re.IGNORECASE)
        return vessel or None, voyage or None
    match = re.fullmatch(r"(.+?)\s+V\.\s*([^\s]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip() or None, match.group(2).strip() or None
    return None, None


def _split_container_type_qty(value: Any) -> tuple[str | None, int | None]:
    if not isinstance(value, str):
        return None, None
    match = re.fullmatch(r"\s*(\d+)\s*[xX*×]\s*([A-Za-z0-9]+)\s*", value)
    if not match:
        return None, None
    return match.group(2).upper(), int(match.group(1))


def _clean_number(value: Any, *, integral: bool) -> tuple[Any, str | None]:
    if value is None or isinstance(value, (int, float)):
        return value, None
    if not isinstance(value, str):
        return value, None
    text = value.replace(",", "").strip()
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*([^\d\s].*)?", text)
    if not match:
        return value, None
    number = float(match.group(1))
    if integral:
        if not number.is_integer():
            return value, None
        cleaned: int | float = int(number)
    else:
        cleaned = number
    unit = match.group(2).strip().upper() if match.group(2) else None
    return cleaned, unit


def _merge_remark(data: dict[str, Any], label: str, value: Any) -> None:
    if value is None or value == "":
        return
    item = f"{label}：{value}"
    remark = data.get("remark")
    if isinstance(remark, str) and remark.strip():
        if item not in remark:
            data["remark"] = f"{remark.strip()}；{item}"
    else:
        data["remark"] = item


def normalize_llm_output(data: dict[str, Any]) -> dict[str, Any]:
    """将 LLM 输出的 raw dict 归一化为 TuoshuOutput 期望的字段名。

    在 Pydantic model_validate 之前调用。
    """
    # 顶层字段归一
    result = _normalize_dict(data, _TOP_LEVEL_ALIASES)

    # doc_type 枚举值中文→英文
    _DOC_TYPE_CN_TO_EN = {
        "做箱通知": "PACKING_NOTICE",
        "运输委托书": "TRANSPORT_ORDER",
        "派车托书": "TRUCKING_ORDER",
        "订舱托书": "BOOKING_NOTE",
        "未知": "UNKNOWN",
    }
    doc_type_val = result.get("doc_type")
    if isinstance(doc_type_val, str) and doc_type_val in _DOC_TYPE_CN_TO_EN:
        result["doc_type"] = _DOC_TYPE_CN_TO_EN[doc_type_val]

    combined_vessel_voyage = result.pop("vessel_voyage", None)
    vessel, voyage = _split_vessel_voyage(combined_vessel_voyage)
    if vessel and not result.get("vessel"):
        result["vessel"] = vessel
    if voyage and not result.get("voyage"):
        result["voyage"] = voyage

    container_type, container_qty = _split_container_type_qty(
        result.pop("container_type_qty", None)
    )
    if container_type and not result.get("type"):
        result["type"] = container_type
    if container_qty is not None and result.get("qty") is None:
        result["qty"] = container_qty

    _merge_remark(result, "开港时间", result.pop("port_opening_time", None))

    # containers 内部归一
    containers = result.get("containers")
    if isinstance(containers, list):
        result["containers"] = [
            _normalize_container(c) if isinstance(c, dict) else c
            for c in containers
        ]
    elif isinstance(containers, dict):
        # LLM 有时只返回单个 container dict 而不是 list
        result["containers"] = [_normalize_container(containers)]

    # factory 内部归一
    result["factory"] = _normalize_factory(result.get("factory"))
    factory_fields = {
        "address": result.pop("factory_address", None),
        "contact": result.pop("factory_contact", None),
        "phone": result.pop("factory_phone", None),
    }
    if any(value not in (None, "") for value in factory_fields.values()):
        factory = result.get("factory")
        if not isinstance(factory, dict):
            factory = {}
            result["factory"] = factory
        for key, value in factory_fields.items():
            if value not in (None, "") and not factory.get(key):
                factory[key] = value

    # order_mapping 内部归一
    order_mapping = result.get("order_mapping")
    if isinstance(order_mapping, dict):
        result["order_mapping"] = _normalize_dict(order_mapping, _ORDER_MAPPING_ALIASES)

    # review_issues 内部归一
    review_issues = result.get("review_issues")
    if isinstance(review_issues, list):
        cleaned_issues = []
        for issue in review_issues:
            if isinstance(issue, dict):
                issue = _normalize_dict(issue, _REVIEW_ISSUE_ALIASES)
                # source_values 必须全是字符串，LLM 可能输出数字
                sv = issue.get("source_values")
                if isinstance(sv, list):
                    issue["source_values"] = [str(v) if not isinstance(v, str) else v for v in sv]
                elif sv is not None and not isinstance(sv, list):
                    issue["source_values"] = [str(sv)]
            cleaned_issues.append(issue)
        result["review_issues"] = cleaned_issues

    # source 内部归一
    result["source"] = _normalize_source(result.get("source"))

    # 处理容器字段泄漏到顶层的问题
    # 这些字段只应该出现在 containers[] 内部，不应出现在顶层
    _CONTAINER_ONLY_KEYS = {
        "type", "qty", "container_no", "seal_no",
        "packages", "packages_unit", "gross_weight_kg", "volume_cbm",
    }
    # 先把顶层的中文容器 key 也归一化（解决 LLM 把容器字段写顶层的问题）
    result = _normalize_dict(result, _CONTAINER_ALIASES)
    leaked = {k: result.pop(k) for k in _CONTAINER_ONLY_KEYS if k in result}
    if leaked:
        leaked = _normalize_container(leaked)
    if leaked and not result.get("containers"):
        # containers 为空，用泄漏字段构建一个 container 项
        result["containers"] = [leaked]
    elif leaked and isinstance(result.get("containers"), list) and result["containers"]:
        first = result["containers"][0]
        if isinstance(first, dict):
            for key, value in leaked.items():
                if first.get(key) is None:
                    first[key] = value

    return result
