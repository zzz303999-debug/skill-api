"""舱单解析结果数据契约（对齐《舱单导入接口文档》v1.0 §4）。

本模块只定义数据结构与常量，不含解析/映射/提交逻辑：
- MANIFEST_REQUIRED：create 模式必填字段（缺失拦截该单）
- ManifestBoxGroup / ManifestContainer：箱型箱量聚合 / 箱号封号明细
- ManifestOrder：一文件一票的解析结果（TMS 舱单字段的超集）
- ManifestParseResult：响应模型（orders + summary + upstream + meta）

口径备注（v1.0 冻结；v1.2 运价默认 0）：
- 起运港 = POL 原文（自由输入+必填，无中文映射）；
- 必填三项 = bl_no / box_groups / pol（每箱运价恒 0，见 payload._UNIT_PRICE，
  不再作为必填项）；
- 未提取到字段一律 None（展示口径）；提交侧空串转换在 payload 层做；
- etd_text 保留原文（无年份不猜测补全，bDate 提交留空）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..order_result import CreateResult
from ..order_result import OrderError as OrderError

# create 模式必填（缺失 → 拦截该单不提交，preview 只报告）：
# 提单号 / 起运港（POL 原文）
# （box_groups 自 v1.7 起升级为文件级拒绝 manifest_box_missing（preview 亦拒绝），
#   不在 _mark_not_ready 按单拦截；保留于元组供解析层 missing 标记与防御）
# （每箱运价已移出必填：TMS 误设必填 + 模版无运价字段 → 恒 0）
MANIFEST_REQUIRED: tuple[str, ...] = ("bl_no", "box_groups", "pol")

# 未提取到原因取值（对齐账单导入惯例）
REASON_NOT_FOUND = "原文未找到"
REASON_INVALID_FORMAT = "格式不合法"


class ManifestBoxGroup(BaseModel):
    """箱型箱量聚合条目（payload 展开为 orderInfos[].bType/ctnNum）。"""

    model_config = ConfigDict(extra="ignore")

    b_type: str = Field(description="箱型（40HQ/20GP 等；受白名单文件级校验）")
    ctn_num: int = Field(default=1, description="该箱型数量")


class ManifestContainer(BaseModel):
    """箱明细一箱：箱号/封号（payload 暂无落点，预览与追溯保留）。"""

    model_config = ConfigDict(extra="ignore")

    container_no: str | None = Field(None, description="箱号（4 字母+7 数字形态）")
    seal_no: str | None = Field(None, description="封条号（SITR+数字等形态）")
    box_type: str | None = Field(None, description="箱型（模版明细通常不提供）")


class ManifestOrder(BaseModel):
    """一票舱单的解析结果（字段名对齐接口文档 §4.2）。"""

    model_config = ConfigDict(extra="ignore")

    # ---- 单头（家族 A/B 共通提取）----
    bl_no: str | None = Field(
        None,
        description="提单号（托书 MBL NO 双号取斜杠后段船司号 / SI Booking·BL Number；必填；TMS 不支持斜杠）",
    )
    family: str = Field(description="家族：authorization（托书）/ si（Shipping Instruction）")
    vessel: str | None = Field(None, description="船名（船名航次拆分，见需求文档 §6.3）")
    voyage: str | None = Field(None, description="航次（末尾数字+N token）")
    etd_text: str | None = Field(None, description="ETD 原文（无年份不补全；bDate 提交留空）")
    pol: str | None = Field(None, description="装货港原文（起运港 bStartPort 数据源；必填）")
    pod: str | None = Field(None, description="卸货港原文")
    final_destination: str | None = Field(None, description="目的地原文（bEndPort 首选来源）")
    shipper_name: str | None = Field(None, description="发货人（Shipper 区块原文）")
    shipper_address: str | None = Field(None, description="发货人地址（区块内 Address/ADD 行或 ' No. ' 连排切分）")
    shipper_tel: str | None = Field(None, description="发货人电话（区块内 Tel/Phone 标签或 11 位手机号形态）")
    consignee_name: str | None = Field(None, description="收货人（Consignee 区块原文）")
    consignee_address: str | None = Field(None, description="收货人地址（同 shipper_address 口径）")
    consignee_tel: str | None = Field(None, description="收货人电话（同 shipper_tel 口径）")
    notifier_name: str | None = Field(None, description="通知人（Notify Party；SAME AS CONSIGNEE 已展开为收货人名/地址/电话）")
    notifier_address: str | None = Field(None, description="通知人地址（SAME AS 展开时连带复制收货人地址）")
    notifier_tel: str | None = Field(None, description="通知人电话（SAME AS 展开时连带复制收货人电话）")
    goods_ename: str | None = Field(
        None,
        description="英文品名（品名块剔除 INVOICE No./HS CODE 元数据行；TMS goodsEname 限 100 字符）",
    )
    hs_code: str | None = Field(None, description="HS CODE（家族 B 表单区；品名块内混排不拆）")
    pieces: float | None = Field(None, description="件数（bTotleNum 数据源；前导数字/底部汇总）")
    gross_weight: float | None = Field(None, description="毛重（GW；bTotleWeight 数据源）")
    net_weight: float | None = Field(None, description="净重（NW；payload 无落点，预览保留）")
    volume_cbm: float | None = Field(None, description="体积 CBM（bTotleBulk 数据源）")
    shipping_mark: str | None = Field(None, description="唛头（Mark & numbers）")
    # ---- 箱信息 ----
    box_groups: list[ManifestBoxGroup] = Field(
        default_factory=list, description="箱型箱量聚合（必填；受白名单文件级校验）"
    )
    containers: list[ManifestContainer] = Field(
        default_factory=list, description="箱号/封号明细（保持行序）"
    )
    # ---- 元信息 ----
    missing_fields: list[str] = Field(default_factory=list, description="必填缺失字段名")
    missing_reasons: dict[str, str] = Field(
        default_factory=dict, description="缺失字段 → 中文原因"
    )
    order_data: dict[str, Any] | None = Field(
        None,
        description="上游 addBill 请求体回显（preview=待提交，create=实际提交；必填缺失键置 null）",
    )
    create_result: CreateResult | None = Field(
        None, description="创建结果（固定六键 + error 三元组，对齐账单口径）；preview 为 null"
    )

    def add_missing(self, field: str, reason: str = REASON_NOT_FOUND) -> None:
        """登记缺失字段（去重保序）与中文原因。"""
        if field not in self.missing_fields:
            self.missing_fields.append(field)
        self.missing_reasons.setdefault(field, reason)


class ManifestParseResult(BaseModel):
    """舱单导入响应模型（对齐接口文档 §4.1 总览）。"""

    model_config = ConfigDict(extra="ignore")

    file: str = Field(description="上传文件名")
    create_order: bool = Field(default=False, description="回显本次开关取值")
    orders: list[ManifestOrder] = Field(default_factory=list, description="一文件一票")
    summary: dict[str, Any] | None = Field(
        None, description="创建汇总（create 模式填充；preview 为 null）"
    )
    upstream: dict[str, Any] | None = Field(
        None,
        description="上游回显（create 且确有新建时填充，对齐 addBill 响应格式）",
    )
    meta: dict[str, Any] = Field(
        default_factory=dict, description="溯源信息（文件哈希、解析时间、引擎、家族）"
    )


class ManifestImportResponse(BaseModel):
    """舱单导入统一响应外壳（对齐 TMS 通道 code/msg/data 口径）。

    - code：业务码（字符串）——"200"=成功；"204"=全部失败；错误场景
      （400/401/413/422/429/503 等）为机器可读错误码（bad_request 等）
    - msg：业务信息（成功/失败描述；错误场景为可直接展示的中文说明）
    - data：业务数据（原 ManifestParseResult 全部字段原样放入；错误场景为原 details）
    HTTP 错误场景同样套本外壳，不再返回 error 结构。
    """

    model_config = ConfigDict(extra="ignore")

    code: str = Field(description="业务码：200=成功；204=全部失败")
    msg: str = Field(description="业务信息（成功/失败描述；错误场景含具体原因）")
    data: ManifestParseResult = Field(description="业务数据（原响应全部字段）")
