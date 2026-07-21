"""托书抽取输出的 Pydantic schema（强类型，直接进 OpenAPI 文档）。

对应 references/schema.md。所有字段允许 null。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DocType = Literal[
    "PACKING_NOTICE",
    "TRANSPORT_ORDER",
    "TRUCKING_ORDER",
    "BOOKING_NOTE",
    "UNKNOWN",
]

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
DATE_OR_DATETIME_PATTERN = r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?$"


class ReviewIssue(BaseModel):
    """需要人工处理后才能进入订单接口的问题。"""

    model_config = ConfigDict(extra="ignore")

    code: str
    field: str
    message: str
    source_values: list[str] = Field(default_factory=list)
    blocking: bool = True


class OrderMapping(BaseModel):
    """抽取结果到订单接口的确定性字段映射。"""

    model_config = ConfigDict(extra="ignore")

    c_sn: str | None = Field(None, description="我司业务编号，对应 internal_ref")
    mbl_no: str | None = Field(None, description="主提单号")
    hbl_no: str | None = Field(None, description="子提单号/分提单号")
    c_title: str | None = Field(None, description="托运人公司名称")
    factory_name: str | None = Field(None, description="工厂门点简称")
    c_note: str | None = Field(None, description="订单备注，包含 PO 号和原文备注")


class ContainerItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str | None = Field(None, description="归一后的箱型：20GP/40GP/40HC/45HC 等")
    qty: int | None = 1
    container_no: str | None = None
    seal_no: str | None = None
    packages: int | None = None
    packages_unit: str | None = None
    gross_weight_kg: float | None = None
    volume_cbm: float | None = None
    po_no: str | None = None
    mbl_no: str | None = None
    remark: str | None = None


class Factory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    address: str | None = None
    contact: str | None = None
    phone: str | None = None


class Source(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: str
    doc_format: str
    template_hint: str | None = None
    extracted_at: str | None = None


class TuoshuOutput(BaseModel):
    """托书统一抽取结果。"""

    model_config = ConfigDict(extra="ignore")

    doc_type: DocType = "UNKNOWN"
    internal_ref: str | None = Field(None, description="我司业务编号，对应订单 c_sn")
    customs_declaration_no: str | None = None
    customer_ref: str | None = None
    mbl_no: str | None = None
    hbl_no: str | None = None

    vessel: str | None = None
    voyage: str | None = None
    carrier: str | None = None

    pol: str | None = None
    pod: str | None = None
    transit_port: str | None = None
    terminal: str | None = None

    etd: str | None = Field(None, pattern=DATE_PATTERN)
    si_cutoff: str | None = None
    customs_cutoff: str | None = None
    loading_time: str | None = Field(None, pattern=DATE_OR_DATETIME_PATTERN)

    containers: list[ContainerItem] = Field(default_factory=list)

    factory: Factory | None = None
    shipper_company: str | None = Field(None, description="托运人公司，对应订单 c_title")
    shipper_agent: str | None = None

    # 展示层扩展字段
    recipient: str | None = Field(None, description="收件方（TO）")
    doc_date: str | None = Field(None, description="文档日期（DATE）", pattern=DATE_PATTERN)
    sender: str | None = Field(None, description="发货方（FROM）")
    sender_contact: str | None = Field(None, description="发货联系人")

    remark: str | None = None
    order_mapping: OrderMapping = Field(default_factory=OrderMapping)
    review_issues: list[ReviewIssue] = Field(default_factory=list)
    ready_for_order: bool = False
    source: Source
    raw_text_snippet: str | None = None
