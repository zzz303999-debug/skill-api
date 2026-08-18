# 托书抽取 PRD

## 1. 文档信息

| 项 | 内容 |
|---|---|
| 产品名称 | 单据智能抽取服务（skill-api）· 托书抽取（tuoshu） |
| 文档版本 | v1.0 |
| 编制日期 | 2026-08-17 |
| 事实基准 | 代码实况（app/skills/tuoshu/）为主，文档冲突时以代码为准 |
| 关联文档 | [托书接口文档](./托书接口文档.md)（接口级契约）；[托书设计文档](./托书设计文档.md)（解析链路设计）；[托书需求文档](./需求文档.md)（v0.1 初稿） |

### 修订记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-08-17 | 初稿：按代码实况归纳托书抽取全链路需求 |

## 2. 背景与目标

### 2.1 背景

货代/物流业务中，托书、做箱通知、运输委托书等单据每天大量产生，人工录入订单信息耗时且易错。单据格式多样：不同货代有不同模板（已积累 12 个模板指纹），不同文件格式（Word/Excel/PDF/图片/扫描件）。

### 2.2 目标

1. 把"看单据 → 提取关键字段 → 录入订单系统"的过程自动化
2. 输出**结构化、强类型、可校验**的数据（Pydantic schema，直接进 OpenAPI），业务系统可直接消费
3. 关键数字（提单号、箱号、件数、毛重等）**宁可人工复核，不可静默出错**
4. 输出**可下单判定**（ready_for_order）与订单字段映射（order_mapping），支持"自动下单 / 人工确认后下单"两条消费路径

### 2.3 核心设计原则

1. **零静默错误**：任何解析降级、数据冲突、来源可疑，都显式标记 review_issue；存在 blocking 问题时 ready_for_order=false
2. **原文保真**：编号类字段（提单号、箱号、封号、人名）逐字复制，不智能改写——O vs 0、I vs 1 的差别就是业务事故
3. **数字不计算**：件数/毛重/体积只做格式清洗，不做求和、不做单位换算（吨转 KG 除外并注明）
4. **来源可溯**：每个结果带原始文件名、格式、模板命中、抽取时间、文件哈希、原文 markdown 全文
5. **确定性优先**：确定性规则（模板指纹映射、标签正则、格式校验）永远压过 LLM 输出；LLM 与确定性结果冲突时保留确定性值并标记复核

## 3. 名词解释

| 术语 | 说明 |
|---|---|
| 托书 | 海运托运委托书类单据统称，含做箱通知、运输委托书、派车托书、订舱托书 |
| 提单号（mbl_no） | 主提单号；≥8 位数字或字母数字（纯字母不算）；**关单号即提单号**，报关单号不是 |
| 子提单号（hbl_no） | 分提单号；不得回退用主提单号填充 |
| 做箱时间（loading_time） | 工厂装箱的日期/时间；**截单时间、截关时间不是做箱时间** |
| 船期（etd） | 开航时间；**开港时间不是船期**（归备注） |
| 中转港 vs 目的港 | 中转港是转运港口，目的港是最终卸货港，不得混淆 |
| review_issue | 人工复核问题：code/field/message/source_values/blocking |
| blocking 问题 | 阻断型复核问题，存在时必须人工确认后才能下单 |
| ready_for_order | 下单就绪标记：无任何 blocking 问题才为 true |
| 模板指纹 | 已知客户/货代单据格式标记（12 个 prompt 路由模板 + 2 个确定性映射模板） |
| MinerU | 文档解析引擎，负责低质量 PDF 页与图片的 OCR 结构化解析 |
| grounding | 自由文本字段的原文依据校验：值必须能在原文/原图找到依据 |

## 4. 用户角色与使用场景

| 角色 | 说明 |
|---|---|
| 业务系统（调用方） | 上传单据取结构化数据；按 ready_for_order 决定自动下单或转人工 |
| 业务人员 | 处理 review_issues：补充缺失、确认冲突、核对编号 |
| 运维 | 维护 references/ 业务知识库与模板样例；配置 MinerU/vision/限流 |

典型场景：

1. **托书自动录入**：业务系统收到托书文件，调抽取接口得到结构化数据 + 复核清单，无阻断问题时自动填入订单表单
2. **附件预解析**：上传做箱通知附件，先解析出下单字段，人工确认补充后提交（/orders/parse-document）
3. **聊天文本下单**：IM 对话中的订舱信息文本，自由文本接口直接生成订单（/orders）
4. **批量处理**：一次上传最多 10 个文件，逐文件返回结果或错误，单文件失败不影响其他

## 5. 功能需求

### 5.1 文件上传与格式校验

- 接口：`POST /skills/tuoshu/extract`，multipart 上传单文件；`POST /skills/tuoshu/batch-extract` 批量（≤10 个）
- 支持格式：`.xlsx`/`.xlsm`/`.xls`/`.docx`/`.doc`/`.pdf`/`.jpg`/`.jpeg`/`.png`/`.bmp`/`.tiff`/`.tif`/`.gif`/`.webp`
- 按内容（magic bytes）识别真实格式，不信任扩展名；扩展名与内容不符、空文件、非受支持图片内容均拒绝并给出中文错误说明
- 单文件上限 20 MB；扫描 PDF 页数上限 10 页（超限报错提示拆分，**绝不截断处理**）

### 5.2 文档转换（bytes → markdown）

- Word/Excel：本地确定性转换器（docx/xls/xlsx/word_xml/doc），`.doc` 依赖 LibreOffice 或 textutil
- PDF：启用 MinerU 时**按页质量探测路由**——每页探测文本层质量（字符数、乱码率、关键标签 bbox 重叠），合格页本地 pdfplumber 提取，不合格页转图送 MinerU OCR 并行处理；未启用时保持本地兼容路径
- 图片：原图直传 MinerU OCR；MinerU 失败或低置信时回退 vision 交叉核验
- Word 内嵌图片：逐张送识别并强制标记复核（需确认其属于正文而非印章/logo/水印）
- 转换降级必须显式标记：Excel 公式无缓存值、附件存在未覆盖内容、内嵌图片未处理，均产生 review_issue

### 5.3 模板识别与路由（零 LLM 成本）

- **prompt 路由**（detect_prompt_route）：按文本特征匹配 12 个已知模板指纹 + 文档类型判定（四类+兜底），只用于选择 prompt 上下文与 few-shot 样例，不发额外 LLM 请求
- **确定性模板映射**（map_template）：2 个精确模板（neizhuang_booking 内装箱委托书、bingsheng_transport 表头序列指纹）用确定性规则直接锁定字段值，LLM 只补未覆盖字段；AI 与锁定值冲突时保留锁定值并产生 blocking `deterministic_ai_conflict`
- 模板命中只是标注，未命中不影响抽取（走通用抽取）
- 文档类型：做箱通知（PACKING_NOTICE）/ 运输委托书（TRANSPORT_ORDER）/ 派车托书（TRUCKING_ORDER）/ 订舱托书（BOOKING_NOTE）/ 未知（UNKNOWN）；「做箱通知」标题含提箱/进港/司机等派车字段时判 TRUCKING_ORDER

### 5.4 LLM 结构化抽取

- 输出受 TuoshuOutput JSON Schema 强约束（字段名严格一致），temperature=0
- 系统提示含字段来源表、12 条抽取规则、review_issue 输出规范；few-shot 按路由选 1 个最相关样例
- 字段名归一化兜底（网关不支持 json_schema 时矫正中文 key）；review_issue 结构无效时单次修复重试（只重发 review_issues 数组，~20s）

### 5.5 确定性校验与修复（后处理）

LLM 输出必须过确定性后处理管线（30+ 项校验/修复），关键项：

| 类别 | 规则 |
|---|---|
| 显式字段恢复 | 从原文正则恢复 recipient/doc_date/carrier/customer_ref/shipper_agent/customer/loading_time/etd/transit_port/hbl_no；原文明示值优先于 LLM 值 |
| 提单号清洗 | ≥8 位字母数字、去空格连字符；关单号即提单号；子提单号误填为主提单号时清空并标记 |
| 保真校验 | 人名/子提单号/箱号/封号必须逐字出现在原文，否则清空 + blocking；箱号必须 4 大写字母+7 数字；封号无空格 |
| 承运人 | 原文明示值优先；由提单号前缀/船名推断时标记 blocking `carrier_by_mbl`/`carrier_by_vessel`；前缀与已有值冲突时按前缀修正 + 标记 |
| grounding | remark/柜备注/封号等自由文本必须在原文找到依据，补写原文没有的内容 → blocking `ungrounded_text` |
| 日期 | YYYY-MM-DD（时间 YYYY-MM-DDTHH:MM:SS）；缺年按文档日期/业务号/原文年份推断，无法推断置空 + `etd_year_missing` |
| 数值 | 件数整数、毛重体积 2-3 位小数；只清单位千分位不求和；吨转 KG；≤0 清空；件数+体积同时缺失 → blocking `missing_container_measurements` |
| 箱型 | 标准 4 位（20/25/40+两字母）；HQ/HC/DV/GP 不得互换；非标逐字保留 + 非阻断提示；总箱量与明细冲突时以明细为准 |
| 港口 | 州/国家修饰不丢（COLUMBUS(OH)→COLUMBUS, OH）；中转港与目的港互不混淆；「见设备单」等待查描述逐字保留，与具体值并存 → blocking |
| 客户来源 | 只认 FM/FROM 后的公司名、明确客户栏、客户简称+装箱/做箱通知抬头、正文抬头公司；文件名前缀不是客户来源 |
| 做箱地址 | 只取详细街道地址；不得把公司名、联系人或电话并入地址 |

- review_issue 白名单制：LLM 自创 code 一律清除；`_ALWAYS_BLOCKING_CODES` 强制 blocking；排序按优先级 + code + field 确定性输出
- 必提取字段缺失判定：mbl_no/customer/factory.address/containers[].type/loading_time/packages+gross_weight_kg+volume_cbm

### 5.6 人工复核机制与下单就绪

- `review_issues[]`：每项含 code/field/message/source_values/blocking
- **blocking=true 的问题存在时 ready_for_order=false**，业务系统必须人工确认；非阻断问题（如已跳过视觉交叉核验、船期缺年份）建议抽检但不阻止下单
- `order_mapping`：确定性订单字段映射——c_sn=我司业务编号、mbl_no、hbl_no、c_title=托运人公司、factory_name=工厂门点简称、c_note=订单备注（PO 号+原文备注）

### 5.7 溯源与展示

- 响应含：skill/version、data（英文 schema）、meta（模型、解析器、覆盖度、文件哈希、转换状态）、content（原文 markdown 全文）
- `meta.conversion_status`：converted / needs_review（有解析层 issue 或转换不完整）
- 中文展示适配器（chinese_schema）：英文 JSON → 中文 key 纯展示转换，只消费已过 schema 校验的数据

## 6. 非功能需求

### 6.1 性能与容量

| 项 | 默认值 |
|---|---|
| 单文件大小上限 | 20 MB |
| 批量文件数上限 | 10 个/批 |
| 并发任务数 | 4（可配 1-64），排队超 10 秒返回 503 |
| 扫描 PDF vision 页数上限 | 10 页 |
| AI 单次调用超时 | 180 秒，失败自动重试 2 次 |
| 图片 vision 直传上限 | 8 MB，超限降级纯 OCR 并强制人工复核 |
| MinerU OCR 并发 | 4（可配 1-16） |

### 6.2 安全

- 接口鉴权：Bearer Token 或 X-API-Key（生产必须开启）；鉴权豁免仅限健康检查、接口文档、能力列表、日志页面
- 日志数据接口必须鉴权（请求体含 PII）；凭证比对恒定时间

### 6.3 限流

- 重档（单据抽取/订单接口）：60 秒内每 IP 10 次；轻档（日志查询）：60 秒内 120 次；内网白名单豁免；被限流请求仍写访问日志

### 6.4 可用性

- 单进程部署（内存限流/并发计数）；容器化 + 健康检查 + 自动重启
- MinerU 失败回退 vision 或本地路径，不静默丢数据；vision 无可用且无 OCR 文本时**直接拒绝**（绝不允许把空文档喂给 LLM 产出捏造数据）

### 6.5 可扩展性

- 新单据能力三步接入：新建 skill 目录 → 定义输出契约 + 抽取逻辑 → 重启自动挂载 /skills/{name}/extract 与 batch-extract，无需改全局代码
- 业务知识库（references/*.md：船公司/港口/字段别名/箱型标准/分类/展示格式/样例库）以文档形式维护，更新即生效

## 7. 接口契约概要

完整契约见 [托书接口文档](./托书接口文档.md)。

| 接口 | 方法 | 用途 |
|---|---|---|
| `/skills/tuoshu/extract` | POST | 单据文件抽取（multipart 上传 file） |
| `/skills/tuoshu/batch-extract` | POST | 批量抽取（≤10 文件，逐文件结果/错误） |
| `/orders` | POST | 自由文本抽取并下单 |
| `/orders/parse-document` | POST | 附件文档转订单字段（不下单） |

## 8. 配置项

| 配置 | 说明 | 默认 |
|---|---|---|
| `llm_base_url/api_key/model_default` | AI 网关 | 部署必填 |
| `llm_vision_enabled` | 视觉能力开关；关闭时图片/扫描件走 MinerU OCR 纯文本 | false |
| `llm_thinking_mode` | 思考模式，关闭降耗时降成本 | disabled |
| `mineru_enabled/base_url/endpoint` | MinerU 开关与地址 | false |
| `mineru_fallback_enabled` | MinerU 失败回退 vision | true |
| `vision_max_pdf_pages` | 扫描 PDF vision 页数上限 | 10 |
| `vision_max_image_bytes` | 图片 vision 直传上限 | 8 MB |
| `image_vision_skip_when_confident` | MinerU 高置信时跳过 vision 提速 | true |
| `parser_text_min_chars` / `parser_garbled_ratio_threshold` | PDF 页质量探测阈值 | 50 / 0.05 |
| `skill_max_concurrency` / `skill_queue_wait_seconds` | 并发与排队上限 | 4 / 10s |
| `rate_limit_*` | 限流档位/白名单 | 本地关闭 |
| `api_max_upload_bytes` | 单文件上限 | 20 MB |

## 9. 错误码

| HTTP | 错误码 | 场景 |
|---|---|---|
| 400 | `bad_request` | 扩展名不支持 |
| 400 | `file_too_large` | 超过 20 MB |
| 400 | `empty_file` | 空文件 |
| 400 | `file_format_mismatch` | 扩展名与内容不符 |
| 400 | `unsupported_image_content` | 图片内容不是受支持格式 |
| 413 | `payload_too_large` | 请求体超限 |
| 422 | `convert_error` | 转换失败/损坏/无 extractable text |
| 422 | `pdf_page_limit_exceeded` | 扫描 PDF 页数超限 |
| 422 | `vision_image_too_large` | 图片/渲染页超 vision 上限且无 OCR 文本 |
| 422 | `empty_converted_content` | 转换后无有效内容 |
| 422 | `vision_disabled_no_ocr` | 无视觉能力且无 OCR 文本 |
| 422 | `order_not_ready` | 自由文本缺提单号/公司名 |
| 429 | `rate_limited` | 触发限流 |
| 502 | `llm_network` / `llm_upstream` / `parse_error` | AI 网关网络故障/拒绝/响应非法 |
| 502 | `order_upstream_error` | 订单上游拒绝/不可达 |
| 503 | `server_busy` | 并发满排队超时 |
| 500 | `internal_error` | 服务内部错误 |

统一结构 `{error: {code, message, description, details}}`，description 为中文说明。

## 10. 质量保障

- **黄金用例回归**：真实业务单据（Word/PDF×2/图片，覆盖文本型 PDF、扫描型 PDF、图片 OCR 与本地转换各路径）+ 期望输出，回归保障抽取质量不退化
- 单元测试覆盖：字段归一化、箱型解析、订单文本抽取、校验规则、错误处理、限流、日志
- 全量基线：pytest 全绿（当前 788 passed / 8 skipped）+ ruff 干净

## 11. 已知限制与后续规划

### 11.1 已知限制

1. 自由文本抽取仅支持显式"标签：值"格式，不支持无标签纯文本段落推断
2. 限流/并发计数为单进程内存实现，多实例部署需共享存储
3. 单文件同步处理，超大批量需拆分
4. 当前 LLM 默认无视觉能力（llm_vision_enabled=false），图片/扫描件走 MinerU OCR 纯文本 + 强制人工复核
5. 仅内置托书一种 skill（架构支持快速扩展）
6. 人工复核操作由业务系统完成，本服务只输出问题清单

### 11.2 后续规划

- 异步任务队列（长批次、多阶段任务）
- 指标监控（/metrics）
- 更多单据类型接入
- 抽取结果持久化（对象存储）
- 人工复核工作台页面

---

本文件与代码冲突时以代码为准。最近更新：2026-08-17。
