"""订单创建结果模型（create_result / error，bill 与 manifest 通道共用）。

create_result 为两个导入通道共用的响应契约（见《竞品账单导入接口文档》
§3.2.1；舱单接口文档 §4.4「对齐账单导入惯例」）：固定六键 + error 三元组。
本模块只定义结构（零业务依赖），构造与消费在各子域层。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OrderError(BaseModel):
    """单条订单创建失败详情（create_result.error，R4/R5 模型化）。

    固定三字段，无 description（本地零消费的冗余双轨已删除，message 为唯一
    可读文案）：code 机器可读（unknown_box_type/missing_bl_no/order_upstream_
    error）；message 单条失败说明；details 结构化补充，无补充为 {}。
    """

    model_config = ConfigDict(extra="ignore")

    code: str = Field(description="机器可读错误码")
    message: str = Field(description="单条失败说明（可读文案/上游原文）")
    details: dict[str, Any] = Field(default_factory=dict, description="结构化补充（上游回显/清单等），无补充为 {}")


class CreateResult(BaseModel):
    """单条订单创建结果（create_result，R5 模型化，对齐接口文档 §3.2.1）。

    固定六键，缺省键恒输出不省略（skipped 默认 False、o_id/upstream/error 默认
    null）：success 成功（含去重 skipped）；skipped 去重跳过；sn 下游订单号；
    o_id TMS 通道业务编号（canonical 成功回显）；upstream 下游成功回显原样
    保留（溯源）；error 失败详情（成功/跳过为 null）。
    """

    model_config = ConfigDict(extra="ignore")

    success: bool = Field(description="是否成功（skipped 去重命中亦算成功）")
    skipped: bool = Field(default=False, description="是否去重跳过（同 sk 已创建成功过该键）")
    sn: str | None = Field(None, description="下游订单号；失败为 null")
    o_id: str | int | None = Field(None, description="TMS 通道业务编号；仅 canonical 成功且有回显时非 null")
    upstream: dict[str, Any] | None = Field(None, description="下游成功回显原样保留（溯源用）；无回显为 null")
    error: OrderError | None = Field(None, description="失败详情；成功/跳过为 null")
