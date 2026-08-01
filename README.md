# skill-api

统一的 skill 抽取 API 服务。把散落的 Claude/OpenCode skill 封装成带强类型契约的
HTTP 接口，供业务系统调用。目前内置海运托书抽取，并支持通过 MinerU 增强 PDF/图片
结构化解析。

## 特性

- **多 skill 可扩展**：每个 skill 一个子目录，启动自动发现并挂路由
- **每个 skill 独立强类型契约**：OpenAPI 文档里能看到各 skill 精确的输入输出 schema
- **统一 LLM 出口**：所有 skill 通过 `app.llm` 调用 OpenAI-compatible API
- **页级质量路由**：合格 PDF 页走 `pdfplumber`，扫描/残缺页走 MinerU
- **图片原图解析**：按文件签名校验格式后直传 MinerU，失败或低置信时转视觉模型
- **零静默错误**：解析降级、模板哨兵失败和 Mapper/AI 冲突均进入 blocking 复核项
- **同步 API**：先跑可行版本，后续可无缝加异步任务队列
- **Docker 部署**：`docker compose up -d` 一键起

## 目录结构

```
skill-api/
├── app/
│   ├── main.py                  # FastAPI 入口，启动时自动注册 skill
│   ├── config.py                # 环境变量
│   ├── errors.py                # 统一错误类型
│   ├── logging_conf.py          # JSON 日志
│   ├── llm/client.py            # OpenAI-compatible 客户端（唯一 LLM 出口）
│   ├── document_parsers/
│   │   └── mineru.py            # MinerU HTTP 客户端与响应解析
│   ├── core/
│   │   ├── skill_base.py        # SkillBase 抽象
│   │   └── registry.py          # 注册中心 + 自动发现
│   └── skills/
│       └── tuoshu/              # 托书抽取 skill
│           ├── __init__.py      # register(TuoshuSkill())
│           ├── skill.py         # 编排：convert → prompt → LLM → 校验
│           ├── schema.py        # Pydantic 输出 schema
│           ├── prompt.py        # prompt 组装
│           ├── convert_service.py  # bytes → markdown 包装层
│           ├── converter.py     # 从 tuoshu-extractor 复用的转换器
│           └── references/      # 业务知识 + few-shot
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── pyproject.toml
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

## 生产部署交接

### 服务与外部依赖

```text
调用方
  └── skill-api:9000
        ├── OpenAI-compatible LLM（托书抽取必需）
        ├── MinerU（Compose 同栈容器，提高图片/扫描 PDF 解析质量）
        └── 订单接口（仅 POST /orders 需要）
```

| 组件 | 是否包含在本仓库 | 是否必需 | 不可用时的影响 |
|------|--------------------|----------|------------------|
| `skill-api` | 是 | 是 | 整个 API 不可用 |
| LLM 服务 | 否 | 托书抽取必需 | 文件转换可能成功，但 LLM 抽取失败 |
| MinerU | 是，独立 Compose 容器 | 扫描件高质量解析需要 | 关闭时图片/扫描件改走 vision |
| 订单接口 | 否 | 仅 `/orders` 必需 | `/orders` 返回 `502/503`，托书抽取不受影响 |

`GET /healthz` 只是 `skill-api` 存活检查，不会请求 LLM、MinerU 或订单接口。
所以上线验收必须再执行一次真实文档抽取。

### 部署前准备

- Python `3.11`；非 Docker 部署使用 `uv` 按 `uv.lock` 安装。
- 准备可访问的 OpenAI-compatible API 地址、API Key 和模型名。
- Docker Compose 部署会同时构建并启动 MinerU；非 Compose 部署时才需要单独准备 MinerU HTTP 服务。
- 云服务器对外建议由 Nginx/Caddy 提供 HTTPS，`9000` 只对反向代理开放。
- 放通 `skill-api` 到 LLM、MinerU 和订单接口的出站网络。

敏感变量 `LLM_API_KEY`、`MINERU_API_KEY` 和订单凭据必须由 Secret/
服务器环境管理，不得提交 `.env`。

### 方式一：Docker Compose

#### 云服务器从源码更新

服务器直接检出本仓库时，可使用部署脚本完成拉代码、构建、停止旧容器、启动新容器和
健康检查。首次部署先安装 Git、Docker Engine 和 Compose plugin，再准备 `.env`：

```bash
cp .env.example .env
# 编辑 .env，至少填写 LLM_BASE_URL、LLM_API_KEY 和 LLM_MODEL_DEFAULT
chmod +x scripts/deploy.sh
./scripts/deploy.sh deploy
```

脚本默认更新 `origin/knight`，且生产目录中的 tracked 文件必须没有本地修改。拉取或构建
失败时旧容器不会停止；新容器健康检查失败时，脚本会尝试恢复部署前的本地镜像。

```bash
./scripts/deploy.sh status
./scripts/deploy.sh logs
./scripts/deploy.sh restart
./scripts/deploy.sh stop
./scripts/deploy.sh start
```

可通过环境变量覆盖部署参数，例如：

```bash
DEPLOY_BRANCH=main HEALTH_TIMEOUT_SECONDS=180 ./scripts/deploy.sh deploy
```

该脚本使用 `docker-compose.yml` 在服务器本地构建 `skill-api` 和 MinerU，两个进程在
不同容器中运行，API 通过 `http://mineru:8888` 访问 MinerU。MinerU 模型在构建镜像时下载并
随镜像发布，更新 MinerU 版本或模型源时需要重新构建镜像。

#### 从镜像仓库更新

GitHub 推送 `v*` tag 后，Actions 会将版本镜像发布到 GHCR。部署人员需在仓库检出目录
保存 `.env`（MinerU 构建还会使用 `mineru/Dockerfile`），并显式指定 API 镜像版本：

```env
SKILL_API_IMAGE=ghcr.io/zzz303999-debug/skill-api:v0.1.0
LLM_BASE_URL=https://<llm-host>/v1
LLM_API_KEY=<secret>
LLM_MODEL_DEFAULT=<model-name>
MINERU_ENABLED=true
MINERU_BASE_URL=http://mineru:8888
MINERU_IMAGE=skill-api-mineru:2.5.4
```

私有 GHCR 镜像需先用只读 Packages Token 登录，然后部署：

```bash
docker compose -f docker-compose.deploy.yml config --quiet
docker compose -f docker-compose.deploy.yml pull skill-api
docker compose -f docker-compose.deploy.yml build --pull mineru
docker compose -f docker-compose.deploy.yml up -d
docker compose -f docker-compose.deploy.yml ps
docker compose -f docker-compose.deploy.yml logs --tail=200 skill-api
```

生产不得依赖 `latest`。回滚时将 `.env` 中 `SKILL_API_IMAGE` 改为上一个已验证版本，
重新执行 `pull` 和 `up -d`。

如 LLM 或 MinerU 运行在 Linux Docker 宿主机而不在 Compose 网络，需给
`skill-api` 增加：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

然后使用 `http://host.docker.internal:<port>`。容器中的 `127.0.0.1` 只代表容器自身，
不代表云服务器宿主机。

使用同机 Nginx/Caddy 时，应将 `docker-compose.deploy.yml` 的端口映射改为
`127.0.0.1:9000:9000`，避免绕过 HTTPS 直接访问 Uvicorn。

### 方式二：uv + systemd（非 Docker）

在 Linux 服务器安装 Python 3.11、uv、LibreOffice、`fonts-noto-cjk` 和 `curl`，然后：

```bash
sudo useradd --system --home-dir /opt/skill-api --shell /usr/sbin/nologin skill-api
cd /opt/skill-api
uv sync --frozen --no-dev --python 3.11
cp .env.example .env
# 编辑 .env，填入生产地址和 Secret
sudo install -d -o skill-api -g skill-api /opt/skill-api/storage
sudo chown root:skill-api /opt/skill-api/.env
sudo chmod 0640 /opt/skill-api/.env
```

如 `skill-api` 用户已存在，跳过 `useradd`。

使用专用的低权限用户运行。`/etc/systemd/system/skill-api.service` 示例：

```ini
[Unit]
Description=skill-api
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=skill-api
Group=skill-api
WorkingDirectory=/opt/skill-api
EnvironmentFile=/opt/skill-api/.env
Environment=HOME=/opt/skill-api/storage
ExecStart=/opt/skill-api/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9000
Restart=always
RestartSec=5
TimeoutStopSec=210
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now skill-api
sudo systemctl status skill-api
sudo journalctl -u skill-api -n 200 --no-pager
```

`Restart=always` 处理进程退出，但不能代替外部监控。应从另一台机器或云拨测每分钟
请求 `/healthz`，并将连续失败和恢复事件发到企业微信。

### HTTPS 反向代理

Nginx 默认请求体限制不足以上传大文档，默认代理超时也可能早于 LLM 超时。生产配置
至少应包含：

```nginx
server {
    listen 443 ssl;
    server_name skill-api.example.com;
    ssl_certificate /etc/letsencrypt/live/skill-api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/skill-api.example.com/privkey.pem;

    # 应与 API_MAX_UPLOAD_BYTES 保持一致。
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 210s;
        proxy_send_timeout 210s;
    }
}
```

证书路径由部署环境的 ACME/证书管理工具生成并按实际域名替换。云安全组对公网只开放 `80/443`，
不开放 `9000`。

### 上线验收

1. 存活检查必须返回 `status=ok` 且包含 `tuoshu`：

   ```bash
   curl -fsS http://127.0.0.1:9000/healthz
   ```

2. 使用非生产样例文档做端到端抽取：

   ```bash
   curl -fsS -X POST http://127.0.0.1:9000/skills/tuoshu/extract \
     -F "file=@./sample.pdf"
   ```

3. 确认返回中存在 `data`、`meta.model`、`meta.parser` 和 `content`。
4. 如启用 MinerU，确认 `meta.parser` 或 `meta.page_routes[].parser` 中存在 `mineru`；
   如出现 `mineru_failed`/`mineru_low_confidence`，查看网络、版本和 MinerU 日志。
5. 如使用 `/orders`，用测试账号单独验证一次；该接口会真实创建订单，不得用
   生产数据反复重试。

## 接口

### `GET /skills`
返回所有已注册 skill 及元数据。

### `GET /healthz`
存活检查，返回 API 状态和已注册 skill。该接口不调用 LLM、MinerU 或订单
接口，因此 `200` 只表示 FastAPI 进程可响应，不表示端到端抽取可用。

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
  "roomId": "upstream-room-id"
}
```

响应中的 `roomId` 原样回传，`source_fields` 原样保留输入标签和值；`order_data` 是按
下单接口字段名映射后的实际请求数据。`roomId` 会原样传到下游请求顶层。

缺少提单号或托运人/公司名称时返回 `422`，不会调用下单接口；订单接口网络错误或
业务拒绝返回 `502`。创建调用不会自动重试，调用方
也不应在结果不明确时盲目重试，以免重复下单。

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

见 `.env.example`。关键项：

| 变量 | 说明 |
|------|------|
| `LLM_BASE_URL` | OpenAI-compatible API 地址，默认 `https://api.openai.com/v1` |
| `LLM_API_KEY` | LLM 服务的 Bearer Token |
| `LLM_MODEL_DEFAULT` | 服务端支持的模型名，默认 `gpt-5.4` |
| `LLM_THINKING_MODE` | 思考模式 `disabled`/`enabled`，默认 `disabled`（结构化抽取可大幅降低 reasoning token 与响应耗时；模型不支持时自动降级） |
| `API_MAX_UPLOAD_BYTES` | 单文件最大字节数，默认 20 MiB |
| `API_BATCH_MAX_FILES` | 单批最大文件数，默认 10 |
| `SKILL_MAX_CONCURRENCY` | 单进程 Skill/LLM 最大并发数，默认 4 |
| `ORDER_API_URL` | 订单创建接口地址 |
| `ORDER_API_EXT_APP_ID` | 下单账号 `ext_app_id` |
| `ORDER_API_EXT_USER_ID` | 下单账号 `ext_user_id` |
| `ORDER_API_JXT_OPEN_ID` | 下单账号 `jxt_open_id`，同时映射到订单 `c_id` |
| `ORDER_API_USER_ID` | 发送人 `userId` |
| `ORDER_API_ORDER_INFO` | `apiKeyInfo.order_info` JSON 数组 |
| `ORDER_API_TIMEOUT_SECONDS` | 下单接口超时秒数，默认 30；创建请求不自动重试 |
| `VISION_MAX_PDF_PAGES` | 扫描 PDF 可完整处理的最大页数，默认 10；超过时返回错误，不截断 |
| `VISION_PDF_RENDER_SCALE` | 扫描 PDF 渲染倍率，默认 2.0 |
| `PARSER_TEXT_MIN_CHARS` | PDF 单页合格文本层的最少字符数，默认 50 |
| `PARSER_GARBLED_RATIO_THRESHOLD` | PDF 单页允许的最大乱码率，默认 0.05 |
| `MINERU_ENABLED` | 是否启用低质量 PDF 页和图片的 MinerU 解析，默认 `true` |
| `MINERU_BASE_URL` | MinerU HTTP 服务地址 |
| `MINERU_ENDPOINT` | MinerU 解析接口路径，默认 `/file_parse` |
| `MINERU_API_KEY` | 可选 Bearer Token；留空时不发送鉴权头 |
| `MINERU_EXPECTED_VERSION` | MinerU 版本锁，默认 `2.5.4`；服务返回版本头时必须完全一致 |
| `MINERU_TIMEOUT_SECONDS` | 单次 MinerU 请求超时秒数，默认 300 |
| `MINERU_FALLBACK_ENABLED` | MinerU 失败时是否回退原有解析流程，默认 `true` |

### MinerU 解析服务

MinerU 接管低质量 PDF 页和原始图片，后续的模板 Mapper、LLM 补全和业务 JSON schema
不变。Docker Compose 会从 `mineru/Dockerfile` 构建锁定版本的 MinerU HTTP 服务，
并通过 Compose 内网地址 `http://mineru:8888` 访问；本项目也保留了对独立 MinerU
服务的 HTTP 客户端兼容能力。

`MINERU_ENABLED=false` 时服务仍可启动：普通 PDF 文本层由 `pdfplumber` 处理，图片和
扫描件由配置的视觉模型处理。因此关闭 MinerU 不等于完全离线，LLM 服务仍是必需依赖。
MinerU 建议只暴露在私有网络，不直接开放公网端口。

```env
MINERU_ENABLED=true
MINERU_BASE_URL=http://<mineru-host>:8888
MINERU_ENDPOINT=/file_parse
MINERU_API_KEY=
MINERU_EXPECTED_VERSION=2.5.4
MINERU_TIMEOUT_SECONDS=120
MINERU_FALLBACK_ENABLED=true
```

`MINERU_BASE_URL` 取决于部署位置：

| skill-api | MinerU | 地址示例 |
|-----------|--------|----------|
| 非 Docker 本机运行 | 同机运行 | `http://127.0.0.1:8888` |
| Docker 容器 | macOS/Windows 宿主机 | `http://host.docker.internal:8888` |
| Docker 容器 | Linux 宿主机 | 先配置 `host-gateway`，再用 `http://host.docker.internal:8888` |
| 同一 Compose 网络 | `mineru` 服务 | `http://mineru:8888` |
| 独立服务器 | 可达的 MinerU 主机 | `http://<mineru-host>:8888` |

当前客户端兼容常见的 `POST /file_parse` 接口：以 `files` 字段上传 PDF 或原始图片，并发送
`backend`、`parse_method`、`lang_list` 等表单参数。响应可以是 JSON（Markdown 字段为
`md_content`、`markdown_content` 或 `markdown`）、直接返回的 Markdown 文本，或包含
`.md` 文件的 ZIP。

PDF 解析链路如下：

1. 每个 PDF 页先探测字符数、乱码率和关键 label bbox。
2. 合格文本页使用 `pdfplumber`，不发送给 OCR。
3. 无文本层、文本残缺或关键 label 重叠页渲染后发送给 MinerU。
4. 图片按真实文件签名校验后以原始字节发送给 MinerU，不转换成 PDF。
5. MinerU 硬失败或低置信且允许 fallback 时转 vision，并生成 blocking
   `mineru_failed` 或 `mineru_low_confidence`；禁止静默降级。
6. `MINERU_FALLBACK_ENABLED=false` 时 MinerU 失败直接返回转换错误。

可通过响应的 `meta.parser` 判断最终解析器：`local`、`mineru`、`pdfplumber`、`vision`
或 `mixed`。`meta.page_routes` 保留每页解析器、质量指标、置信度和问题代码。
`meta.source_sha256` 和 `meta.content_sha256` 分别标识原附件与转换文本，
`meta.conversion_status` 为 `converted` 或 `needs_review`。纯 vision、无转换文本或存在解析器
复核项时固定返回 `needs_review`，不能静默进入可下单状态。
Excel/Word 还会返回 `meta.coverage`：Excel 记录工作表、合并区域、公式和缓存值覆盖；
Word 记录 story parts、文本框和内嵌图片覆盖。`coverage.complete=false` 时同样固定为
`needs_review`。
MinerU 请求参数由代码中的 `MINERU_REQUEST_PROFILE` 固定。服务若返回
`X-MinerU-Version`（或 `MinerU-Version`）响应头，必须与 `MINERU_EXPECTED_VERSION`
完全一致；缺少版本头时记录 `mineru_version_header_missing` 警告并继续解析，明确返回的
版本不一致时才作为契约错误处理且不执行 fallback。

抽取 prompt 会根据文档标题/字段和已知模板指纹本地路由，只注入一个相关 few-shot
样例；结构化输出 schema 由网关的 `response_format` 传递，不再重复注入 Markdown
版 schema 和展示规范。MinerU 返回的正文保持全量传给模型，不做截断。`tuoshu_prompt_built`
日志会记录 `system_chars`、`few_shot_chars` 和 `document_chars`，可与网关返回的
`usage.prompt_tokens` 对照排查成本变化。

四份已人工核验文档、固定解析文本、期望路由和期望 JSON 位于 `tests/golden/tuoshu/`。
普通 `pytest` 会校验资产哈希、本地解析输出和最终 Schema；`make test-golden` 会调用
LLM 并输出字段级 diff，`make test-mineru-golden` 会调用 MinerU 并输出原始解析层 diff。
