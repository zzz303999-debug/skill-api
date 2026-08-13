"""竞品账单导入的解析结果数据契约。

本模块只定义数据结构与常量，不含解析/归集/下单逻辑：
- REQUIRED_HEADERS：识别有效账单必需的列
- RECEIVABLE_FEE_COLUMNS：应收费用列名（费用中文名作 shou 键）
- HEADER_COLUMN_MAP：账单表头列名 → BillRow 字段名（列顺序变化不影响解析）
- BillPeriod / BillRow / BillOrder / BillParseResult：结算区间、账单行、归集订单、响应模型

字段对齐《竞品账单导入接口文档》v1.0 的 5.1 映射表与 3.3/3.4 响应结构。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 识别有效账单必需的列：表头需同时包含「客户编号」「提单号」两列
REQUIRED_HEADERS: tuple[str, ...] = ("客户编号", "提单号")

# 应收费用列名：按费用中文名建立 shou 条目（如 shou[0][运费][money]），
# 同费用名多行金额累加；应付/双拖/成本费用线本期不发送
RECEIVABLE_FEE_COLUMNS: tuple[str, ...] = (
    "运费",
    "待时费",
    "预提费",
    "洋山费",
    "落还箱费",
    "其它费",
)

# 表头列名变体 → 标准列名：真实账单表头存在内部空格（如「落/还箱 费」），
# 匹配前先去除全部空白，别名表再处理去空白后仍不一致的变体（如斜杠）
HEADER_ALIASES: dict[str, str] = {
    "落/还箱费": "落还箱费",
}

# 有意不映射的账单列（接口文档 §5.1 明确「不映射」）：
# 表头匹配到这些列不产生未识别列告警；「月份」等走派生逻辑的列仍会告警（属良性）
IGNORED_HEADERS: tuple[str, ...] = ("当前状态", "已收/付金额")

# 账单表头列名 → BillRow 字段名。
# 一列一映射；同一字段可有多个来源列（如「联系人」「客户联系人」均映射 c_name，
# 优先级回退由解析逻辑处理）；「当前状态」「已收/付金额」等列不映射。
HEADER_COLUMN_MAP: dict[str, str] = {
    "序号": "seq",
    "日期": "b_date",
    "客户名称": "c_title",
    "客户联系人": "c_name",
    "客户编号": "c_sn",
    "业务类型": "biz_type",
    "提单号": "order_num1",
    "门点": "factory_name",
    "箱型": "b_type",
    "联系人": "c_name",
    "联系电话": "c_phone",
    "装卸货地点": "factory_bei",
    "装卸货地址": "factory_bei",
    "箱号": "container_no",
    "做箱时间": "b_date_time",
    "港区": "b_wharf",
    "提箱堆场": "b_get_address",
    "还箱堆场": "b_back_address",
    "车牌号": "d_num",
    "车队": "fleet",
    "司机": "d_name",
    "司机手机": "d_phone",
    "备注": "remark",
    "应付备注": "payable_remark",
}

# 业务信息必填字段（对齐《订单创建接口文档》v1.0 口径）：
# order_num1 / c_title / box[] 为提取必填；type 固定 1、data[].b_order_num 随提单号联动；
# driver[].b_date 为非必填，缺失不返回未提取到提示
REQUIRED_FIELDS: tuple[str, ...] = ("order_num1", "c_title", "box")

# 必填项未提取到时 missing_fields 的取值（见接口文档 5.2）
MISSING_ORDER_NUM1 = "order_num1"
MISSING_C_TITLE = "c_title"
MISSING_BOX = "box"

# 未提取到原因取值
REASON_NOT_FOUND = "原文未找到"
REASON_INVALID_FORMAT = "格式不合法"


class BillPeriod(BaseModel):
    """账单结算区间（抬头「结算日期」），用于日期补年份推断。"""

    model_config = ConfigDict(extra="ignore")

    start: str | None = Field(None, description="区间起始，YYYY-MM-DD")
    end: str | None = Field(None, description="区间结束，YYYY-MM-DD")


class BillRow(BaseModel):
    """账单一行（一柜一行）的解析结果，值为单元格原文（原文保真，不做清洗）。

    清洗/校验/转换（提单号去空格连字符与浮点尾巴、日期补年份、金额转数字等）
    由后续解析步骤负责；费用金额以数字形式存 fees，非数字值由解析跳过。
    """

    model_config = ConfigDict(extra="ignore")

    seq: str | None = Field(None, description="序号，组内排序依据")
    b_date: str | None = Field(None, description="日期（候选 b_date），月-日格式，需补年份")
    b_date_time: str | None = Field(None, description="做箱时间（候选 b_date）")
    c_title: str | None = Field(None, description="客户名称")
    c_name: str | None = Field(None, description="客户联系人；为空时回退「联系人」列")
    c_sn: str | None = Field(None, description="客户编号（实为委托号），为空时不发送")
    biz_type: str | None = Field(None, description="业务类型（进口/出口），暂存 c_note")
    order_num1: str | None = Field(None, description="提单号原文")
    b_type: str | None = Field(
        None,
        description="箱型原文，非空即合法（不做格式校验；含 40HQ 等标准箱型及大冷/飞翼车/拼箱 等业务表述）",
    )
    factory_name: str | None = Field(None, description="门点/工厂简称")
    factory_bei: str | None = Field(None, description="装卸货地点/地址（两列合并）")
    container_no: str | None = Field(None, description="箱号，汇总入 c_note 兜底")
    b_wharf: str | None = Field(None, description="港区")
    b_get_address: str | None = Field(None, description="提箱堆场")
    b_back_address: str | None = Field(None, description="还箱堆场")
    d_num: str | None = Field(None, description="车牌号")
    fleet: str | None = Field(None, description="车队，暂存 c_note")
    d_name: str | None = Field(None, description="司机")
    d_phone: str | None = Field(None, description="司机手机")
    c_phone: str | None = Field(None, description="联系电话")
    remark: str | None = Field(None, description="备注，追加到 c_note")
    payable_remark: str | None = Field(None, description="应付备注，追加到 c_note")
    fees: dict[str, float | str] = Field(
        default_factory=dict,
        description="应收费用：费用中文名（RECEIVABLE_FEE_COLUMNS）→ 金额数字；非数字原文（含中文大写）原样保留，由归集侧按类型过滤",
    )


class BillOrder(BaseModel):
    """按提单号归集后的订单（一票一单）解析结果。"""

    model_config = ConfigDict(extra="ignore")

    order_num1: str | None = Field(None, description="提单号（订单号）；缺失/非法时为 null")
    c_title: str | None = Field(None, description="客户名称；缺失时为 null")
    container_count: int = Field(default=0, description="该订单柜数")
    row_count: int = Field(default=0, description="该订单原始账单行数")
    missing_fields: list[str] = Field(default_factory=list, description="未提取到字段名")
    missing_reasons: dict[str, str] = Field(
        default_factory=dict, description="未提取到字段 → 中文原因"
    )
    order_data: dict[str, Any] | None = Field(
        None,
        description="下单接口请求体数据（嵌套结构）；必填项未提取到时对应字段为 null",
    )
    create_result: dict[str, Any] | None = Field(
        None,
        description="创建结果 {success, sn, error}，由创建步骤填充；preview 模式为 null",
    )


class BillParseResult(BaseModel):
    """账单导入的解析结果响应（对齐接口文档 3.3 响应总览）。

    orders 为既有流程语义归集（BillOrder）；canonical_orders 为标准
    字段归集（CanonicalOrder，TMS 通道）——两条路径互斥填充，字段为新增，
    既有消费者不受影响。
    """

    model_config = ConfigDict(extra="ignore")

    file: str = Field(description="上传文件名")
    bill_period: str | None = Field(None, description="结算区间 YYYY-MM-DD~YYYY-MM-DD")
    total_rows: int = Field(default=0, description="识别到的数据行数（一柜一行）")
    order_count: int = Field(default=0, description="归集后的订单数（按提单号合并）")
    create_order: bool = Field(default=False, description="回显本次开关取值")
    orders: list[BillOrder] = Field(default_factory=list)
    canonical_orders: list[CanonicalOrder] = Field(
        default_factory=list, description="TMS 通道标准订单（模板驱动解析时填充）"
    )
    summary: dict[str, Any] | None = Field(
        None, description="汇总 total/success/failed，由创建步骤填充；preview 模式为 null"
    )
    meta: dict[str, Any] = Field(
        default_factory=dict, description="溯源信息（文件哈希、解析时间、引擎等）"
    )


# ---- 标准业务订单模型（CanonicalOrder，TMS 业务订单接入，见《字段映射表》§1） ----


class BoxGroup(BaseModel):
    """同组多箱型聚合条目：箱型 + 数量（payload 展开为 box[N][b_type/box_num]）。"""

    model_config = ConfigDict(extra="ignore")

    b_type: str = Field(description="箱型（40HQ/20GP/非标原样）")
    box_num: int = Field(default=1, description="该箱型数量")


class ContainerInfo(BaseModel):
    """同组一箱：箱号/箱型/封号（payload 展开为 b_num/b_lock）。"""

    model_config = ConfigDict(extra="ignore")

    container_no: str | None = Field(None, description="箱号")
    box_type: str | None = Field(None, description="箱型")
    seal_no: str | None = Field(None, description="封条号")


class CanonicalOrder(BaseModel):
    """标准业务订单（业务信息类，27+ 字段，核心/常见/扩展三档，见《字段映射表》§1）。

    由模板配置驱动解析的标准字段行按 group_key 归集产出（T5）；既有流程
    经 BillOrder → CanonicalOrder 转换（to_canonical）得到同一模型，TMS 通道统一
    用本模型构造 form-data（payload.py）。必填缺失行标红进 missing_fields，不阻塞。
    """

    model_config = ConfigDict(extra="ignore")

    # ---- 核心字段（≥6 家族覆盖）----
    bl_no: str | None = Field(None, description="提单号（必填）")
    box_groups: list[BoxGroup] = Field(default_factory=list, description="箱型箱量聚合（必填）")
    customer_name: str | None = Field(None, description="客户名称")
    work_date: str | None = Field(None, description="做箱时间 YYYY-MM-DD")
    port_area: str | None = Field(None, description="港区")
    pickup_point: str | None = Field(None, description="提箱点")
    biz_type: str | None = Field(None, description="业务类型（进口/出口/…）")
    # ---- 常见字段（3~5 家族覆盖）----
    biz_no: str | None = Field(None, description="业务编号（归集主键；TMS 侧进 b_note 备查）")
    order_date: str | None = Field(None, description="业务日期 YYYY-MM-DD（派生 month）")
    customer_no: str | None = Field(None, description="客户编号")
    customer_contact: str | None = Field(None, description="客户联系人")
    contact_person: str | None = Field(None, description="联系人（装卸点）")
    contact_phone: str | None = Field(None, description="联系电话")
    door_point: str | None = Field(None, description="门点")
    load_address: str | None = Field(None, description="装卸货地址")
    return_point: str | None = Field(None, description="还箱点")
    seal_no: str | None = Field(None, description="封条号")
    vessel: str | None = Field(None, description="船名")
    voyage: str | None = Field(None, description="航次")
    ship_date: str | None = Field(None, description="船期 YYYY-MM-DD")
    discharge_port: str | None = Field(None, description="卸货港")
    delivery_place: str | None = Field(None, description="交货地")
    plate_no: str | None = Field(None, description="车牌号")
    driver_name: str | None = Field(None, description="司机")
    driver_phone: str | None = Field(None, description="司机手机")
    fleet: str | None = Field(None, description="车队")
    pieces: int | None = Field(None, description="件数")
    gross_weight: float | None = Field(None, description="毛重")
    remark: str | None = Field(None, description="备注（b_note；竞品业务编号拼入备查）")
    # ---- 扩展字段（1~2 家族覆盖，预留位）----
    io_type: str | None = Field(None, description="进出口")
    port_open_time: str | None = Field(None, description="开港时间 YYYY-MM-DD")
    port_cut_time: str | None = Field(None, description="截港时间 YYYY-MM-DD")
    port_in_time: str | None = Field(None, description="进港时间 YYYY-MM-DD")
    shipping_company: str | None = Field(None, description="船公司")
    cargo_name: str | None = Field(None, description="货名")
    load_point: str | None = Field(None, description="装卸点")
    # ---- 聚合结构（同组多行/多箱型）----
    containers: list[ContainerInfo] = Field(
        default_factory=list, description="同组多行箱信息聚合（一票多箱）"
    )
    month: str | None = Field(None, description="账期 YYYY-MM（order_date 派生）")
    # ---- 元信息 ----
    missing_fields: list[str] = Field(default_factory=list, description="缺失字段清单（必填缺失行）")
    source_template: str = Field(default="", description="命中的 template_id")
    row_count: int = Field(default=0, description="归集原始行数")
    create_result: dict[str, Any] | None = Field(
        None, description="TMS 下单结果 {success, sn, o_id, error}；preview 模式为 null"
    )

    def add_missing(self, field: str) -> None:
        """登记缺失字段（去重保序）。"""
        if field not in self.missing_fields:
            self.missing_fields.append(field)


# 必填字段（TMS 通道，见《字段映射表》§4）：提单号 + 箱型；其余缺失记 missing 不阻塞
CANONICAL_REQUIRED: tuple[str, ...] = ("bl_no", "box_groups")


def _first_nonempty(d: dict[str, Any], *keys: str) -> Any | None:
    """按序取首个非空值。"""
    for key in keys:
        value = d.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def to_canonical(order: BillOrder, source_template: str = "jinxin_v1") -> CanonicalOrder:
    """BillOrder → CanonicalOrder 转换（既有流程零感知；内置模板家族的 TMS 通道入口）。

    映射口径见《TMS业务订单新增接口-逆推规范》§4；旧流程 order_data 的扁平键
    逐项对齐到标准字段；缺失项（必填 bl_no/box_groups）登记 missing_fields。
    """
    data = order.order_data or {}
    box_groups = [
        BoxGroup(b_type=box["b_type"], box_num=int(box.get("box_num", 1)))
        for box in data.get("box", []) or []
        if isinstance(box, dict) and box.get("b_type")
    ]
    driver = (data.get("driver") or [{}])[0]
    canonical = CanonicalOrder(
        bl_no=order.order_num1 or _first_nonempty(data, "order_num1"),
        box_groups=box_groups,
        customer_name=order.c_title or _first_nonempty(data, "c_title"),
        customer_no=_first_nonempty(data, "c_sn"),
        customer_contact=_first_nonempty(data, "c_name"),
        contact_phone=_first_nonempty(data, "c_phone"),
        door_point=_first_nonempty(data, "factory_name"),
        load_address=_first_nonempty(data, "factory_bei"),
        port_area=_first_nonempty(data, "b_wharf"),
        work_date=_first_nonempty(driver, "b_date"),
        pickup_point=_first_nonempty(driver, "b_get_address"),
        return_point=_first_nonempty(driver, "b_back_address"),
        plate_no=_first_nonempty(driver, "d_num"),
        driver_name=_first_nonempty(driver, "d_name"),
        driver_phone=_first_nonempty(driver, "d_phone"),
        month=_first_nonempty(data, "month"),
        remark=_first_nonempty(data, "c_note"),
        source_template=source_template,
        row_count=order.row_count,
    )
    # 必填缺失登记（旧流程 missing 口径 → 标准字段名）
    if order.order_num1 is None or not canonical.bl_no:
        canonical.add_missing("bl_no")
    if not box_groups:
        canonical.add_missing("box_groups")
    if order.c_title is None and not canonical.customer_name:
        canonical.add_missing("customer_name")
    return canonical
