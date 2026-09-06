"""ManifestOrder → addBill JSON 请求体（《舱单导入接口文档》v1.0 §2.2 类型口径）。

接口怪癖全部封装在本层，不外泄：
- 静态超集键：全字段恒发（含空串），对齐抓包样例（cId 空串可创建，bId=11801 实证）；
- **展示侧 null / 提交侧空串**：build_order_data 产出预览回显（必填缺失键置
  None，不用 0/空串填猜测值）；to_submit_payload 转换为实际提交体（None→""）；
- 类型口径（2026-08-19 抓包）：type/ieFlag 恒 2/1（number）；ctnNum number；
  unitPrice **字符串**、totalPrice **数字**（均恒 0，见 _UNIT_PRICE）；bTotle*
  单头字符串；bDate v1 留空（模版 ETD 无年份，不猜测补全）；
- orderInfos：每箱型一组（bType/ctnNum/unitPrice/totalPrice + bTotle* 空串）；
- bEndPort ← Final Destination（缺失时 POD）原文（v1.0 冻结默认值）。

build_order_data 为纯函数，客户端与测试共用同一实现。
"""

from __future__ import annotations

from ..schema import ManifestOrder

# 常量（v1.0 冻结：抓包样例，用户确认恒定）
_TYPE = 2
_IE_FLAG = 1
# 每箱运价默认 0（2026-08-20 用户确认）：TMS 误将 unitPrice 设为必填，
# 舱单模版本身无运价字段 → 恒填 0 满足必填校验，不再查价格表
_UNIT_PRICE = 0


def _num_str(value: float | None) -> str | None:
    """数字 → 简洁字符串（14.0 → "14"、446.16232 → "446.16232"）；None 原样。"""
    if value is None:
        return None
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def build_order_data(order: ManifestOrder) -> dict:
    """ManifestOrder → addBill 请求体（展示口径：必填缺失键置 None）。

    每箱运价恒 0（模版无运价字段，TMS 误设必填；_UNIT_PRICE 冻结说明）。
    """
    # 明细行件毛体（2026-08-20 TMS 实测：明细行件毛体空 → 按组回填）：
    # 单箱型 → 组合计 = 单头件毛体；多箱型明细行无箱型归属、无法按型拆 → 留空
    single_group = len(order.box_groups) == 1
    order_infos: list[dict] = []
    for group in order.box_groups:
        order_infos.append(
            {
                "bType": group.b_type,
                "ctnNum": group.ctn_num,
                "unitPrice": str(_UNIT_PRICE),
                "totalPrice": _UNIT_PRICE * group.ctn_num,
                "bDate": None,  # v1 留空（模版 ETD 无年份，不猜测补全）
                "bTotleNum": _num_str(order.pieces) if single_group else "",
                "bTotleWeight": _num_str(order.gross_weight) if single_group else "",
                "bTotleBulk": _num_str(order.volume_cbm) if single_group else "",
            }
        )

    return {
        # 常量（v1.0 冻结）
        "type": _TYPE,
        "ieFlag": _IE_FLAG,
        # 单头业务字段（缺失 None 展示；提交侧转空串）
        "bOrderNum": order.bl_no,
        "bStartPort": order.pol,  # 起运港 = POL 原文（自由输入+必填）
        "bStartPortCode": "",
        "bEndPort": order.final_destination or order.pod,  # 冻结默认值
        "bEndPortCode": "",
        "bMidPort": "",
        "bMidPortCode": "",
        "bPort": "",  # 港区：模版无来源（v1.0 冻结空串）
        "bCompany": "",  # 船公司：模版无直接字段（v1.0 冻结空串）
        "bShipName": order.vessel,
        "bShipNum": order.voyage,
        "bRemark": "",
        "bTotleNum": _num_str(order.pieces),
        "bTotleWeight": _num_str(order.gross_weight),
        "bTotleBulk": _num_str(order.volume_cbm),
        # 客户（v1 不做匹配建档：空串实证可创建，bId=11801）
        "cId": "",
        "cName": "",
        "cPhone": "",
        "cSn": "",
        "cTitle": "",
        # 三栏（名称/地址/电话拆分归位，2026-08-20；缺失展示 None、提交转空串）
        "shipperName": order.shipper_name,
        "shipperTel": order.shipper_tel,
        "shipperAddress": order.shipper_address,
        "shipperFactoryId": "",
        "shipperFactoryName": "",
        "shipperFactoryContacts": "",
        # 门点地址 ← 发货人地址（2026-08-20 用户确认：模版有就录入；TMS 三栏地址
        # 与门点地址双写，数据同源）
        "shipperFactoryAddressMsg": order.shipper_address,
        "shipperFactoryRemark": "",
        "consigneeName": order.consignee_name,
        "consigneeTel": order.consignee_tel,
        "consigneeAddress": order.consignee_address,
        "consigneeFactoryId": "",
        "consigneeFactoryName": "",
        "consigneeFactoryContacts": "",
        "consigneeFactoryAddressMsg": order.consignee_address,
        "consigneeFactoryRemark": "",
        "notifierName": order.notifier_name,
        "notifierTel": order.notifier_tel,
        "notifierAddress": order.notifier_address,
        # 货物
        "goodsCname": "",
        "goodsEname": order.goods_ename,
        "goodsType": "",
        "shippingMark": order.shipping_mark,
        "orderChannel": "",
        # 箱型箱量 + 每箱运价（每组一条）
        "orderInfos": order_infos,
        # 费用/附件（v1 恒空）
        "yfFees": [],
        "ysFees": [],
        "billFiles": [],
        "orderFiles": [],
    }


# 提交侧需要 None→"" 转换的键（数字键保持：type/ieFlag/ctnNum/totalPrice）
_STR_KEYS: tuple[str, ...] = (
    "bOrderNum",
    "bStartPort",
    "bEndPort",
    "bShipName",
    "bShipNum",
    "bTotleNum",
    "bTotleWeight",
    "bTotleBulk",
    "shipperName",
    "shipperTel",
    "shipperAddress",
    "shipperFactoryAddressMsg",
    "consigneeName",
    "consigneeTel",
    "consigneeAddress",
    "consigneeFactoryAddressMsg",
    "notifierName",
    "notifierTel",
    "notifierAddress",
    "goodsEname",
    "shippingMark",
    "bDate",
    "unitPrice",
)


def to_submit_payload(order_data: dict) -> dict:
    """展示口径 → 实际提交体（None→""；结构原样复制，不改动展示版）。

    仅转换既知字符串键（_STR_KEYS + orderInfos 内条目）；必填齐全的单在
    服务层已拦截，正常不会出现 None，防御性兜底转空串。
    """
    payload = dict(order_data)
    # 顶层键：_STR_KEYS 内 None → ""；其余键原样（type/ieFlag/ctnNum/totalPrice 保持）
    for key in _STR_KEYS:
        if payload.get(key) is None:
            payload[key] = ""
    # orderInfos 条目：同样口径
    payload["orderInfos"] = [
        {k: ("" if v is None else v) for k, v in info.items()}
        for info in payload.get("orderInfos") or []
    ]
    return payload
