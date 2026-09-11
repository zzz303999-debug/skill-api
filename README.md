# skill-api

单证解析与业务数据接入服务：把散落的单证解析能力封装成带强类型契约的 HTTP 接口，供其他业务系统调用。内置能力：

- **竞品账单导入 TMS**：上传竞品应收对账单，解析归集后预览或批量创建订单（`POST /orders/bill/import`）
- **舱单导入 TMS**：上传英文舱单（托书/SI，`.xlsx`），解析后预览或创建 TMS 舱单（`POST /orders/manifest/import`）
- **订单创建**：自由文本 / 上传文档 → 订单字段 → 调用下游下单（`POST /orders`、`POST /orders/parse-document`）
- **托书抽取**：海运托书（PDF/图片/doc/docx/xlsx）→ 结构化 JSON（`POST /skills/tuoshu/extract`）

## 特性

- **模板驱动解析**：内置 8 家族账单模板 + AI 表头映射（校验通过且对账 matched 后自动固化，同指纹再次导入直接命中不调 AI）
- **四类归集**：业务/财务信息 → 订单；基础信息 → 客户/门点/司机/车辆建档；费用栏目 → 费用管理。preview/create 双模式，preview 零副作用
- **按提单号去重**：create 成功单登记注册表（first-write-wins、按 sk 隔离），重传自动跳过；舱单不本地去重
- **统一 LLM 出口**：所有 LLM 调用走 `app.llm`（OpenAI-compatible，thinking 模式/JSON schema 探测降级）
- **页级质量路由**：合格 PDF 页走 `pdfplumber`，扫描/残缺页走 MinerU；图片按文件签名校验后直传 MinerU，失败或低置信时转视觉模型
- **TMS 写串行**：全部下游写调用（下单/建档/费目自举）走全局串行通道，任意时刻至多 1 个在飞（TMS 侧硬约束，防业务编号撞唯一约束）
- **零静默错误**：解析降级、模板哨兵失败和 Mapper/AI 冲突均进入 blocking 复核项
- **鉴权与限流**：`API_KEY` 凭证（Bearer / X-API-Key）；按客户端 IP 滑动窗口限流（heavy/light 两档）
- **审计日志**：请求级审计（请求体/响应体/错误码/耗时），按天 JSONL 存储，内置查询页面与接口
- **注册表存储**：无数据库，持久化走单文件 JSON 注册表（导入去重、费率、主数据建档），单进程部署
- **Docker 部署**：`make docker-up`（即 `docker compose --env-file .env -f docker/docker-compose.yml up -d`）一键起（含 MinerU CPU 镜像，模型随镜像发布）

## 快速开始

### 本地开发

```bash
cp .env.example .env             # 填 LLM_BASE_URL、LLM_API_KEY 等
make install                     # uv venv + 装依赖
make dev                         # uvicorn --reload
```

访问 `http://localhost:9000`：`/docs`（Swagger UI）、`/healthz`、`/skills`。

### 生产部署

```bash
cp .env.example .env             # 至少填 LLM_*、API_KEY（生产必须）
chmod +x scripts/deploy.sh
./scripts/deploy.sh deploy       # 拉码 → 构建 → 健康检查 → 失败自动回滚
```

- `docker/docker-compose.yml`：源码构建模式（同时构建 MinerU），由 `scripts/deploy.sh` 使用
- `docker/docker-compose.deploy.yml`：镜像部署模式，`.env` 中必须设置 `SKILL_API_IMAGE` 指定具体版本，禁止使用 `latest`；仅监听 `127.0.0.1:9000`，对外入口需自备 nginx/负载均衡（TLS）

## 接口

完整规格见 OpenAPI（`/docs`）与各域接口文档：

| 接口 | 说明 | 详细文档 |
|---|---|---|
| `POST /orders/bill/import` | 竞品账单导入（preview / create；`file` + `create_order` + `sk` 头透传 TMS） | 竞品账单导入接口文档 |
| `POST /orders/manifest/import` | 舱单导入（仅 `.xlsx`；create 时 `sk` 头必填） | 舱单导入接口文档 |
| `POST /skills/{skill}/extract` | skill 抽取（当前 `tuoshu` 托书；`batch-extract` 支持多文件） | 托书接口文档 |
| `POST /orders` | 自由文本下单（只解析显式「字段：值」，不调 LLM；缺必填 422） | — |
| `POST /orders/parse-document` | 附件 → 订单字段（不实际下单）；字段缺失 200 + `needs_manual_confirmation` | — |
| `GET /healthz` | 存活检查（含依赖探测，探测失败不影响 200） | — |
| `GET /api/logs` | 审计日志查询（含 PII，必须鉴权） | — |

```bash
curl -X POST http://localhost:9000/orders/bill/import \
  -F "file=@./应收对账单.xlsx" -F "create_order=false"
```

## 鉴权

`.env` 设置 `API_KEY` 后（生产必须），除豁免路径外所有接口须携带凭证：`Authorization: Bearer <API_KEY>` 或 `X-API-Key: <API_KEY>`。

豁免路径：`GET /healthz`、`GET /skills`、`/docs`、`/redoc`、`/openapi.json`、`/favicon.ico`。`GET /api/logs` 含 PII，**不在豁免内**。未配置 `API_KEY` 时鉴权关闭（仅限可信内网/本地开发）。

## 添加新 skill

1. 新建 `app/skills/<skill_name>/`
2. 实现三件事：`schema.py`（`XxxOutput(BaseModel)`）、`skill.py`（`class XxxSkill(SkillBase)` 实现 `run()`）、`__init__.py`（注册）
3. 重启服务自动挂载 `POST /skills/<skill_name>/extract`，OpenAPI 同步更新

## 环境变量

见 `.env.example`（完整清单）。关键项：

| 变量 | 说明 |
|------|------|
| `API_KEY` | 接口访问凭证（Bearer / X-API-Key）；生产必须设置 |
| `APP_ENV` | `test`/`prod`：决定 `config/*.{env}.yaml` 按环境隔离；生产必须 `prod` |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL_DEFAULT` | LLM 出口配置（OpenAI-compatible） |
| `JXT_*` | 竞品账单下游（GetWebKey / login / AddWork）地址与凭据 |
| `MINERU_*` | MinerU 服务地址、版本锁（`MINERU_EXPECTED_VERSION` 须与镜像一致）、模型源 |
| `SKILL_MAX_CONCURRENCY` / `SKILL_QUEUE_WAIT_SECONDS` | 全局并发上限与排队超时（超时 503） |
| `API_MAX_UPLOAD_BYTES` / `API_BATCH_MAX_FILES` | 上传限制（默认 20 MiB / 10 个） |
| `STORAGE_DIR` / `STORAGE_KEEP_HOURS` | 注册表与日志存储（默认 `./storage`；审计建议 720h） |
| `RATE_LIMIT_*` | 限流开关与 heavy/light 两档阈值（生产建议开启） |
| `ACCESS_LOG_*` | 审计日志（请求体/响应体记录、截断、摘要化路径） |

## MinerU 解析服务

低质量 PDF 页（文本层不足/乱码率超阈值）与原始图片走 MinerU，合格 PDF 文本层仍走 `pdfplumber`。Compose 从 `docker/mineru/Dockerfile` 构建 CPU 镜像（CPU 版 torch + modelscope 模型随镜像发布），经内网 `http://mineru:8888` 访问。

契约保护：响应版本头须与 `MINERU_EXPECTED_VERSION` 完全一致，不符或解析失败按 `MINERU_FALLBACK_ENABLED` 回退。建议只暴露在私有网络（Compose 内网），不直接开放公网端口。

## 目录速览

```
app/          api（薄路由/中间件） core（横切设施） llm mineru orders（订单 + 账单/舱单导入） skills（托书）
config/       费率与主数据环境配置（fee_price_map.{env}.yaml、master_data.{env}.yaml，APP_ENV 选择）
templates/    内置账单模板（8 家族 yaml）
docker/       部署包：skill-api/MinerU 镜像构建 + compose 编排
scripts/      deploy.sh 部署脚本、dep_check.py 架构规则检查
tests/        单元测试 + golden 资产
storage/      运行期数据（日志/模板固化/去重注册表）——git 忽略
```

约定：单进程单 worker 部署；依赖方向单向 `api → orders → core / llm / mineru`；提交前 `make check`（ruff + 全量测试 + 依赖规则）全绿。
