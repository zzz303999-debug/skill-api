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


class ContainerItem(BaseModel):
    model_config = ConfigDict(extra="allow")

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
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    address: str | None = None
    contact: str | None = None
    phone: str | None = None


class Source(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str
    doc_format: str
    template_hint: str | None = None
    extracted_at: str | None = None


class TuoshuOutput(BaseModel):
    """托书统一抽取结果。"""

    model_config = ConfigDict(extra="allow")

    doc_type: DocType = "UNKNOWN"
    booking_no: str | None = None
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

    etd: str | None = None
    si_cutoff: str | None = None
    customs_cutoff: str | None = None
    loading_time: str | None = None

    containers: list[ContainerItem] = Field(default_factory=list)

    factory: Factory | None = None
    shipper_agent: str | None = None

    remark: str | None = None
    source: Source
    raw_text_snippet: str | None = None
