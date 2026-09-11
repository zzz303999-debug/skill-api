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

    type: str | None = Field(
        None,
        description=(
            "原文箱型代码；标准短代码为 20/25/40 加两位字母（如 20GP、40HQ），"
            "禁止在 HQ/HC/DV/GP 等类型间改写"
        ),
    )
    qty: int | None = 1
    container_no: str | None = Field(None, description="箱号，必须为 4 个大写字母加 7 位数字")
    seal_no: str | None = Field(None, description="封号，不得包含空格或 OCR 图片区域文字")
    packages: int | None = None
    packages_unit: str | None = None
    gross_weight_kg: float | None = Field(None, description="毛重（KG），最多保留 3 位小数")
    volume_cbm: float | None = Field(None, description="体积（CBM），最多保留 3 位小数")
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
    mbl_no: str | None = Field(None, description="至少 8 位，仅由数字或英文字母数字组成")
    hbl_no: str | None = Field(None, description="至少 8 位，仅由数字或英文字母数字组成")

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
    customer: str | None = Field(
        None,
        description="客户名称或简称；优先取 FM 后的值，缺失时取明确客户栏或正文抬头公司",
    )
    shipper_company: str | None = Field(
        None,
        description=(
            "正文明确发货人/托运人栏位；zuoxiang_std_esff 模板取做箱工厂，"
            "对应订单 c_title"
        ),
    )
    shipper_agent: str | None = Field(None, description="文档正文抬头或落款中的委托公司")

    # 展示层扩展字段
    recipient: str | None = Field(None, description="收件方（TO/致/ATTN）")
    doc_date: str | None = Field(None, description="文档日期（DATE）", pattern=DATE_PATTERN)
    sender: str | None = Field(None, description="发货方（FROM）")
    sender_contact: str | None = Field(None, description="发货联系人（FROM/FM）")

    remark: str | None = None
    order_mapping: OrderMapping = Field(default_factory=OrderMapping)
    review_issues: list[ReviewIssue] = Field(default_factory=list)
    ready_for_order: bool = False
    source: Source
    raw_text_snippet: str | None = None
