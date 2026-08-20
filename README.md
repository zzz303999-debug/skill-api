# skill-api

单证解析与业务数据接入服务：把散落的单证解析能力封装成带强类型契约的 HTTP 接口，供其他业务系统调用。内置能力：

- **竞品账单导入 TMS**：上传竞品应收对账单，解析归集后预览或批量创建订单（`POST /orders/bill/import`）
- **舱单导入 TMS**：上传英文舱单（托书/SI，`.xlsx`），解析后预览或创建 TMS 舱单（`POST /orders/manifest/import`）
- **订单创建**：自由文本 / 上传文档 → 订单字段 → 调用下游下单（`POST /orders`、`POST /orders/parse-document`）
- **托书抽取**：海运托书（PDF/图片/doc/docx/xlsx）→ 结构化 JSON（`POST /skills/tuoshu/extract`）

## 特性

- **多接口服务**：skill 可扩展（自动发现挂路由）+ 订单链路 + 竞品账单导入 + 舱单导入，OpenAPI 文档展示各接口精确输入输出 schema
- **竞品账单导入**：模板驱动解析（内置模板 + AI 表头映射自动固化）、四类归集（业务信息/财务信息 → 订单，基础信息 → 客户/门点/司机/车辆建档，费用栏目 → 费用管理）、preview/create 双模式、按提单号去重（first-write-wins）
- **舱单导入**：家族识别（托书/SI）+ label 定位解析，`.xlsx` → TMS 舱单字段（addBill JSON 通道），preview/create 双模式、按提单号去重（first-write-wins）、箱型白名单与账单导入共用一份配置
- **统一 LLM 出口**：所有 LLM 调用走 `app.llm`（OpenAI-compatible，thinking 模式/JSON schema 探测降级）
- **页级质量路由**：合格 PDF 页走 `pdfplumber`，扫描/残缺页走 MinerU；图片按文件签名校验后直传 MinerU，失败或低置信时转视觉模型
- **鉴权**：配置 `API_KEY` 后所有接口必须携带凭证（Bearer / X-API-Key），未授权请求留审计记录
- **限流**：按客户端 IP 滑动窗口，heavy（LLM 密集接口）/ light（日志查询）两档
- **审计日志**：请求级审计（请求体/响应体/错误码/耗时），按天 JSONL 文件存储，内置查询页面与接口
- **注册表存储**：无数据库，持久化走单文件 JSON 注册表（导入去重、费率、主数据建档），单进程部署
- **零静默错误**：解析降级、模板哨兵失败和 Mapper/AI 冲突均进入 blocking 复核项
- **Docker 部署**：`docker compose up -d` 一键起（含 MinerU）

## 目录结构

```
skill-api/
├── app/
│   ├── main.py                  # FastAPI 入口：路由注册、鉴权、限流、统一错误处理、访问日志
│   ├── config.py                # 环境变量（pydantic-settings，基于 __file__ 定位 .env）
│   ├── errors.py                # 统一错误类型（SkillAPIError）
│   ├── logging_conf.py          # JSON 结构化日志
│   ├── access_log.py            # 请求审计日志：按天 JSONL + 内存环形缓冲 + 查询
│   ├── rate_limit.py            # 按 IP 滑动窗口限流
│   ├── core/
│   │   ├── skill_base.py        # SkillBase 抽象基类 + SkillMeta
│   │   └── registry.py          # Skill 注册中心 + 自动发现
│   ├── llm/
│   │   └── client.py            # OpenAI-compatible 客户端（唯一 LLM 出口；thinking/json_schema 探测降级）
│   ├── document_parsers/
│   │   ├── mineru.py            # MinerU HTTP 客户端与响应解析
│   │   └── models.py            # 解析结果共享数据结构
│   ├── orders/                  # 订单链路
│   │   ├── schema.py            # /orders 输入输出契约
│   │   ├── extractor.py         # 自由文本只读显式“字段：值”抽取（不调 LLM）
│   │   ├── mapper.py            # 校验抽取结果并生成下游 data 参数
│   │   ├── client.py            # 订单创建 HTTP 客户端（创建请求不自动重试）
│   │   ├── document.py          # /orders/parse-document：文档 → 订单字段（走 LLM）
│   │   └── bill/                # 竞品账单导入（/orders/bill/import）
│   │       ├── parser.py        # 模板驱动解析 + AI 表头映射（ai_header.py）
│   │       ├── aggregator.py    # 四类归集（业务/财务/基础/费用）
│   │       ├── template.py      # 账单模板库（内置 8 家族 + 自动固化）
│   │       ├── fee_map.py       # 费目映射与对账（fee_price_map.yaml + 别名词典）
│   │       ├── fee_registry.py  # 费用管理注册表（按单计，去重）
│   │       ├── imported_registry.py  # 导入成功单去重（提单号 first-write-wins）
│   │       ├── master_data*.py  # 客户/门点/司机/车辆建档与计数
│   │       ├── payload.py       # 下游 AddWork/publishCreateOrder 载荷构建
│   │       ├── client.py        # 竞品账单下游（GetWebKey → login → AddWork）
│   │       ├── service.py       # 编排（parse → aggregate → create）
│   │       └── schema.py        # 账单导入响应契约
│   │   └── manifest/            # 舱单导入（/orders/manifest/import）
│   │       ├── parser.py        # 家族识别 + label 定位提取 + 箱明细模式识别
│   │       ├── payload.py       # TMS addBill 载荷构建（每箱运价恒 0）
│   │       ├── registry.py      # 成功单去重注册表（提单号 first-write-wins）
│   │       ├── client.py        # 舱单下游 addBill 提交（JSON body + sk 头）
│   │       ├── service.py       # 编排（parse → 白名单 → preview/create）
│   │       └── schema.py        # 舱单导入响应契约
│   └── skills/
│       └── tuoshu/              # 海运托书抽取 skill
│           ├── skill.py         # 编排：convert → prompt → LLM → 后处理 → 校验
│           ├── schema.py        # Pydantic 输出 schema（强类型，进 OpenAPI）
│           ├── converter.py     # doc/docx/xlsx/pdf/图片 → markdown 转换器
│           ├── deterministic_mapper.py  # 模板指纹 + 确定性字段映射
│           ├── postprocessor.py # 后处理编排：确定性映射 + review_issues 复核
│           └── references/      # 业务知识（ports/carriers/aliases/…）+ few-shot examples
├── config/                      # 费率与主数据配置（fee_price_map.{env}.yaml、master_data.yaml）
├── templates/                   # 内置账单模板（8 家族 yaml）
├── mineru/
│   └── Dockerfile               # MinerU CPU 镜像构建（模型随镜像发布）
├── scripts/
│   └── deploy.sh                # 服务器部署脚本（拉码、构建、健康检查、回滚）
├── docs/                        # 需求/设计/接口文档（托书、竞品账单导入、舱单）
├── tests/                       # 单元测试 + golden 资产（tests/golden/）
├── storage/                     # 运行期数据（日志、账单模板固化、账单/舱单导入去重注册表）——git 忽略
├── Dockerfile
├── docker-compose.yml           # 本地/单机部署（skill-api + MinerU）
├── docker-compose.deploy.yml    # 生产部署（镜像发布 + 构建 MinerU）
├── Makefile                     # install/dev/test/lint/docker 等命令
├── pyproject.toml
├── uv.lock
└── .env.example
```

## 快速开始

### 本地开发

```bash
cp .env.example .env             # 填 LLM_BASE_URL、LLM_API_KEY 等
make install                     # uv venv + 装依赖
make dev                         # uvicorn --reload
```

访问：

- `http://localhost:9000/docs`  — Swagger UI
- `http://localhost:9000/healthz`
- `http://localhost:9000/skills` — 已注册的 skill 列表
- `http://localhost:9000/logs`   — 请求日志页面

## 生产部署交接

运维与部署交接以仓库根目录 [`DEPLOY.md`](DEPLOY.md) 为**唯一依据**，涵盖：部署方式选择（镜像部署 / 源码构建 / uv+systemd）、完整环境变量清单、首次部署、更新回滚、HTTPS 反向代理、健康检查与监控、上线验收与常见排查。

快速开始（源码构建模式）：

```bash
cp .env.example .env
# 编辑 .env，至少填写 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL_DEFAULT 和 API_KEY
chmod +x scripts/deploy.sh
./scripts/deploy.sh deploy
```

- `docker-compose.yml`：源码构建模式（同时构建 MinerU），由 `scripts/deploy.sh` 使用
- `docker-compose.deploy.yml`：镜像部署模式（API 镜像来自 GHCR，`.env` 中必须设置 `SKILL_API_IMAGE` 指定具体版本，禁止使用 `latest`）

## 鉴权

`.env` 中设置 `API_KEY` 后（生产必须），除下列**豁免路径**外，所有接口必须携带凭证，两种方式任选其一：

- `Authorization: Bearer <API_KEY>`
- `X-API-Key: <API_KEY>`

豁免路径（无需凭证）：`GET /healthz`、`GET /skills`、`/docs`、`/redoc`、`/openapi.json`、`/favicon.ico`、`GET /logs`（日志页面本身无数据）。注意：`GET /api/logs` 含 PII，**不在豁免内**，必须鉴权才能查看。未配置 `API_KEY` 时鉴权关闭（仅限可信内网/本地开发）。

## 接口

### `GET /skills`
返回所有已注册 skill 及元数据。

### `GET /healthz`
存活检查，返回 API 状态、已注册 skill 与依赖探测（LLM/MinerU/订单上游配置与可达状态）。探测失败不影响 `200`（`status` 恒为 `ok`，仅 `dependencies` 展示），避免网络抖动误判容器不健康。

### `GET /logs`
内置的请求日志查看页面（浏览器直接访问，云审计留痕入口）。表格展示每条
请求的**时间、客户端 IP、方法、路径、上传文件名与大小、耗时、状态码、错误码
和请求 ID**，IP 单元格悬停可看 UA 与 `X-Forwarded-For`；**点击带 ▼ 的行可
展开查看完整请求体 JSON**（JSON 请求自动美化格式化，截断会标注）。支持按
IP/文件名/路径/状态码筛选、自动刷新（10s）与分页加载。日志按天写入
`storage/logs/requests-YYYY-MM-DD.jsonl`（服务重启后仍可查询近期历史），
过期文件按日志时间自动整文件清理（保留时长由 `STORAGE_KEEP_HOURS` 控制）；
`/logs` 与 `/api/logs` 自身的请求不记录。

### `GET /api/logs`
请求访问日志查询接口，返回 JSON（时间倒序）：

- `limit`/`offset`：分页，默认 `limit=200`、`offset=0`
- `ip`：按客户端 IP 子串过滤（云场景下为 `X-Forwarded-For` 首地址）
- `file`/`path`：按文件名、路径子串过滤
- `status`：按状态码过滤
- `request_id`：按请求 ID 过滤（请求可携带 `X-Request-ID` 头透传）

每条记录包含审计关键字段：`ip`（客户端 IP）、`user_agent`、`body`（JSON
请求体，受 `ACCESS_LOG_BODY_MAX_CHARS` 截断）、`body_truncated`、
`file_size`、`status`、`error_code`、`duration_ms`、`ts`。大响应接口
（`/orders/bill/import`）默认只记录排查摘要（`response_summarized`），
完整响应体单独保留（`response_full`，导出场景 `include_full=1` 取回）。

```bash
curl "http://localhost:9000/api/logs?file=托书&limit=20"
curl "http://localhost:9000/api/logs?ip=203.0.113.7"
```

### `POST /skills/{skill_name}/extract`
运行指定 skill，返回结构化 JSON。

请求：`multipart/form-data`

- `file`：上传文件

示例：

```bash
curl -X POST http://localhost:9000/skills/tuoshu/extract \
  -F "file=@./order.pdf"
```

响应：

```json
{
  "skill": "tuoshu",
  "version": "0.1.0",
  "data": {},
  "meta": {
    "model": "gpt-5.4",
    "usage": {},
    "parser": "mineru",
    "parser_fallback": false
  }
}
```

解析响应会返回 `parser`、`parser_fallback` 和逐页 `page_routes`；混合 PDF 的
`parser` 为 `mixed`。

### `POST /skills/{skill_name}/batch-extract`

一次上传多个文件。服务会在全局并发限制内执行，逐文件返回 `result` 或结构化
`error`，单个文件失败不会中断其他文件。

上传限制由 `API_MAX_UPLOAD_BYTES`、`API_BATCH_MAX_FILES` 控制，Skill/LLM 总并发由
`SKILL_MAX_CONCURRENCY` 控制。

### `POST /orders`

接收上游自由文本，使用独立的订单文本 Schema 完成抽取，并在订单必填字段齐全时调用
订单创建接口，不依赖文件托书 Skill。接口只解析显式的“字段：值”，不调用 LLM，
不补全、不推断、不归一化字段值。请求为 JSON：

```json
{
  "content": "提单号：ASHHKP29193205；托运人：……",
  "roomId": "upstream-room-id",
  "userId": "10"
}
```

响应中的 `roomId` 原样回传，`source_fields` 原样保留输入标签和值；`order_data` 是按
下单接口字段名映射后的实际请求数据。`roomId` 与 `userId` 会原样传到下游请求顶层。

缺少提单号或托运人/公司名称时返回 `422`，不会调用下单接口；订单接口网络错误或
业务拒绝返回 `502`。创建调用不会自动重试，调用方
也不应在结果不明确时盲目重试，以免重复下单。

### `POST /orders/parse-document`

上传附件（托书、做箱通知等，PDF/图片/doc/docx/xlsx），转换为下单接口字段但**不实际下单**（走 LLM 抽取）。

请求：`multipart/form-data`，`file` 字段上传文件。

必填字段：提单号（≥8 位纯数字或字母数字）、箱型（4 位）、客户、地址、做箱日期、件数、毛重、体积。字段缺失或格式不合法时**不报错**：返回 `200` + `needs_manual_confirmation=true` + `missing_fields`（缺失字段）+ `missing_reasons`（缺失原因：原文未找到，请人工确认 / 格式不合法）。`order_data` 始终返回（缺失项为 `null`，做箱日期缺失时 `driver` 为 `[{}]`），由调用方人工确认后补充并提交。

```bash
curl -X POST http://localhost:9000/orders/parse-document \
  -F "file=@./托书.pdf"
```

### `POST /orders/bill/import`

上传竞品应收对账单（`.xls`/`.xlsx`/`.xlsm`），模板驱动解析归集后**预览或批量创建订单**（竞品账单导入 TMS）。详细规格见 [`docs/竞品/竞品账单导入接口文档.md`](docs/竞品/竞品账单导入接口文档.md)。

请求：`multipart/form-data`

- `file`：账单文件
- `create_order`：`false`（默认，只预览不下单）/ `true`（逐单创建订单）

```bash
curl -X POST http://localhost:9000/orders/bill/import \
  -F "file=@./应收对账单.xlsx" \
  -F "create_order=false"
```

行为要点：

- **解析**：内置 8 家族账单模板 + AI 表头映射（映射通过校验且对账 matched 后自动固化到模板库，同指纹再次导入直接命中不调 AI）
- **四类归集**：业务信息/财务信息 → 业务订单；基础信息 → 客户/门点/司机/车辆建档（按单计数）；费用栏目 → 费用管理
- **preview 模式零副作用**：不建单、不登记去重、不写任何注册表
- **create 模式**：逐单走 `GetWebKey → login → AddWork` 链路创建订单；按提单号去重（first-write-wins，成功单登记 `imported_orders.json`，同提单号再次导入整体忽略）；下游凭证获取失败返回 `502`
- **响应**：`file`/`bill_period`/`total_rows`/`order_count`/`create_order`/`orders`（或 `canonical_orders`）/`summary`/`upstream`/`meta`；完整结构见 OpenAPI schema

### `POST /orders/manifest/import`

上传英文舱单（托书/SI，`.xlsx`），解析后**预览或创建 TMS 舱单**（addBill JSON 通道）。详细规格见 [`docs/舱单/舱单导入接口文档.md`](docs/舱单/舱单导入接口文档.md)。

请求：`multipart/form-data`

- `file`：舱单文件（仅 `.xlsx`，magic bytes 校验，上限 20 MB）
- `create_order`：`false`（默认，只预览不触达 TMS）/ `true`（逐单创建）
- `sk` 请求头：create 模式必填，TMS token 原样透传下游（服务端不落盘）

```bash
curl -X POST http://localhost:9000/orders/manifest/import \
  -F "file=@./SI.xlsx" \
  -F "create_order=false"
```

行为要点：

- **解析**：家族识别（托书 Entrusting books / SI Shipping Instruction）+ label 定位提取 + 箱明细内容模式识别，一文件一票
- **必填三项**：提单号 / 箱型箱量 / 起运港（POL 原文清洗，自由输入）；preview 缺失只标记，create 拦截该单（不阻塞其他单，错误码 `manifest_order_not_ready`）
- **箱型白名单**：复用账单导入同一份 `config/box_type_whitelist.yaml`；任一单含白名单外标准码箱型 → 全部未决单拒绝（400 `unknown_box_type`），preview 亦拒绝、不调下游
- **preview 模式零副作用**：不触达 TMS、不写任何注册表
- **create 模式**：逐单走 addBill（JSON body + `sk` 头）；成功判定 = `code` **数字** 200（与账单 AddWork 字符串 `"200"` 不同）；按提单号去重（first-write-wins，成功单登记 `imported_manifests.json`）；全部命中无新建 → 409 `duplicate_manifest`；失败单不登记，重导照常提交；不自动重试
- **响应**：`file`/`create_order`/`orders`（含 `order_data` 请求体回显与 `create_result`）/`summary`/`upstream`/`meta`；完整结构见 OpenAPI schema

## 添加新 skill

三步：

1. **新建目录** `app/skills/<skill_name>/`
2. **实现三件事**：
   - `schema.py`：`class XxxOutput(BaseModel)` 定义抽取输出
   - `skill.py`：`class XxxSkill(SkillBase)`，实现 `run(file_bytes, filename, options)`
   - `__init__.py`：`from app.core.registry import register; register(XxxSkill())`
3. **重启服务**：自动挂载 `POST /skills/<skill_name>/extract`，OpenAPI 文档同步更新

无需改任何全局代码。详见 `CLAUDE.md`。

## 环境变量

见 `.env.example`（完整清单）。关键项：

| 变量 | 说明 |
|------|------|
| `API_HOST` | 监听地址，默认 `0.0.0.0` |
| `API_PORT` | 监听端口，默认 `9000` |
| `API_MAX_UPLOAD_BYTES` | 单文件最大字节数，默认 20 MiB |
| `API_BATCH_MAX_FILES` | 单批最大文件数，默认 10 |
| `SKILL_MAX_CONCURRENCY` | 单进程 Skill/LLM 最大并发数，默认 4 |
| `SKILL_QUEUE_WAIT_SECONDS` | 在途任务满时新请求排队等待的最长秒数，超时返回 503 |
| `API_KEY` | 接口访问凭证（Bearer / X-API-Key）。生产必须设置；为空时不启用鉴权（仅限可信内网） |
| `APP_ENV` | 运行环境 `test`/`prod`：决定 `config/fee_price_map.{env}.yaml` 等按环境隔离的配置；生产必须显式设置 `prod` |
| `ORDER_API_URL` | 订单创建接口地址（下游仅需业务数据、`userId` 与 `roomId`；`userId` 由上游经 `/orders` 透传，无服务端身份凭据） |
| `ORDER_API_TIMEOUT_SECONDS` | 下单接口超时秒数，默认 30；创建请求不自动重试 |
| `JXT_EXT_APP_ID` | 竞品账单下游 GetWebKey 凭据（服务端静态配置，不随请求传入） |
| `JXT_EXT_USER_ID` | 竞品账单下游 GetWebKey 凭据 |
| `JXT_JXT_OPEN_ID` | 竞品账单下游 GetWebKey 凭据 |
| `JXT_GETWEBKEY_URL` | 竞品账单下游凭证接口，默认 `https://a3.jxt56.com/Api/Account/GetWebKey` |
| `JXT_LOGIN_URL` | 竞品账单下游登录接口，默认 `https://a3.jxt56.com/Api/login` |
| `JXT_ADDWORK_URL` | 竞品账单下单接口，默认 `https://s3.jxt56.com/Car/WorkOut/AddWork` |
| `JXT_MANIFEST_ADDBILL_URL` | 舱单新增接口（addBill，JSON body + `sk` 头，与 AddWork 不同通道），默认 `https://service.jxt56.com/crm/order/bill/addBill` |
| `JXT_TIMEOUT_SECONDS` | 竞品账单下游超时秒数，默认 30 |
| `JXT_CREATE_CHANNEL` | 下单通道：`form`=AddWork 表单（默认，实测可用）；`json`=嵌套 JSON（旧默认，暂不可用） |
| `FEE_TO_OTHER_WARNING_THRESHOLD` | to_other 长尾监控阈值：某原费目名归并次数 ≥ 该值 → 对账报告 warning |
| `LLM_BASE_URL` | OpenAI-compatible API 地址，默认 `https://api.openai.com/v1` |
| `LLM_API_KEY` | LLM 服务的 Bearer Token |
| `LLM_MODEL_DEFAULT` | 服务端支持的模型名，默认 `gpt-5.4` |
| `LLM_TIMEOUT_SECONDS` | LLM 调用超时秒数，默认 180 |
| `LLM_MAX_RETRIES` | LLM 调用重试次数，默认 2 |
| `LLM_THINKING_MODE` | 思考模式 `disabled`/`enabled`，默认 `disabled`（结构化抽取可大幅降低 reasoning token 与响应耗时；模型不支持时自动降级） |
| `VISION_MAX_PDF_PAGES` | 扫描 PDF 可完整处理的最大页数，默认 10；超过时返回错误，不截断 |
| `VISION_PDF_RENDER_SCALE` | 扫描 PDF 渲染倍率，默认 2.0 |
| `VISION_MAX_IMAGE_BYTES` | 原图 base64 直传 LLM 的字节数上限，默认 8 MiB；超过则跳过 vision 并标记人工复核 |
| `IMAGE_VISION_SKIP_WHEN_CONFIDENT` | 图片 MinerU OCR 高置信时跳过 LLM vision 交叉核验以提速，默认 `true` |
| `PARSER_TEXT_MIN_CHARS` | PDF 单页合格文本层的最少字符数，默认 50 |
| `PARSER_GARBLED_RATIO_THRESHOLD` | PDF 单页允许的最大乱码率，默认 0.05 |
| `MINERU_ENABLED` | 是否启用低质量 PDF 页和图片的 MinerU 解析，代码默认 `false`；Compose 部署经 `.env.example` 开启为 `true` |
| `MINERU_BASE_URL` | MinerU HTTP 服务地址（Compose 部署为 `http://mineru:8888`） |
| `MINERU_ENDPOINT` | MinerU 解析接口路径，默认 `/file_parse` |
| `MINERU_API_KEY` | 可选 Bearer Token；留空时不发送鉴权头 |
| `MINERU_EXPECTED_VERSION` | MinerU 版本锁，默认 `2.5.4`；服务返回版本头（`x-mineru-version`/`mineru-version`）时必须完全一致，不一致报契约错误并回退 |
| `MINERU_TIMEOUT_SECONDS` | 单次 MinerU 请求超时秒数，默认 120 |
| `MINERU_FALLBACK_ENABLED` | MinerU 失败/版本不符时是否回退原有解析流程，默认 `true` |
| `MINERU_OCR_CONCURRENCY` | MinerU OCR 并发数，默认 4 |
| `MINERU_VERSION` | MinerU 镜像构建版本锁，默认 `2.5.4`（`mineru/Dockerfile` 构建用） |
| `MINERU_IMAGE` | MinerU 本地镜像标签，默认 `skill-api-mineru:2.5.4` |
| `MINERU_MODEL_SOURCE` | MinerU 模型下载源，默认 `modelscope`（可选 `huggingface`） |
| `MINERU_SHM_SIZE` | MinerU 容器共享内存，默认 `8g` |
| `MINERU_COMMAND` | MinerU 容器启动命令，默认 `mineru-api --host 0.0.0.0 --port 8888` |
| `HEALTH_PROBE_ENABLED` | `/healthz` 依赖可达性探测开关，默认 `true`；关闭后 `dependencies` 返回 `skipped` |
| `HEALTH_PROBE_TIMEOUT_SECONDS` | 单依赖探测超时秒数，默认 2；需低于 Docker healthcheck 的 `timeout`（5s） |
| `STORAGE_DIR` | 存储目录，默认 `./storage` |
| `STORAGE_KEEP_HOURS` | 日志/文件保留小时数，默认 24；审计场景建议调大（如 720 = 30 天）。按天日志文件按日志时间整文件清理（粒度天，实际保留约 ceil(小时/24)+1 天） |
| `ACCESS_LOG_RECORD_BODY` | 是否记录 JSON 请求体，默认 `true` |
| `ACCESS_LOG_BODY_MAX_CHARS` | 请求体记录最大字符数，默认 4096，超出截断 |
| `ACCESS_LOG_RECORD_RESPONSE` | 是否记录 JSON 响应体（logs 页面展示用），默认 `true`；开启时 JSON 响应被全量缓冲（内存峰值与响应体量相关，截断只影响落盘） |
| `ACCESS_LOG_RESPONSE_MAX_CHARS` | 响应体记录最大字符数，默认 64 KiB，超出截断并标记 `response_truncated`（仅截断日志，客户端仍收完整响应） |
| `ACCESS_LOG_SUMMARIZE_PATHS` | 响应摘要化路径子串（逗号分隔），命中路径的 JSON 响应只记录排查摘要，默认 `/orders/bill/import`；置空恢复完整记录 |
| `ACCESS_LOG_RESPONSE_FULL_MAX_CHARS` | 摘要化时完整响应体单独保留的最大字符数（`response_full`，导出/取证用），默认 1 MiB |
| `ACCESS_LOG_TRUST_PROXY` | 是否信任 `X-Forwarded-For`/`X-Real-IP` 记录真实客户端 IP，默认 `true` |
| `RATE_LIMIT_ENABLED` | 请求限流开关（内存滑动窗口，按客户端 IP），默认 `false`；生产建议开启 |
| `RATE_LIMIT_HEAVY_MAX_REQUESTS` | heavy 档（`/orders`、`/skills/*` 等 LLM 密集接口）窗口内最大请求数，默认 10 |
| `RATE_LIMIT_HEAVY_WINDOW_SECONDS` | heavy 档窗口秒数，默认 60 |
| `RATE_LIMIT_LIGHT_MAX_REQUESTS` | light 档（`/api/logs` 日志查询）窗口内最大请求数，默认 120 |
| `RATE_LIMIT_LIGHT_WINDOW_SECONDS` | light 档窗口秒数，默认 60 |
| `RATE_LIMIT_WHITELIST` | 免限流 IP 白名单，逗号分隔（如内网网关）；留空则不豁免 |
| `LOG_LEVEL` | 日志级别，默认 `INFO` |

### MinerU 解析服务

MinerU 接管**低质量 PDF 页**（文本层不足/乱码率超阈值）和**原始图片**，将其解析为 markdown 结构化内容；合格的 PDF 文本层仍走 `pdfplumber`。MinerU 之后的分析流程（模板 Mapper、LLM 补全和业务 JSON schema）不变，`MINERU_ENABLED=false` 时服务仍可启动：普通 PDF 由 `pdfplumber` 处理，图片和扫描件由配置的视觉模型处理——关闭 MinerU 不等于完全离线，LLM 服务仍是必需依赖。

**部署形态**：Docker Compose 从 `mineru/Dockerfile` 构建锁定版本的 CPU 镜像（模型随镜像发布，构建版本由 `MINERU_VERSION` 锁定，模型下载源 `MINERU_MODEL_SOURCE` 可选 `modelscope`/`huggingface`），通过 Compose 内网地址 `http://mineru:8888` 访问；也保留了对独立部署 MinerU 服务的 HTTP 客户端兼容。

**契约保护**：客户端校验响应版本头（`x-mineru-version`/`mineru-version`），必须与 `MINERU_EXPECTED_VERSION` 完全一致，版本不符或解析失败时按 `MINERU_FALLBACK_ENABLED` 决定是否回退原有解析流程（默认回退，保证解析不中断）。图片 OCR 高置信时跳过 LLM vision 交叉核验以提速（`IMAGE_VISION_SKIP_WHEN_CONFIDENT`）。

**安全**：MinerU 建议只暴露在私有网络（Compose 内网），不直接开放公网端口。

```env
MINERU_ENABLED=true
MINERU_BASE_URL=http://<mineru-host>:8888
MINERU_ENDPOINT=/file_parse
MINERU_API_KEY=
MINERU_EXPECTED_VERSION=2.5.4
MINERU_TIMEOUT_SECONDS=120
MINERU_FALLBACK_ENABLED=true
MINERU_VERSION=2.5.4
MINERU_IMAGE=skill-api-mineru:2.5.4
MINERU_MODEL_SOURCE=modelscope
MINERU_SHM_SIZE=8g
MINERU_COMMAND=mineru-api --host 0.0.0.0 --port 8888
```
