"""自由文本下单接口的输入输出契约。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreateOrderFromTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, description="上游传入的自由文本订单内容")
    roomId: str = Field(min_length=1, description="上游会话/房间标识，原样回传")


class OrderApiResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str | int
    msg: str | None = ""
    data: Any = None


class CargoItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    b_order_num: str | None = None
    j: str | None = Field(None, description="件数，纯数字字符串")
    m: str | None = Field(None, description="毛重，纯数字字符串")
    t: str | None = Field(None, description="体积，纯数字字符串")
    hh: str | None = Field(None, description="货名")
    mt: str | None = Field(None, description="唛头")


class BoxItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    b_type: str
    box_num: int = Field(ge=1)


class DriverItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    b_date: str | None = None
    b_date_time_start: str | None = None


class OrderTextExtraction(BaseModel):
    """自由文本专用抽取结构，字段名与下单接口 data 对齐。"""

    model_config = ConfigDict(extra="ignore")

    order_num1: str | None = None
    c_title: str | None = None
    c_name: str | None = None
    c_phone: str | None = None
    b_ship_name: str | None = None
    b_ship_num: str | None = None
    b_ship_company: str | None = None
    factory_name: str | None = None
    factory_bei: str | None = None
    b_factory_not: str | None = None
    b_start_dock: str | None = None
    b_end_port: str | None = None
    b_end_dock: str | None = None
    b_wharf: str | None = None
    b_open_ship_time: str | None = None
    c_sn: str | None = None
    c_note: str | None = None
    data: list[CargoItem] = Field(default_factory=list)
    box: list[BoxItem] = Field(default_factory=list)
    driver: list[DriverItem] = Field(default_factory=list)


class CreateOrderFromTextResponse(BaseModel):
    roomId: str
    source_fields: dict[str, str]
    extracted: OrderTextExtraction
    order_data: dict[str, Any]
    upstream: OrderApiResponse
    meta: dict[str, Any] = Field(default_factory=dict)


class DocumentBoxItem(BaseModel):
    """附件文档抽取的箱型条目。"""

    model_config = ConfigDict(extra="ignore")

    b_type: str | None = Field(
        None,
        description="箱型，4 位：箱长 20/25/40 加两位字母（如 40HQ、20GP）",
    )
    box_num: int | None = Field(None, description="箱量")


class DocumentCargoItem(BaseModel):
    """附件文档抽取的货物明细行（不含货名/唛头）。"""

    model_config = ConfigDict(extra="ignore")

    b_order_num: str | None = Field(
        None, description="该行提单号，纯数字或字母数字，至少 8 位"
    )
    j: str | None = Field(None, description="件数，纯数字字符串")
    m: str | None = Field(None, description="毛重，纯数字字符串")
    t: str | None = Field(None, description="体积，纯数字字符串")


class OrderDocumentExtraction(BaseModel):
    """附件文档专用抽取结构，字段名与下单接口 data 对齐。

    必填：提单号、箱型、客户、地址、做箱日期、件数、毛重、体积。
    其余字段（船名、船次、船公司、目的港、码头、港区、开船时间、备注等）
    为可选，结构对齐标准订单格式。

    data 为货物明细行列表：一票多客户/多提单号时每行一条
    （含该行提单号、件数、毛重、体积）；仅一行时也输出一条，
    packages/gross_weight/volume 单值字段与 data 第一条一致。
    """

    model_config = ConfigDict(extra="ignore")

    order_num1: str | None = Field(None, description="提单号，纯数字或字母数字，至少 8 位")
    c_title: str | None = Field(None, description="客户，取 FM 后的值或抬头公司")
    c_name: str | None = Field(None, description="现场联系人")
    c_phone: str | None = Field(None, description="联系电话")
    b_ship_name: str | None = Field(None, description="船名")
    b_ship_num: str | None = Field(None, description="船次/航次")
    b_ship_company: str | None = Field(None, description="船公司")
    factory_name: str | None = Field(None, description="门点简称/工厂名称")
    factory_bei: str | None = Field(None, description="详细街道地址")
    b_factory_not: str | None = Field(None, description="装箱备注")
    b_start_dock: str | None = Field(None, description="启运港/装货港")
    b_end_port: str | None = Field(None, description="目的港")
    b_end_dock: str | None = Field(None, description="目的港码头")
    b_wharf: str | None = Field(None, description="港区")
    b_open_ship_time: str | None = Field(None, description="开船时间，YYYY-MM-DD")
    b_date: str | None = Field(None, description="做箱日期，YYYY-MM-DD")
    c_sn: str | None = Field(None, description="内部编号")
    c_note: str | None = Field(None, description="备注/注意事项")
    packages: str | None = Field(None, description="件数，数字 + CTNS（单位可省略）")
    gross_weight: str | None = Field(None, description="毛重，数字 + KGS（单位可省略），保留 2-3 位小数")
    volume: str | None = Field(None, description="体积，数字 + CBM（单位可省略），保留 2-3 位小数")
    data: list[DocumentCargoItem] = Field(
        default_factory=list,
        description="货物明细行：每行含该行提单号 b_order_num、件数 j、毛重 m、体积 t"
    )
    box: list[DocumentBoxItem] = Field(default_factory=list)


class ParseDocumentResponse(BaseModel):
    file: str
    extracted: OrderDocumentExtraction
    order_data: dict[str, Any] | None = None
    needs_manual_confirmation: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    missing_reasons: dict[str, str] = Field(default_factory=dict)
