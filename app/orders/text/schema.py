"""自由文本下单子域契约：请求/抽取结构/响应（文本链路专用）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreateOrderFromTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, description="上游传入的自由文本订单内容")
    roomId: str = Field(min_length=1, description="上游会话/房间标识，原样回传")
    userId: str = Field(min_length=1, description="上游用户标识，透传给下游订单接口")


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
