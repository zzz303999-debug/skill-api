# TMS 业务订单新增接口 · 抓包逆推规范

> 依据：2026-08-13 浏览器 form-data 抓包（新增一单 20GP×1 出口订单）+ 响应报文。
> 状态：字段语义为逆推结论，**待最小报文 A/B 实测确认**（见 §6）。
> 与既有 bill-import 的关系：该 form-data 结构即下单「AddWork 表单通道」的字段格式，本规范可直接作为其 payload 构造依据。

---

## 1. 请求格式

- **端点与鉴权**：复用既有 `/orders` 下单通道（与 bill-import 客户端同一鉴权体系，JXT 凭据），**设置 `create_order=true` 触发下单**——无需新增端点，客户端层改动极小
- **Content-Type**：表单提交（form-data），字段为扁平括号记法 `group[idx][field][sub]`
- **冗余双写**：顶层 `a`/`b`/`c` 三个元字段——`a`、`c` 为空对象；**`b` 是部分字段（83 键）的 JSON 字符串双写**，完整字段集（~160 键）只存在于扁平表单里
- **响应**：`{"code": "200", "msg": "添加成功", "data": [订单对象]}` —— 注意 **code 是字符串** `"200"`，客户端比较时不能用数值

---

## 2. 字段分组逆推

### 2.1 单头业务字段（顶层）

| 表单字段 | 抓包值 | 逆推含义 | 响应字段印证 |
|---|---|---|---|
| `order_num1` | 1 | 票数 | `order_num1` |
| `b_ship_name` / `b_ship_num` | 空 | 船名 / 航次 | 同名 |
| `b_start_dock` | SHANGHAI | 起运港/始发地 | `b_start_dock` |
| `b_wharf` | 空 | 港区/码头（**与 b_start_dock 的语义边界待确认**） | `b_wharf` |
| `b_end_port` / `b_end_dock` | 空 | 目的港 / 目的码头 | 同名 |
| `b_ship_company` | 空 | 船公司 | 同名（响应示例 `b_ctn_owner: "MSK 马士基"`） |
| `b_open_ship_time` / `b_close_ship_time` | 空 | 开港时间 / 截港时间 | 同名 |
| `b_unberthing_time` / `wharf_time` / `outoc_time` / `inoc_time2` / `save_time` | 空 | 靠泊/进出场等时间节点 | 同名 |
| `month` | 2026-08 | 账期（YYYY-MM） | `month` |
| `type` | 1 | 业务类型枚举（**1 ↔ 出口**，响应 `work_type: "出口"`） | `work_type` |
| `b_note` | 空 | 订单备注 | 同名 |
| `cargo_port` | 空 | 装箱港? | 同名 |

### 2.2 客户字段

| 表单字段 | 逆推含义 |
|---|---|
| `c_title` / `c_name` / `c_phone` / `c_sn` | 客户抬头 / 名称 / 电话 / 编号 |
| `c_id` / `cu_id_list` | 客户档案 ID（响应 `cu_id: 15478`） |
| `c_note` | 客户备注 |
| `payment_time` / `tax_rate` / `account` / `freight_id` | 付款期限 / 税率 / 结算账户 / 运价ID |

### 2.3 货物明细 `data[N]`（数组，每提单号一条）

| 子字段 | 抓包值 | 逆推含义 | 响应印证（`b_jmt[]`） |
|---|---|---|---|
| `data[0][b_order_num]` | 1 | **提单号（必填✓）** | `b_jmt[0].b_order_num` |
| `data[0][j]` | 空 | 件数 | `j` |
| `data[0][m]` | 空 | 毛重 | `m` |
| `data[0][t]` | 空 | 体积 | `t` |
| `data[0][hh]` | 空 | 货名 | `hh` |
| `data[0][mt]` | 空 | 唛头 | `mt` |

> j/m/t/hh/mt 五字段由响应结构 `b_jmt` 确证（件/毛/体/货/唛）。

### 2.4 箱信息

| 表单字段 | 抓包值 | 逆推含义 |
|---|---|---|
| `box[0][b_type]` | 20GP | **箱型（必填✓）**，`box[]` 为数组 → 一票多箱型 |
| `box[0][box_num]` | 1 | 该箱型数量 |
| `b_num` | 空 | 箱号（**顶层单值字段** → 多箱号写法待验证，见 §6-3） |
| `b_lock` | 空 | 封号 |
| `b_tare` / `multiple_tare[]` | 空/[] | 皮重 / 多皮重 |
| `b_danger[s/t/c/dj/p/uc/f/packaging]` | 空 | 危险品八子项（响应 `b_danger` 对象印证） |

### 2.5 门点与车辆

| 表单字段 | 逆推含义 |
|---|---|
| `factory_name` / `factory_id` | 门点名称 / 门点档案 ID（装卸工厂） |
| `factory_bei` / `b_factory_not` | 门点备注 |
| `car_id` / `cg_name` / `car_name` | 车辆档案 ID / 车队（抓包 `王东测试车队`） |

### 2.6 派车段 `driver[0]`（数组）

| 子字段 | 逆推含义 |
|---|---|
| `b_get_address` / `b_back_address` | **提箱点 / 还箱点** |
| `b_date` / `b_date_time_start` | **做箱时间** |
| `d_group` / `d_name` / `d_num` / `d_phone` | 车队 / 司机 / 车牌 / 司机手机 |
| `kind` / `d_sinout` / `distance` / `you_hao` / `d_oil` | 业务种类 / 进出口段 / 公里数 / 油耗 |
| `fore_time` / `driver_note` | 预提时间 / 司机备注 |
| `get_ys_zj` / `pay_yf_zj` | 应收合计 / 应付合计（抓包均 0.00，费用留空时带默认） |

### 2.7 费用四通道（本轮不填，结构留给后续三类）

| 通道 | 语义 | 费目结构（以抓包为例） |
|---|---|---|
| `shou[0][费目]` | **应收** | money / lock_status / price_id(价格表ID) / price_type / cost_id / is_profit / dai_dian(代垫) / customer_tax_rate / class_id + `shou[0][note]` |
| `pay[0][费目]` | **应付** | 同上 + `pay[0][note]` |
| `duo_get[0][费目]` | **多级应收/代收** | 上同 + m_id / client_name / car_group / multiple_tare + `duo_get_hj_zj` 合计 |
| `cost[0][费目]` | **成本（供应商）** | 上同 + driver_name + `supplier_hj_zj` 合计、`cb_ids` |

> **四类归集在接口层得到印证**：业务信息（顶层+data+box+driver）/ 应收 shou / 应付 pay / 成本 cost(+duo_get)。
> 关键约束：**费用按「费目名 → price_id」引用 TMS 价格表**（抓包：运费=820、预提费=121744、油费=1134、打劫费=1135），后续三类接入前需先同步价格表做费目映射。

### 2.8 操作元字段

| 表单字段 | 抓包值 | 说明 |
|---|---|---|
| `user_name` / `section_name` | 章家俊 / 销售部 | 录单人 / 部门（响应回显） |
| `o_id` | 空 | 订单 ID——**空 = 新增，非空 = 更新**（同一接口复用） |
| `appendCost` | true | 追加费用开关 |
| `c_sign` / `yuwu` / `is_bring` / `d_ids` / `cb_ids` | undefined/空 | 签名/业务标记/关联ID，新增场景可空 |

---

## 3. 响应关键回值

| 字段 | 值 | 用途 |
|---|---|---|
| `code` | "200"（字符串） | 成功判定 |
| `data[0].sn` | EX26080355 | **TMS 业务编号（回写对账用）** |
| `data[0].o_id` / `d_id` | 21034692 / 21715471 | 订单 ID / 派车段 ID（后续费用/更新接口的关联键） |
| `data[0].cu_id` / `car_id` | 15478 / 89 | 客户/车辆档案解析结果（可用于校验客户名是否命中档案） |
| `data[0].put_status` / `audit_status` | 待处理 / 1 | 初始状态 |
| `data[0].month` | 2026-08 | 账期回显 |

---

## 4. 标准模型 → TMS 字段最终映射（业务信息类）

| CanonicalOrder | TMS 表单字段 | 备注 |
|---|---|---|
| `bl_no` ★必填 | `data[0][b_order_num]` | 归集键，一单一个 |
| `box_type_qty` ★必填 | `box[N][b_type]` + `box[N][box_num]` | 多箱型展开为数组；`40HQ*2` → 一条 box_num=2 |
| `container_no` | `b_num` | 多箱号写法待验证（§6-3） |
| `seal_no` | `b_lock` | |
| `vessel` / `voyage` | `b_ship_name` / `b_ship_num` | |
| `shipping_company` | `b_ship_company` | |
| `customer_name` | `c_title` | 【2026-08-13 实测确认】响应回显验证（EX26081037/38 回显 `c_title=测试客户204`），TMS「客户」字段自由文本可保存；注意 c_title 非空时控制器硬读 `c_id`，必须同发 `c_id` 空串（缺键 204 拒单 Undefined index: c_id）；「客户」档案选择器与自由文本的自动建档关系待界面确认 |
| `customer_contact` | `c_name` | 客户联系人 → UI「联系人」（2026-08-13 实证；军羽/123/赢辉无此字段，留空） |
| `customer_no` | `c_sn` | |
| `door_point` | `factory_name` | |
| `work_date` | `driver[0][b_date]` | `date_flex` 已归一为 YYYY-MM-DD |
| `pickup_point` / `return_point` | `driver[0][b_get_address]` / `driver[0][b_back_address]` | |
| `port_area` | `b_wharf`（备选 `b_start_dock`） | 语义边界待 §6-2 确认 |
| `discharge_port` | `b_end_port` | |
| `pieces` / `gross_weight` / `cargo_name` | `data[0][j]` / `data[0][m]` / `data[0][hh]` | |
| `biz_type` / `io_type` | `type` | 枚举：1=出口（其余值待补全） |
| `remark` | `b_note` | 费用原文等非数字内容也可拼入 |
| `driver_name` / `plate_no` / `driver_phone` / `fleet` | `driver[0][d_name/d_num/d_phone/d_group]` | |
| `port_open_time` / `port_cut_time` | `b_open_ship_time` / `b_close_ship_time` | |
| `order_date` | 无直接字段 | **派生 `month`（YYYY-MM）**；原始日期可进 b_note 或不传 |
| `biz_no` | 无直接字段 | TMS 自动生成 sn；竞品业务编号建议进 `b_note` 备查 |

**常量/默认**：`order_num1=1`（单票）、`appendCost`、费用四通道留空（本轮）、`o_id` 留空（新增）、`user_name/section_name` 取系统配置的操作员。

---

## 5. 归集策略最终结论（原 §6 待确认项已解）

- **一单 = 一票（一个提单号）**：`data[]` 数组支持多单批量提交，可按组逐票调用，也可一批多票（`data[0..N]` + 各自 `box[]`）——推荐**逐票调用**，与失败隔离语义一致；
- **一单多箱型**：同提单号下 `40GP+40HQ` → `box[0]`/`box[1]` 两条，**不需要拆单**；
- **一箱一号**：`b_num` 顶层单值，多箱号场景待验证后再定（最坏情况回退为逐箱拆单调用，`split_per_container` 配置位已预留）。

---

## 6. 不确定点与验证实验（按优先级）

| # | 问题 | 验证方法 |
|---|---|---|
| 1 | 费用四通道全空能否成功新增（本轮核心前提） | 最小报文：仅 提单号+箱型+month+type+user_name，shou/pay/duo_get/cost 全部省略 |
| 2 | `b_wharf` 与 `b_start_dock` 语义边界（港区映射目标） | 两字段分别置不同值提交，看 TMS 界面落点 |
| 3 | 一票多箱号时 `b_num` 写法（逗号分隔？重复字段？还是仅首箱） | 一票两箱测试单 |
| 4 | `b` 字段是否后端必需（JSON 双写冗余） | 省略 `b` 只发扁平表单，对比结果 |
| 5 | `type` 枚举全值（进口/倒箱/内装…） | 界面切换类型抓包对比 |
| 6 | `order_num1` 语义（票数/序号） | 多单抓包对比 |
| ~~7~~ | ~~端点 URL 与鉴权~~ | **已解决（2026-08-13）**：复用 `/orders` 通道 + `create_order=true` |

---

## 7. 对既有设计文档的更新点

1. 《字段映射表》§4：必填结论补充为「**提单号=`data[0][b_order_num]`、箱型=`box[N][b_type]`**」，8 家族全覆盖结论不变；
2. 《架构设计》⑥接口适配层：payload 构造器按本规范 §4 实现，**表单编码 + `b` 双写**两个怪癖封装在适配层内部，不外泄；
3. 后续三类（应收/应付/成本）的接入前提是**价格表费目映射**（费目名 → price_id），建议单独立项。
