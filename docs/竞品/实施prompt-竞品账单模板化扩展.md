# 实施 Prompt：竞品账单模板化解析 + TMS 业务订单下单扩展

> 使用方法：把本文件整体粘贴给 coding agent。前置准备见「§0 前置准备」，需要先由人完成。

---

## 角色与背景

你要在一个已有的 FastAPI 项目上扩展「竞品账单导入」功能。昨天已完成 `feature/bill-import` 的第一版（金科信单一模板的应收对账单解析 → 归集 → 预览/下单），代码位于 `app/orders/bill/`，测试位于 `tests/orders/bill/`。

现在要做第二版：**模板化的多竞品账单解析 + TMS 业务订单新增**。所有设计决策已经定稿，你的任务是按设计实施，不要推翻架构决策；对设计未覆盖的细节可以自行决定，但必须在代码注释或 PR 描述中说明。

## §0 前置准备（人已完成/需确认）

1. 设计文档已放在 `docs/竞品/` 下，实施前**必须完整阅读**，优先级从高到低：
   - `TMS业务订单新增接口-逆推规范.md` —— 接口字段与报文结构（本次下单适配的唯一事实来源）
   - `标准业务订单模型-字段映射表.md` —— CanonicalOrder 27 个字段定义 + 八家族映射矩阵
   - `模板配置初稿-8家族.yaml.md` —— 8 份模板 YAML 配置 + 别名字典（从代码块中提取落盘）
   - `竞品账单解析-TMS业务订单接入设计.md` —— 总体架构与模板配置 schema
2. 37 个竞品账单样本已放在 `tests/golden/bill/families/` 下（按家族子目录：junyu/tonghuan/qiuyi/zhiyi/yahao/haichuan123/tonghuan1111/yinghui；根目录两个通寰 xls 已归档进去）。
3. 昨天 review 的三处修复若尚未应用，先应用（ai_header 的 header_row 越界校验、save_template 原子写、删除 aggregator 的 skipped_illegal 死字段），各自带回归测试。

## §1 目标

在**不破坏既有金科信流程**的前提下，把 bill-import 升级为：

```
上传 .xls/.xlsx
 → 模板识别（L1 指纹命中 → L2 族级近似 → L3 AI 映射+人工确认固化）
 → 配置驱动解析（单行/双行表头、区块感知、列名#N 消歧、行过滤）
 → 归一化为 CanonicalOrder（业务信息 27 字段）
 → 按 biz_no/bl_no 归集（一单=一票）
 → 预览 / 直连 TMS 下单（复用 /orders 通道，create_order=true，form-data）
```

## §2 架构约束（已定稿，必须遵守）

1. **零竞品硬编码**：代码中不允许出现「通寰/军羽/秋怡/志驿/亚灏/海川/赢辉」等竞品名，也不允许出现任何竞品专属的列名分支。竞品差异只能存在于 `templates/*.yaml` 配置中。金科信内置模板同样迁移为 `templates/jinxin_v1.yaml`，与其他家族走同一条代码路径——这是架构成立的证明。
2. **配置 schema** 以《接入设计》§3 为准：`match.fingerprints` / `family_min_overlap` / `header.row_anchor` / `two_row` / `section_fill` / `data.row_filter` / `fee_boundary` / `required` / `group_key` / `year_source` / `columns` / `normalizers`。
3. **模型能力的定位**：LLM 只在 L3（新模板家族）生成列映射，输出必须是合法的模板配置 YAML 片段，经人工确认后写入 `templates/` 固化。既有 ai_header 的四道校验闸门保留并复用。
4. **接口怪癖不出 payload 层**：form-data 括号记法、`b` 字段 JSON 双写、响应 code 为字符串 "200"、"o_id 空=新增"，全部封装在 `payload.py`/`client.py` 内部。
5. 昨天的行为语义全部沿用：单失败隔离不中断、凭证失败 502 不逐单、不自动重试防重复下单、预览/直连双模式。

## §3 实施任务（按依赖顺序）

### T1 `normalizers.py`（新增，纯函数，先行）

实现 6 个模板无关归一化函数，每个配参数化单测：

- `date_flex(value, year_hint)`：`1-1`（M-D，年份来自 year_hint，跨年 12→1 月进位）、`2021-01-01`、`2020-12-11 12:05` 三种输入 → `YYYY-MM-DD`
- `strip_float_tail(value)`：`574848870.0` → `574848870`、`6588.0` → `6588`，非数字字符串原样
- `box_parse(value)`：`40HQ` → `[{type:"40HQ",qty:1}]`；`40HQ*2` → qty=2；`20GP 主段` → 清洗后缀；非标箱型（大冷/飞翼）原样保留 type
- `vessel_voyage_split(value)`：`MAERSK SARNIA/752E` → `("MAERSK SARNIA", "752E")`；无 `/` 时整体归 vessel
- `to_int` / `to_number`：件数/毛重，空值 → None
- year_hint 的解析：从文件名（`2018-01到2018-12`、`2020-10`）或表头上方结算日期行（`结算日期：2018-01-01-2018-12-31`）提取，按配置的 `year_source` 顺序尝试

### T2 `template_store.py`（新增）

- 启动加载 `templates/*.yaml` + `alias_dictionary.yaml`；指纹算法：`md5(表头行非空单元格去空白后以 '¶' 连接)[:8]`
- `identify(sheet_view) -> TemplateMatch`：L1 指纹精确命中 → L2 列名集合重合度 ≥ `family_min_overlap`（缺列记入 `missing`，不报错）→ 未命中返回 `None` 触发 L3
- 把现金科信内置模板迁移为 `templates/jinxin_v1.yaml`；从《模板配置初稿》md 中提取 8 份家族配置 + 别名字典落盘

### T3 `parser.py`（扩展）

- 表头定位改为 `row_anchor`（锚点文本，不硬写行号）；支持 `two_row: true` 双行表头 + `section_fill: forward` 区块名右填充
- 列解析键升级为 `区块.列名`（双行家族），单列家族无前缀；支持 `列名#N` 同名消歧（取第 N 次出现）
- 数据行过滤配置化（`seq_numeric`：序号列为数值才是数据行；`stop_on` 遇到即终止）
- 费用列右边界按 `fee_boundary` 三种模式识别（`section_header` / `total_columns` / `column_range`），本轮只用于圈定业务列范围，费用值不映射
- **回归要求**：金科信 golden（1094 行真实账单）解析结果与迁移前逐字段一致，用既有测试证明

### T4 `schema.py`（扩展）

- 新增 `CanonicalOrder`：业务信息 27 字段（定义见《字段映射表》§1，核心/常见/扩展三档），含 `missing_fields`（缺失字段清单）与 `source_template`（命中的 template_id）
- `BillOrder` 保留不动；提供 `BillOrder → CanonicalOrder` 转换，既有流程零感知

### T5 `aggregator.py`（扩展）

- 归集键配置化：`group_key.primary: biz_no`，`fallback: bl_no`（军羽/通寰无业务编号）
- 同组多行的箱信息聚合为 `containers: [{container_no, box_type, seal_no}]`；同组多箱型聚合为 `box_groups: [{b_type, box_num}]`（供 payload 展开 `box[N]`）
- 删除 skipped_illegal 死字段（若 T0 未做）

### T6 `ai_header.py`（扩展 L3）

- L1/L2 未命中时：取表头区（≤15 行），先查别名字典，未命中列交 LLM 映射，prompt 要求输出**模板配置 YAML 片段**（columns 段 + two_row/fee_boundary 判定）
- 沿用四道闸门校验（含 T0 的 header_row 越界修复）；校验通过 → 预览界面展示映射结果 → 人工确认 → 写入 `templates/{family}_v1.yaml` 并计算指纹入库
- 保留 header_row 定位能力（新家族锚点未知时先定位表头行）

### T7 `payload.py` + `client.py`（新增/扩展）

- `build_order_form(order: CanonicalOrder) -> dict`：按《逆推规范》§4 映射表生成扁平 form 字段：
  - 提单号 → `data[0][b_order_num]`；箱型 → `box[N][b_type]`/`box[N][box_num]`（多箱型展开）；箱号/封号 → `b_num`/`b_lock`
  - 船名/航次/船公司、客户（c_name/c_sn）、门点（factory_name）、做箱时间（`driver[0][b_date]`）、提/还箱点（`driver[0][b_get_address/b_back_address]`）、件数/毛重/货名（`data[0][j/m/hh]`）、备注（b_note）
  - `order_date` → 派生 `month`（YYYY-MM）；`biz_no` → 拼入 b_note 备查；`biz_type/io_type` → `type` 枚举映射表（目前仅确认 1=出口，其余值留 TODO 并默认 1）
  - 常量：`order_num1=1`、`create_order=true`、`o_id` 留空、费用四通道（shou/pay/duo_get/cost）本轮整体省略
  - 同时生成 `b` 字段（JSON 双写，子集范围与抓包一致：见《逆推规范》§1）
- client 复用既有 `/orders` 通道与鉴权；响应判定 `code == "200"`（**字符串**）；回取 `data[0].sn`（TMS 业务编号）与 `o_id` 写入结果
- 多箱号（containers > 1）时 `b_num` 写法未定：**先取首箱并记 warning**，待实测后调整（配置位 `split_per_container` 预留，默认 false）

### T8 测试

- T1 六个归一化函数：参数化单测覆盖所有样本中实测出现过的脏数据形态
- T2/T3：8 家族各 ≥1 个 golden 用例——用 `tests/golden/bill/families/` 真实文件走「识别 → 解析 → 归一化」，断言关键字段抽样值（提单号/箱型/做箱时间）与行数；秋怡/志驿的多年份文件验证 L2 族级近似命中
- T5：同 biz_no 多行（秋怡一票 6 箱）聚合断言；军羽按 bl_no 兜底归集断言
- T7：payload 构造快照测试（对照《逆推规范》§4 字段名）；client 用 httpx mock 断言 form 字段与 create_order=true
- 回归：既有 111 个用例 + 全量测试必须全绿；ruff 全绿

## §4 验收标准

1. 金科信既有流程零行为变化（golden 基线逐字段一致）
2. 8 个家族样本全部走通「上传 → 识别命中 → 解析 → CanonicalOrder → 预览」，必填（提单号/箱型）缺失行标红进 missing_fields
3. 直连模式产出符合《逆推规范》的 form-data 并成功下单（**依赖人工先完成一次最小报文验证**：仅提单号+箱型+month+type+user_name+create_order=true 能否新增成功；若失败，payload 按实测结果调整）
4. 代码全库搜不到任何竞品名与竞品专属列名
5. 新增一个从未见过的模板（可用 37 样本之外任意 xls 构造）能走通 L3 → 人工确认 → 固化 → 二次上传 L1 命中的全流程

## §5 明确不做（本轮范围外）

- 应收/应付/成本费用映射（shou/pay/duo_get/cost 四通道留空）——依赖 TMS 价格表费目映射，后续立项
- 应付账单 xlsx（集行格式）解析——属「应付」类别
- `type` 枚举全值、多箱号 `b_num` 写法——留 TODO，实测后补

## §6 工作方式

- 先跑通 T1（纯函数无依赖），再按 T2→T8 顺序推进；每个 T 完成后跑相关测试再进入下一个
- 遇到设计与实测冲突（如某家族表头与配置初稿不符），**以实测为准修改配置**，并在 PR 描述中记录差异
- 不要修改与本任务无关的文件；不要改动既有路由的响应结构
