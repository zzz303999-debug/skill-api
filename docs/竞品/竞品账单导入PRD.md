# 竞品账单导入 PRD

## 1. 文档信息

| 项 | 内容 |
|---|---|
| 产品名称 | 单据智能抽取服务（skill-api）· 竞品账单导入 |
| 文档版本 | v1.0 |
| 编制日期 | 2026-08-17 |
| 事实基准 | 代码实况（app/orders/bill/，feature/bill-import）为主，文档冲突时以代码为准 |
| 关联文档 | [竞品账单导入接口文档](./竞品账单导入接口文档.md)（v1.2，接口级契约）；[竞品账单解析-TMS业务订单接入设计](./竞品账单解析-TMS业务订单接入设计.md)；[标准业务订单模型-字段映射表](./标准业务订单模型-字段映射表.md)；[TMS业务订单新增接口-逆推规范](./TMS业务订单新增接口-逆推规范.md)；[费目映射表](./费目映射表-标准费目码-price_id.md)；[阶段三-基础资料阈值录入设计](./阶段三-基础资料阈值录入设计.md)；[上线前验收清单](./上线前验收清单-三阶段总.md) |

### 修订记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-08-17 | 初稿：按代码实况归纳三阶段（业务信息/费用/基础资料）全量需求 |

## 2. 背景与目标

### 2.1 背景

金科信等竞品系统导出的历史应收对账单（`.xls` / `.xlsx`，单文件可达数千行）需要导入 TMS。账单存在**表头逐年漂移**（19 个表头签名 ≈ 8 个模板家族）、**双行表头同名费用冲突**、**一票多柜**、**日期缺年份**、**脏数据多样**（浮点尾巴/公式/中文大写金额）等特征，无法直接平移导入。

### 2.2 产品一句话

竞品 Excel 账单解析后自动生成至 TMS：**四类归集**——业务信息/财务信息 → 业务订单新增；基础信息 → 客户/门点/司机/车辆建档；费用栏目 → 费用管理新增。

### 2.3 目标

1. 上传一份竞品账单，自动完成解析、归集、字段映射，产出可直接人工核对或直接创建订单的结构化结果
2. 提供**预览 → 人工确认 → 逐单创建**的完整导入闭环
3. **模板配置驱动，零竞品硬编码**：新增模板家族只加 YAML 配置，不改代码
4. 费用四通道（应收/应付/成本/多级应收）随单录入，费用恒等对账可审计
5. 基础资料（客户/工厂/司机/车辆）按出现次数阈值筛选建档，不无条件自动建

### 2.4 范围

| 范围 | 内容 | 状态 |
|---|---|---|
| 阶段一 | 业务信息线：解析 → 归集 → 预览/创建业务订单 | 已实施 |
| 阶段二 | 财务信息线：费用四通道映射、price_id、费目自举、对账报告 | 已实施 |
| 阶段三 | 基础信息线：阈值计数 + 依赖序建档 + 订单回填 | 已实施 |
| 不在范围 | 应付账单 xlsx（集行格式）；`type` 枚举全值；多箱号 `b_num` 落点；TMS 查重接口（方案二） | 见 §11 |

### 2.5 核心原则

1. **默认不自动下单**：`create_order` 缺省 `false`，调用方显式传 `true` 才创建
2. **一票一单**：同提单号（或业务编号）的多柜合并为一个业务订单
3. **零静默错误**：关键字段缺失不阻塞接口返回，必须经 `missing_fields` / `missing_reasons` 返回未提取到信息
4. **不自动重试**：下单/建档请求不自动重试，防重复；失败由调用方处理后重提
5. **原文保真**：提单号、客户名、箱号等逐字复制，不做智能改写
6. **只报告不拦截**：对账差异、建档失败、未达阈值均进报告，不使订单丢失
7. **preview 零副作用**：预览模式不触达任何注册表读写与下游建档/下单请求

## 3. 名词解释

| 术语 | 说明 |
|---|---|
| 竞品系统 | 金科信等第三方货代系统，导出历史应收对账单 |
| 应收对账单 | 竞品系统按结算周期导出的账单，一文件一个结算周期，每柜一行 |
| 结算区间 | 账单抬头标注的结算日期范围，用于日期补年份 |
| 提单号（bl_no） | 订单号（`order_num1` / `data[0][b_order_num]`）；≥8 位字母数字，纯字母不算 |
| 一票多柜 | 同一提单号对应多个柜子，账单中表现为多行（2026-08-31 起**不再合并**，见「一行一票」） |
| 一行一票 | 每条账单数据行独立为一个业务订单（2026-08-31 用户拍板；同提单号多行各自成单） |
| 归集 | 数据行 → 业务订单的组装过程；2026-08-31 起为一行一票（不再按键合并多行） |
| 模板家族 | 同源竞品的账单模板集合；表头逐年漂移但结构同族 |
| L1/L2/L3 | 模板识别三级：指纹精确命中 / 族级近似匹配 / AI 表头映射+人工固化 |
| AddWork | 下游 TMS 业务订单新增接口（`s3.jxt56.com/Car/WorkOut/AddWork`） |
| 预览模式 | 只解析不下单，返回归集结果与缺失清单 |
| 创建模式 | 解析后逐单创建订单，同时返回逐单创建结果 |
| 费目 | TMS 价格表中的费用条目（运费/待时费/预提费…），以 price_id 引用 |
| 四通道 | 费用四条数据线：shou（应收）/ pay（应付）/ cost（成本）/ duo_get（多级应收） |
| 档案建档 | 调用 TMS 建档接口族创建基础资料档案（客户/工厂/司机/车辆/费目） |
| 成功单注册表 | 提单号+箱号（行序号兜底）→ 首次创建回执的去重登记（imported_orders.json） |

## 4. 用户角色与使用场景

| 角色 | 说明 |
|---|---|
| 调用方（客户系统/集成方） | 上传账单文件，先预览核对、再创建；处理失败单重提 |
| 运维 | 维护模板 YAML、费目 price_id、建档端点等配置；清脏数据、重置注册表 |
| 业务 | 人工确认 L3 新模板映射结果；确认 TMS 档案/费用落位 |

典型场景：

1. **历史账单批量迁移**：逐份导入历史对账单，重建历史业务订单
2. **导入前预览核对**：先预览，核对归集结果与缺失字段，再正式创建
3. **直连创建**：数据质量有信心的账单直接创建，一次性逐单下单
4. **失败单补救**：个别订单下游拒绝/超时，调用方按逐单错误修正后重提（失败单不登记去重，重导不被误拦）
5. **新竞品接入**：上传新模板 → L3 AI 映射 → 人工确认 → 固化为 YAML → 二次上传 L1 直接命中（代码改动量 0）

## 5. 功能需求

### 5.1 文件上传与格式校验

- 接口：`POST /orders/bill/import`，`multipart/form-data`，参数 `file`（必填）+ `create_order`（bool，缺省 false）
- 支持 `.xls` / `.xlsx` / `.xlsm`，大小上限 20 MB
- 按文件内容 magic bytes 识别真实格式（不信任扩展名）；扩展名与内容不符 → 400 `file_format_mismatch`
- 空文件 / 损坏 / 加密文件必须拒绝并给出中文错误说明（400 / 422）
- 解析在服务端线程池执行（并发上限 4，可配），满负荷排队超 10 秒返回 503

### 5.2 模板识别（三级漏斗）

- **L1 指纹精确命中**：表头行非空单元格去空白后以 `¶` 连接，md5 前 8 位与模板 `match.fingerprints` 比对，直接使用模板配置
- **L2 族级近似**：列名集合与模板期望列名集合重合度 ≥ `family_min_overlap`（默认 0.85），应对同家族表头逐年漂移；缺失列记 missing 不报错
- **L3 AI 映射**：L1/L2 未命中时，LLM 只读表头区（前 15 行 × 全部列，每格截断 20 字符）输出列映射；映射结果必须过**四道校验闸门**：
  - a. 必映射字段齐全（bl_no + box_type_qty）
  - b. 字段不重复映射
  - c. 费用目标在白名单内，白名单外降级 ignore 并上报
  - d. 抽样校验（提单号列非空前 20 行样本，匹配率 ≥50%；费用列数字率 ≥80%）
  - 闸门不过 → 400 `header_mapping_rejected`（details 含 AI 映射/失败原因/表头原文）
- LLM 不可用/响应非法 → 回退精确匹配（表头同时含「客户编号」「提单号」两列），找不到照旧 400
- L3 通过的候选配置随响应 `meta.l3_template` 返回，人工确认后固化为 `templates/{family}_v1.yaml`，同指纹再次上传直接 L1 命中
- 旧 sha1 指纹固化模板（storage/bill_templates/*.json）继续可用，兼容历史

### 5.3 配置驱动解析

模板配置（YAML）声明六项，全部声明式、无 Python 代码：

| 配置项 | 说明 |
|---|---|
| `match` | fingerprints / family_min_overlap |
| `header` | row_anchor（表头行定位锚点）/ two_row（双行表头）/ section_fill |
| `data` | row_filter（seq_numeric：序号列为数值才是数据行）/ stop_on（遇「合计」等终止） |
| `columns` | 标准字段 ← 源列名；支持「区块.列名」「列名#N」同名消歧 |
| `normalizers` | 按字段名引用全局归一化函数（date_flex/strip_float_tail/box_parse/vessel_voyage_split/to_int/to_number） |
| `fee_boundary` | 费用区边界三模式：section_header / total_columns / column_range |

- 代码中禁止出现任何竞品名与竞品专属列名分支；金科信模板（jinxin_v1）与其他家族走同一条代码路径
- 双管线分流：模板 columns 目标 ⊆ BillRow 字段 → 旧语义管线（兼容既有金科信流程）；目标为标准字段（CanonicalOrder）→ TMS 标准管线。旧语义订单经转换进入同一 TMS 通道

### 5.4 归集（一票一单）

- 归集键配置化：`group_key.primary`（默认 bl_no），primary 缺失回落 `fallback`（biz_no）；双键都缺失的行单独成组
- `box_groups[]`：同箱型数量累加；**箱型不做格式校验，非空即合法**（40HQ/20GP 等标准箱型与 大冷/飞翼车/拼箱/2X20 等非标表述均正常归集），仅列为空才标记缺失
- `containers[]`：同组每行一箱（箱号/箱型/封条号），保持行序
- 单值字段取组内首行非空；账期 `month`（YYYY-MM）由 order_date 优先、work_date 回退派生
- 必填缺失（bl_no / box_groups / customer_name）登记 `missing_fields`，**不阻塞、不拦截**

### 5.5 日期补年份

- 账单内日期常为月-日（如 `1-27`），年份从 `year_source`（结算日期行优先、文件名兜底）推断
- 跨年区间（如 2018-01~2019-12）：M ≤ 6 归结束年、M > 6 归起始年（`12-7`→2018-12-07、`1-5`→2019-01-05）
- 支持完整日期、带时间串、datetime 对象三种形态；无法推断 → 字段置 null（日期为非必填，缺失不标记）

### 5.6 字段映射与缺失处理

- 标准订单模型（CanonicalOrder）分三档：核心（bl_no/box_type_qty/customer_name/work_date/port_area/pickup_point/biz_type）、常见（biz_no/order_date/customer_no/customer_contact/contact_person/contact_phone/door_point/load_address/return_point/seal_no/vessel/voyage/ship_date/discharge_port/delivery_place/plate_no/driver_name/driver_phone/fleet/pieces/gross_weight/remark）、扩展（io_type/port_open_time/port_cut_time/port_in_time/shipping_company/cargo_name/load_point）
- 必填项（`order_num1`/`c_title`/`box[]`，旧管线口径；`bl_no`/`box_groups`，标准管线口径）未提取到时 `order_data` 对应字段置 **null**（不用空串/0 填充），并在 `missing_fields`/`missing_reasons` 返回中文原因（`原文未找到`/`格式不合法`）
- **本服务不拦截、不判断**：缺失单在创建模式下照常提交，由调用方决定是否放行、由 AddWork 端校验决定成败

### 5.7 预览模式（create_order=false）

- 只解析归集，返回 `file` / `bill_period` / `total_rows` / `order_count` / `orders[]`（含 order_data/missing_fields/missing_reasons）/ `meta`（文件 SHA-256、字节数、解析时间、引擎、模板、未识别列、对账报告、L3 候选模板）
- **零副作用**：不读写成功单注册表、不计数、不建档、不发下游请求
- 人工修正缺失字段的唯一方式：修改文件后重新导入

### 5.8 创建模式（create_order=true）

- 下游链路（v1.4，2026-08-19）：调用方登录 TMS 获取 token → 请求头 `sk` 透传 → `AddWork × N`（逐单串行；建档/费目自举同用）；服务端不再换取凭据
- create 模式缺 `sk` 头 → 400 `bad_request`（不进入解析/下单流程）；sk 无效由下游判定 → 该单 error 透传；preview 零下游调用不要求
- **下单通道**：AddWork 端点 + `sk` 头 + `create_order=true` + form-data 展平字段（2026-08-13 实测可直连下单）；嵌套 JSON 通道（publishCreateOrder）已弃用（强制要求 userId+roomId，204 拒单）
- payload 规则：`o_id` 空 = 新增；`type` 固定 1（仅确认出口枚举）；`b` 字段 JSON 双写实测非必需，不再发送；客户 `c_title`=客户名称、`c_name`=客户联系人；`c_id` 恒发（缺键 204 拒单）；箱号 `b_num` 只写首箱（多箱号落点未定，warning 提示）；费用有值的通道整段发射、通道级 note 恒发、合计由我方计算回写
- 单失败隔离：某单失败不影响后续订单，失败原因完整透传 `create_result.error`（含 code/msg/响应原文）
- 响应附 `summary`（total/success/failed/skipped/created/success_sns/failed_details）与 `upstream`（成功单原始回显）

### 5.9 重复上传去重

- **同一「提单号+箱号」对同一 sk 只允许创建成功一次**（成功单注册表 `storage/imported_orders.json`；一行一票配套，2026-08-31 起）
- **去重键**：`提单号|箱号`；行无箱号时兜底 `提单号|#行序号`（无箱号模板同号多行各自成键）；箱号/序号均缺退化为纯提单号（历史行为）
- 只登记创建成功单；失败单不登记，修正后重导失败单照常创建
- 键规范化：两段各自 strip 首尾空白 + 统一大写
- 并发：组合键锁包住「查重→提交→登记」临界区，同键跨请求串行化
- create 模式逐单创建前先查注册表，命中 → `create_result = {success:true, skipped:true, sn:<首次创建>}`，不调下游
- preview 模式零读写；清除方式：删除注册表文件全量重置

### 5.10 费用四通道（阶段二）

- 费用列发现配置化（三种 fee_boundary 模式），区块感知（双行表头家族「应收.运费」≠「应付.运费」）
- 费目归一三级解析：模板 `fees.mapping` → 全局 `config/fee_alias_dictionary.yaml` → unmapped_fee 策略（to_other 归并为其它费+原名进备注 / skip_report 不生成记录）
- price_id 映射四级解析：YAML 显式 id → registry（自举产物）→ 自举建档 → 降级 skip_report；YAML 与 registry 冲突时 YAML 优先
- **费目自举**：真实导入中命中且无 price_id 的费目码懒创建（AddCarPrice 建档）→ registry 登记 → 当批回填；幂等、失败下批重试、preview 只输出计划清单；`enabled` 按环境配置（test=true / prod=false）
- 导入规则：同费目多行金额累加；0/空不生成记录；`import:false` 项（如税金）仅对账不录入；负向扣减项按配置取负录入；「其它费」建档带 `is_other=1` 特判
- payload 发射：每通道每费目 `{channel}[0][{tms_name}]` 六属性键（money/price_id/price_type/is_profit/dai_dian）+ 通道级 note 恒发；cost 费目条目硬发 driver_name；合计回写 driver[0][get_ys_zj]/driver[0][pay_yf_zj]/cost[0][supplier_hj_zj]；excluded 项不录入、不进合计
- duo_get（多级应收）设计上未启用（模板中公司成本通道注释停用）

### 5.11 费用对账报告

- 恒等校验：`bill_total − recorded_total = excluded_total`，容差 0.01；超差按单列条目（单号、通道、三口径值），**只报告不拦截**
- 无锚点（账单侧无合计列）不算 mismatch（no_anchor 语义）
- 报告含：price_id 降级清单、unmapped skip 逐行计数、金额解析失败清单、to_other 原名计数（≥阈值 warning）、费目自举报告
- 账单自带锚点（合计行/总箱型箱量/合计大写）校验归集结果，同样只报告不拦截

### 5.12 基础资料阈值建档（阶段三）

- **阈值筛选**：客户/工厂/司机/车辆累计出现 ≥ N 次（N 默认 5，配置 `master_data.threshold`）才调用 TMS 建档接口；未达阈值订单照常带文本提交，仅报告标注「未建档(x/N)」
- 计数键：客户=归一名（全角→半角+去全部空白）；工厂=「名+地址」复合键；司机=「司机名+车牌」组合键（同人换车/同车换人算不同档案）；跨全部模板家族全局累计（单文件 JSON 持久化）
- 建档依赖序：客户 → 工厂；车辆 → 司机；委托人只计数不建档；费目建档走自举管线
- 当批建档、当批回填：达阈值后订单 `c_id`/`factory_id` 等档案主键回填 payload；未建档订单零变化
- 同批去重：建档调用每键只发一次；建档失败保持计数、下批重试、进报告
- 终态登记：TMS 已存在（204 已存在拒单/成功无主键）→ `exists_external` 不再重试；数据缺失永久不可建（司机无车牌/无手机，TMS 硬约束）→ `skip_archive` 不再重试
- 任一档案类端点 TODO/未配置 → 该档案类降级为只计数不建档（不 fail fast）
- **preview 只读**：不计数、不建档，报告展示当前计数状态

### 5.13 审计与溯源

- 请求/响应自动写入访问日志（含文件哈希、订单数、逐单创建结果），`/logs` 页面可查
- 响应 `meta`：文件 SHA-256、字节数、解析时间、解析引擎、行数、模板、未识别列、对账报告、基础资料报告
- 成功单注册表记录 sn + 来源文件哈希，可溯源

## 6. 非功能需求

### 6.1 性能与容量

| 项 | 值 |
|---|---|
| 单文件大小上限 | 20 MB，超限拒绝不截断 |
| 单次导入订单数建议 | ≤ 2000 单，超出建议拆分文件 |
| 创建模式耗时 | 分钟级（逐单串行），调用方需放宽客户端超时 |
| 解析并发 | 4（可配），排队超 10 秒 503 |

### 6.2 限流

- heavy 档（与 /orders 同档）：默认 60 秒内每 IP 10 次
- 内网 IP 可配白名单豁免；被限流请求仍写访问日志

### 6.3 安全

- 接口鉴权：Bearer Token 或 X-API-Key（生产必须开启）
- 下游凭据（ext_app_id/ext_user_id/jxt_open_id）只配服务端环境变量，不随请求传入、不写响应
- 错误不泄露内部实现：description 为可展示中文，原始响应仅在 details 按需透传

### 6.4 可用性与部署约束

- **必须单进程单 worker 部署**：成功单注册表、费目注册表、基础资料计数均依赖进程内锁 + 单文件 JSON；多 worker/多实例需换共享存储
- 单笔订单创建失败不阻塞整批（失败隔离）
- 建档/自举任何失败不使订单丢失（降级语义）

## 7. 接口契约概要

完整契约见 [竞品账单导入接口文档](./竞品账单导入接口文档.md)（v1.2）。

| 项目 | 值 |
|---|---|
| 路径 | `POST /orders/bill/import` |
| 内容类型 | multipart/form-data |
| 参数 | `file`（必填）/ `create_order`（bool，缺省 false） |
| 鉴权 | 需要（Bearer Token 或 X-API-Key） |
| 限流 | heavy（60 秒内 10 次） |
| 响应 | file / bill_period / total_rows / order_count / create_order / orders[] / canonical_orders[] / summary（仅 create）/ upstream（仅 create）/ meta |

## 8. 配置项

| 配置 | 文件 | 说明 |
|---|---|---|
| 模板家族 | `templates/*.yaml` | 8 家族模板 + 别名字典；新增家族只加配置 |
| 字段别名字典 | `templates/alias_dictionary.yaml` | 标准字段 ← 源列名别名（L3 前道） |
| 费目别名字典 | `config/fee_alias_dictionary.yaml` | 费目名 → 标准费目码 |
| price_id 映射 | `config/fee_price_map.{env}.yaml` | 按环境隔离；缺文件 fail fast；含 fee_bootstrap 段 |
| 基础资料 | `config/master_data.yaml` | threshold / sn_prefix / endpoints（6 建档接口）/ defaults / duplicate_markers |
| 下游凭证 | 无（v1.4：服务端不再配置/换取，调用方登录 TMS 后经请求头 `sk` 透传） | — |

## 9. 错误码

| HTTP | 错误码 | 触发场景 |
|---|---|---|
| 400 | `bad_request` | 扩展名不支持、找不到含「客户编号」「提单号」的表头、空文件 |
| 400 | `file_format_mismatch` | 扩展名与内容格式不符 |
| 400 | `file_too_large` | 超过 20 MB |
| 400 | `empty_file` | 空文件 |
| 400 | `header_mapping_rejected` | L3 AI 映射未过校验闸门 |
| 413 | `payload_too_large` | 请求体超限 |
| 422 | `convert_error` | 文件损坏/加密无法打开 |
| 502 | `order_upstream_error` | 凭证获取失败或下游系统级拒绝 |
| 429 | `rate_limited` | 触发 heavy 限流 |
| 401 | `unauthorized` | 凭证缺失/错误 |
| 503 | `server_busy` | 并发任务满，排队超时 |
| 500 | `internal_error` | 服务内部错误 |

统一错误结构：`{error: {code, message, description, details}}`。账单内字段缺失**不报错**（200 + missing 信息）；单笔创建失败**不触发**整请求错误。

## 10. 验收标准

1. **回归基线**：全量 pytest 全绿（当前基线 788 passed / 8 skipped）+ ruff 干净；金科信既有流程零行为变化（golden 逐字段一致）
2. **模板覆盖**：8 个模板家族样本全部走通「上传 → 识别命中 → 解析 → 归集 → 预览」；必填缺失行标红进 missing_fields
3. **直连下单**：AddWork 端点 + sk 头 + create_order=true 实测可下单（EX26080356 等实证单）
4. **费用闭环**：四通道费目/金额/合计落位正确；对账恒等式成立；费目自举 golden 实证（秋怡 2019 create 模式 dropped 1901 → 0）
5. **建档闭环**：同档案连导 N 批后触发建档，档案界面可见，订单 `c_id` 回填，报告「建档清单」正确
6. **去重闭环**：同提单号重传/并发命中 → skipped，不重复下单；失败单重导不被误拦
7. **零硬编码**：全库搜不到竞品名与竞品专属列名分支
8. **上线前用户侧验收**：见 [上线前验收清单](./上线前验收清单-三阶段总.md)（A 阻塞项/B 验证项/C 清理项）

## 11. 已知限制与后续规划

### 11.1 已知限制

1. 去重仅覆盖功能上线后创建的单（历史单需 TMS 清单回填）；下游「已创建但响应超时」假失败本地兜不住，待 TMS 查重接口（方案二）补强
2. 仅支持「修改文件后重新导入」的人工修正方式，无独立提交通道
3. 创建模式逐单串行，大账单耗时较长
4. 箱号 `b_num` 只写首箱（多箱号落点未定）；`type` 枚举仅确认 1=出口
5. 基础资料计数/建档依赖单进程部署
6. 15 个标准费目在测试环境 price_id 待建档/待补（生产价格表整表待填）；费类表（class_id 全集）未到位，暂统一 4612
7. duo_get（多级应收）通道未启用；委托人档案只计数不建档（端点/来源字段缺失）

### 11.2 后续规划

1. TMS 查重接口接入（去重方案二，补强超时假失败）
2. 凭证缓存（按实测有效期优化）
3. 多箱号 `b_num` 写法实测（split_per_container 配置位已预留）
4. 应付账单 xlsx（集行格式）解析
5. 异步任务队列（超大账单后台处理 + 结果查询）
6. 费类表到位后细分 class_id；生产价格表/建档端点补齐

---

本文件与代码冲突时以代码为准。最近更新：2026-08-17。
