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

### 2.7 费用四通道（shou/pay/cost 已实证闭环；duo_get 设计上未启用）

> **实证状态（2026-08-15）**：应收 shou[] 已实证（EX26081252/EX26080031）；
> 应付 pay[] 已实证（EX26083664：运费 40.00，回显 pay 数组 + d_yf=40.00）；
> 成本 cost[] 已实证（EX26083665：油费 25.50，回显 supplier 数组 + cb_ids + type=3）；
> 多级应收 duo_get[] 设计上未启用（模板中公司成本通道已注释停用）。
> 实测脚本：.tmp/verify_pay_live.py [pay|cost]（dry-run 默认零网络，JXT_LIVE_TEST=1 建单）

| 通道 | 语义 | 费目结构（以抓包为例） |
|---|---|---|
| `shou[0][费目]` | **应收** | money / lock_status / price_id(价格表ID) / price_type / cost_id / is_profit / dai_dian(代垫) / customer_tax_rate / class_id + `shou[0][note]` |
| `pay[0][费目]` | **应付** | 同上 + `pay[0][note]` |
| `duo_get[0][费目]` | **多级应收/代收** | 上同 + m_id / client_name / car_group / multiple_tare + `duo_get_hj_zj` 合计 |
| `cost[0][费目]` | **成本（供应商）** | 上同 + driver_name + `supplier_hj_zj` 合计、`cb_ids` |

> **四类归集在接口层得到印证**：业务信息（顶层+data+box+driver）/ 应收 shou / 应付 pay / 成本 cost(+duo_get)。
> 关键约束：**费用按「费目名 → price_id」引用 TMS 价格表**（抓包：运费=820、预提费=121744、油费=1134、打劫费=1135）。
> 发送侧统一发射见 payload.py _emit_fees（T13）：每通道每费目 `{channel}[0][{tms_name}]` 六属性键（money/price_id/price_type/is_profit/dai_dian）+ 通道级 note 恒发（缺键 204 拒单，2026-08-13 live 实证）；合计由我方计算回写（driver[0][get_ys_zj]=Σshou、driver[0][pay_yf_zj]=Σpay、cost[0][supplier_hj_zj]=Σcost）；cost 费目条目额外硬读 driver_name（缺键 204 拒单，2026-08-13 live 实证）。缺失费目码由 fee_bootstrap 自举建档（T25）后复用 price_id（单表跨通道通用）。

### 2.8 操作元字段

| 表单字段 | 抓包值 | 说明 |
|---|---|---|
| `user_name` / `section_name` | 章家俊 / 销售部 | 录单人 / 部门（响应回显） |
| `o_id` | 空 | 订单 ID——**空 = 新增，非空 = 更新**（同一接口复用） |
| `appendCost` | true | 追加费用开关 |
| `c_sign` / `yuwu` / `is_bring` / `d_ids` / `cb_ids` | undefined/空 | 签名/业务标记/关联ID，新增场景可空 |

**费用记录结构（2026-08-13 响应实证，测试单 EX26081252，每费目填 1.00）**：

| 结论 | 实证 |
|---|---|
| `price_id` **全局唯一、跨通道通用** | 预提费=121744 同现于 pay/get；油费=1134 同现于 pay/get/supplier → 费目映射表一张即可，不按通道区分 |
| 费用记录 `type` 枚举 | `1`=应收（存 `price[0].get`）、`2`=应付（存 `price[0].pay`）、`3`=成本（存 `supplier` 数组，`driver_id:0`、`car_group:"1"`） |
| 费用记录字段全集 | `cost_id / name / money / price_id / price_type=1 / is_profit=1 / dai_dian=1 / currency=CNY / quantity / unit_price / customer_tax_rate / note` |
| 合计回显 | `b_totle_ys`=应收合计（4×1.00=4.00）、`b_totle_yf`=应付合计——我方计算回写成立 |
| duo_get（多应收） | **仍未验证**：本次测试未填多应收，响应中无对应记录；其双输入框语义与 type 值待补测 |

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
| `customer_name` | ~~`c_name`~~ **待抓包确认** | 【2026-08-13 实证修正】`c_name` 渲染为 UI「**联系人**」字段（军羽 r73 客户名称"锦煦"误入联系人框实证）；「客户」为独立档案选择器字段，其 form 键（`c_title`/`c_id`/`cu_id_list` 候选）待手工填单抓包确认 |
| `customer_contact` | `c_name` | 客户联系人 → UI「联系人」（军羽/123/赢辉无此字段，留空） |
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

## 5. 归集策略结论（2026-08-31 更新：一行一票）

- **【已废止】一单 = 一票（一个提单号）**：原结论按提单号归集合并一票多柜；**2026-08-31 用户拍板改为一行一票**——每条账单数据行独立为一个业务订单，同提单号多行各自成单，TMS 允许同提单号存在多条订单（业务确认）；
- **一单多箱型**：单行内 `40GP*1+40HQ*1` → `box[0]`/`box[1]` 两条，**不需要拆单**；
- **一箱一号**：`b_num` 顶层单值；去重键配套改为「提单号+箱号（行序号兜底）」，见《竞品账单导入接口文档》§4.4。

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

---

## 8. 价格表建档接口（2026-08-14 抓包实证，测试建「测试」费目）

> 用途：**费目不存在时可程序建档拿 price_id**——「其它费」硬依赖的兜底路径。
> **端点（2026-08-14 补）**：`POST https://s3.jxt56.com/Car/CarPrice/AddCarPrice`

### 8.1 请求字段（form-data）

| 字段 | 抓包值 | 逆推含义 |
|---|---|---|
| `name` | 测试 | 费目名（= 订单四通道 `{channel}[0][{name}]` 的键） |
| `sn` | CS | 费目编码 |
| `inout` | 3 | 进出口适用（推测 1=出口/2=进口/3=通用，待验证） |
| `is_get` / `is_pay` | on / on | 可用于应收 / 可用于应付（checkbox，on=启用） |
| `price_type` | 1 | 价格类型（与费用记录默认值一致） |
| `is_profit` | 1 | 计利润 |
| `status` | 1 | 启用状态 |
| `dai_dian` | 2 | 代垫标记（注意：建档=2，费用记录里=1，语义/取值域不同） |
| `bao_zhang` | 1 | 包账标记 |
| `od` | 0 | 排序号 |
| `classification_name` / `class_id` | 运费 / 4612 | **费类名 + 费类 ID**（费类是独立档案表：运费类=4612） |
| `rate` | 空 | 税率（空=无） |
| `use_imprest` | 0 | 备用金标记 |
| `expense_rate` | 5 | 费用率（%） |

### 8.2 响应（`code:200` + `添加成功`）

| 回值 | 值 | 说明 |
|---|---|---|
| `data.price_id` | **124823** | **新建费目的 price_id——建档即得映射表所需 id** |
| `data.is_supplier` / `is_driver_supplier` | 0 / 0 | 供应商/司机供应商标记（默认 0） |
| `data.is_other` | 0 | **「其它」标记存在** → 其它费可能是带 `is_other=1` 的专属费目，建档时探明 |
| `data.add_time` | 2026-08-14 09:32 | 建档时间 |
| 回显 | 全字段 | 请求字段全量回显（含 car_id/cu_id 等会话残留值） |

### 8.3 衍生物

- 本次测试建档产物「测试」price_id=124823 为**脏数据**，建议测试环境删除，避免污染价格表与下拉框；
- 建档接口 + 已有 5 个实证 id → **全量费目可程序化建档/补齐**，但批量建档前仍需拿到费类表（class_id ↔ classification_name 全集），获取方式同价格表（outerHTML / 导出 / 客服）；
- 建档字段与费用记录字段高度同构（price_type/is_profit/dai_dian/rate↔customer_tax_rate），可复用同一套默认值配置。

### 8.4 自举链路实施状态（2026-08-14 合入，实施 prompt「费目自举」T24-T26）

> 代码落地：`fee_bootstrap.py`（编排）+ `fee_registry.py`（注册表）+ `fee_price_map.py` 解析顺序四级改造。

| 项 | 结论 | 实证 |
|---|---|---|
| 解析顺序 | **YAML 显式 id → registry（自举产物）→ 自举创建 → 降级 skip_report**；YAML 优先（人工修正压过自动产物） | 单测 TestResolutionOrder |
| 懒创建 | 只建「真实导入中命中且解析为 null」的码，不批量预建 | 秋怡 2019 实测建 14 码（other/yangshan/pre_inport/drop_box/damage_box/amend/waiting/port_misc/inspect/overdue/weigh/move/overweight/tally），`tax`（import:false）不建 |
| 幂等 | registry 命中即复用，不重发建档；同批同码只调一次 | 单测 TestIdempotent |
| 失败降级 | 建档失败（非 200/取不到 price_id）→ 当批该码全部 skip_report + 报告「自举失败」清单；registry 不记失败，下批重试 | 单测 TestFailureRetry |
| **preview 零副作用** | create_order=false 只输出「计划创建清单」（planned）**不发建档请求**（与阶段三 preview 只读语义一致）；真实导入（create_order=true）才建档 | 单测 TestPreviewReadOnly；golden preview=planned+dropped 非零 / create=dropped 归零 |
| 环境开关 | `fee_bootstrap.enabled`：test=true / prod=false（生产由运维管控，自举需显式开启） | config/fee_price_map.{env}.yaml |
| is_other 特判 | `other` 码建档额外发 `is_other: "1"`（§8.2 实证 is_other 字段）；创建后需人工在 UI 确认归类 | 单测 TestIsOther |
| 当批回填 | 建档成功 → registry 登记 → 同批 apply_price_map 命中回填 → payload 正常发射 | golden：秋怡 create 模式 dropped 1901 → 0 |

- 建档 form 复用 `master_data.endpoints.price_create`（endpoint_key 引用，不重复配 URL），凭证链路复用建档族（sk 体系）；
- 注册表存储 `{storage_dir}/fee_registry.json`（JSON + 进程锁 + 原子写，复用 master_data_store 模式）；
- 遗留：费类表（class_id ↔ classification_name 全集）未到位，`class_id` 暂统一 4612（运费类），其它费类待费类表到位后细分。

---

## 9. 客户建档接口（2026-08-14 抓包实证，建测试客户「test」）

> 阶段三阈值建档的客户侧依赖。
> **端点（2026-08-14 补）**：`POST https://s3.jxt56.com/Car/CarClient/AddCarClient`

### 9.1 请求字段（form-data，按语义分组）

| 分组 | 字段（抓包值） | 逆推含义 |
|---|---|---|
| 主体 | `client_name`(test) / `sn`(TEST) / `sys_type`(1) | 客户名 / 编码 / 系统类型 |
| 分组 | **`cg_name`(同行) / `cg_id`(4)** | **客户分组名 + 分组 ID**（⚠️ 与订单响应中费用记录的 `cg_id`=客户id 语义疑似不同，见 §9.3） |
| 联系 | `client_address`(测试地址) / `e_mail` / `qq` / `to_wx_id` / `client_bei`(test) | 地址/邮箱/QQ/微信/备注 |
| 联系人 | `data[0][n]`(zzz) / `data[0][p]`(11111111111) | **联系人数组：n=姓名、p=电话**（响应原样回显为 data 数组） |
| 商务 | `tax_rate`(0) / `account`(10) / `accounting_start_time`(2) / `contract_expiration_date` / `payment_type` / `quota` / `low_profit_value` / `low_profit_margin` | 税率 / 账期天数 / 起算日 / 合同到期 / 付款方式 / 额度 / 低利润阈值 |
| 归属 | `su_name`(章家俊) / `su_id`(15478) / `verify` / `forwarder_name` | 业务员名+ID / 审核 / 指定货代 |
| 订阅 | `like[dh]`(on) / `like[o]`(on) | 订阅开关（dh/o 待解） |
| **随建价格** | `price_type`(2) / `price_item`(出车费) / `price_group`(运费) / `class_id`(4612) / `price_rate`(10) / `profit_type`(1) | **建档同时携带一条客户价格记录**（费类表与价格表建档同源：运费=4612） |

### 9.2 响应（`code:200` + `添加成功`）

- **`data.client_id` = 169642** —— 新建客户档案 ID（订单侧 `c_id`/`cg_id` 回填目标）；
- `verify=1` / `status=1` / `sync_status_client=1` 默认态；`img_data=[]`、`multiple_tare=[]` 空数组；
- 开户/税务/收款字段全集回显（company_name/taxpayer_number/bank_* / receive_* / payee_* 等，均 null）——**最小建档可全省略**；
- `add_time` 为格式化字符串（"08-14 11:16"）。

### 9.3 衍生物

- 阈值建档时的**最小字段集**（建议）：`client_name` + `sn` + `cg_id`/`cg_name`（分组必填？待验证）+ `su_id` + `data[0][n/p]`；价格字段（price_*）**不随建档提交**，避免污染客户价格表——本次抓包的「出车费 rate=10」即脏数据，建议删；
- `cg_id` 双重语义待验证：建档请求中 = 客户分组(4=同行)；订单响应费用记录中 = 客户 id(166328)；
- 脏数据：测试客户 client_id=169642（test/TEST）建议删除。

---

## 10. 委托人（发货方）建档接口（2026-08-14 抓包实证，建「test」）

> 阶段三阈值建档的委托方侧依赖。**端点仍缺**（其余五个 2026-08-14 已补；委托人为本阶段只计数不建档，不阻塞——补抓：委托人界面保存请求，路径推测形如 `/Car/CarBailor/AddBailor`，以实测为准）。

| 请求字段（值） | 逆推含义 |
|---|---|
| `bailor_title`(test) / `sn`(TEST) | 委托人名 / 编码 |
| `name`(zzz) / `tel`(11111111111) / `address`(test) | 联系人 / 电话 / **地址**（单值，非数组） |
| `code6`(tttt) / `out_car_id`(空) | 编码6（语义待解）/ 外部车辆 id |
| `account_bank`(测试银行) / `account_name` / `bank_number` / `wxid` | 银行账户信息 |
| `is_involve`(1) / `is_quoted`(0) / `payment_type` / `note`(test) | 参与标记 / 已报价 / 付款方式 / 备注 |
| `sys_type`(1) | 系统类型 |

**响应**：`code:200` + `data.bailor_id`=**159132**；`add_time` 为 **unix 时间戳字符串**（"1786677443"，与客户建档的格式化串不一致——各建档接口响应风格不统一）；会话残留 `car_id`/`cu_id` 照例回显。脏数据 bailor_id=159132 建议删。

---

## 11. 司机建档接口（2026-08-14 抓包 + live smoke 双重实证）

> 阶段三依赖。**司机记录上直接带车牌**（`num`=沪GF6068），并关联 `truck_id`（车辆档案）——司机是建档依赖链的末端（车辆 → 司机）。
>
> **⚠️ 端点修正（2026-08-14 生产界面抓包，推翻早前 AddDriverGroup 方案 A）**：
> 正确端点是 **`/Car/CarDriver/AddCarDriver`**（与客户/工厂/车辆同族命名）。实测推翻三条推断：
> 1. **`bailor_title` / `bailor_id` 可空**——抓包提交空值成功（早前 AddDriverGroup 端点的
>    「bailor_title 必填 / bailor_id 为 dm_car_driver_group 表主键且唯一」不适用于本端点）；
> 2. **`num`（车牌）必填**——缺省拒单 `请重新选择车牌`；
> 3. **`phone`（手机）必填**——缺键 → 500 `Undefined index: phone`（CarDriver.php 硬读），
>    空串 → 拒单 `no: phone`；
> 4. **成功响应带主键 `data.id`**（294329/294333）——可登记幂等，不再存在「无主键无法登记」问题。
>
> **结论（2026-08-14 定论）**：司机建档正常启用（config/master_data.{env}.yaml `driver_create`，APP_ENV 选择 test/prod 双份随镜像分发），
> 无车牌/无手机号司机 → 登记 skip_archive 终态不建档（数据缺失，TMS 硬约束）。

| 分组 | 字段（值） | 说明 |
|---|---|---|
| 主体 | `name`(test) / `sn`(TEST) / `phone`(11111111111) / `sinout`(自做) | 司机姓名 / 编码 / **手机（必填，空串拒单 no: phone）** / 自做或外协 |
| **关联** | **`num`(沪DD8780) / `truck_id`(290521)** | **车牌（必填，缺省拒单「请重新选择车牌」）+ 车辆档案 id（建档顺序前置）**；bailor_title/bailor_id 可空（不发送） |
| 证件 | `card_num`+`card_validity` / `driviers_num` / `license_valid_date_start/end` / `d_validity` / `cycer`+`cycer_validity` | 身份证 / 从业资格证（TMS 原拼写 driviers）/ 驾照有效期 / 行驶证有效期 |
| **日期双写** | `license_valid_date`="2026-08-18 - 2026-09-30" / `valid_date`（range 串） | 拆分字段 + 区间串**同时提交**（与订单 `b` 字段同套路，照抄） |
| 其他 | `employment_time` / `note` / `imgFile` 各图片位（空） / `type`(空) / `sys_type`(1) | 入职日期 / 备注 / 证件照（可空） |

**响应**：`code:200` + 主键回值键名是 **`data.id`**（294329，不是 driver_id）；关联 id 原样回显。

## 12. 车辆建档接口（2026-08-14 抓包实证）

| 分组 | 字段（值） | 说明 |
|---|---|---|
| 主体 | **`num`(沪C00000)** / `t_id`(TR543534) / `vehicle_owner`(test) / `sys_type`(1) | 车牌 / 挂车或车型 id（语义待解，可空验证）/ 车主 |
| 证件 | `driving_num`+`d_validity` / `operation_num`+`operation_validity` / `carfnum` / `engine_num` | 行驶证 / 营运证 / 车架号 / 发动机号 |
| 保险 | `jqrisk`+`jqrisk_validity` / `buri_num`+`buri_validity` | 交强险 / 商业险 |
| 归属 | `remind_user`(章家俊) / `remind_id`(15478) / `section_id`(6987) / `section_name`(销售部) | 提醒人 / **所属部门 id**（配置项） |
| 参数 | `oil`(5) / `moop`(3) / `coefficient`(1111) / `rent_car_price` / `type`(bill) | 油耗 / 载重?（语义待解）/ 系数 / 租车价 / 来源标记 |
| 图片 | `driving_card` 等 7 个图片位（空） | 可空 |

**响应**：`code:200` + `data.truck_id`=**297329**；`is_complete:1`；`pimg:[]`。

## 13. 工厂地址建档接口（2026-08-14 抓包实证）

| 分组 | 字段（值） | 说明 |
|---|---|---|
| **归属客户** | **`client_id`(169642) / `client_name`(test)** | **必须先有客户档案**——依赖链关键 |
| 主体 | `name`(test) / `sn`(TEST) / `address`(测试地址) / `address_search` | 工厂名 / 编码 / 地址全文 / 地址搜索串 |
| 区划 | `province`/`city`/`district`/`town`（天津市/天津市/南开区/向阳路街道）/ `adcode` / `jing_du`/`wei_du` | 四级区划；**adcode 可由区划推导**（南开区→120104）；经纬度可空 |
| 业务 | `km`(10000) / `km_get` / `km_back` / `bei`(11111111111) / `box_bei`(1111) | 里程 / 提箱里程 / 还箱里程 / 备注电话? / 箱备注（语义粗解，建档可空） |

**响应**：`code:200` + `data.factory_id`=**1881303**（与订单 EX26080031 的 factory_id=1866618 同档案系）；默认组 `dispatch_level:1 / working_hours:2 / assemble_type:1 / appoint:1 / cost_checked:1`；联系人以 JSON 串回显在 `data` 字段。

## 14. 建档接口族总结（6 接口同构）

| 档案 | 主键回值键 | 前置依赖 | 最小字段集（防污染） |
|---|---|---|---|
| 价格费目 | `price_id` | 费类 class_id | name / sn / class_id / is_get / is_pay |
| 客户 | `client_id` | 分组 cg_id | client_name / sn / cg_id / su_id / data[0][n/p] |
| 委托人 | `bailor_id` | — | bailor_title / sn / name / tel / address |
| 车辆 | `truck_id` | section_id（部门） | num / sn? / section_id / remind_id |
| 司机 | `id` | truck_id（车辆档案） | name / phone / num(车牌) / sn / sinout（num/phone 必填） |
| 工厂地址 | `factory_id` | **client_id**（⚠️ 实测成功响应可能 data:[] 无主键，按「已建档无 id」登记不再重试） | client_id / name / sn / address / 四级区划 |

**建档顺序（依赖图）**：客户 → 工厂地址；车辆 → 司机；委托人 → 司机（可选）；价格费目独立。
**共性**：form-data + `code:"200"`；日期区间双写；会话残留字段回显无意义；响应日期格式不统一（格式化串/unix 串/区间串并存）——解析按主键存在性取值，不做格式假设。

**端点（2026-08-14 补齐 6/6）**——⚠️ **建档接口族在 `s3.jxt56.com`，与订单前端 `tms.jxt56.com` 不同宿主**（CORS allow-origin=tms.jxt56.com；allow-headers 含 `sk`/`refresh-sk`，鉴权同构）：

| 档案 | 端点（POST） |
|---|---|
| 价格费目 | `https://s3.jxt56.com/Car/CarPrice/AddCarPrice` |
| 客户 | `https://s3.jxt56.com/Car/CarClient/AddCarClient` |
| 司机 | `https://s3.jxt56.com/Car/CarDriver/AddCarDriver`（2026-08-14 实证修正：非 DriverGroup；司机=司机+车牌实体，与「司机+车牌组合计数」口径互证） |
| 车辆 | `https://s3.jxt56.com/Car/CarTruck/AddCarTruck` |
| 工厂地址 | `https://s3.jxt56.com/Car/CarFactory/AddCarFactory` |
| 委托人 | **仍缺**（本阶段只计数不建档，不阻塞） |

**实现注意**：client 须支持建档族用独立 base host（或 endpoints 直接配全量 URL）；鉴权头（sk 体系）复用前先用一个端点实测连通。
