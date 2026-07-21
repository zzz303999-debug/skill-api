# skill-api

统一的 skill 抽取 API 服务。把散落的 Claude/OpenCode skill 封装成带强类型契约的
HTTP 接口，供业务系统调用。目前内置海运托书抽取，并支持通过 MinerU 增强 PDF
结构化解析。

## 特性

- **多 skill 可扩展**：每个 skill 一个子目录，启动自动发现并挂路由
- **每个 skill 独立强类型契约**：OpenAPI 文档里能看到各 skill 精确的输入输出 schema
- **统一 LLM 出口**：所有 skill 通过 `app.llm` 调 OpenClaw 网关（OpenAI 兼容协议）
- **PDF 多级解析**：可选 MinerU，失败时按配置降级到 `pdfplumber` 和视觉模型
- **同步 API**：先跑可行版本，后续可无缝加异步任务队列
- **Docker 部署**：`docker compose up -d` 一键起

## 目录结构

```
skill-api/
├── app/
│   ├── main.py                  # FastAPI 入口，启动时自动注册 skill
│   ├── config.py                # 环境变量
│   ├── deps.py                  # 鉴权等通用依赖
│   ├── errors.py                # 统一错误类型
│   ├── logging_conf.py          # JSON 日志
│   ├── llm/openclaw.py          # OpenClaw 网关客户端（唯一 LLM 出口）
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
cp .env.example .env             # 填 OPENCLAW_API_KEY 等
make install                     # uv venv + 装依赖
make dev                         # uvicorn --reload
```

访问：

- `http://localhost:8080/docs`  — Swagger UI
- `http://localhost:8080/healthz`
- `http://localhost:8080/skills` — 已注册的 skill 列表

### Docker 部署

```bash
cp .env.example .env             # 填生产配置
docker compose up -d --build
docker compose logs -f
```

### 龙虾环境按版本部署

GitHub 推送 `v*` tag 后，Actions 会构建并发布 GHCR 镜像。龙虾不需要 Git，使用
只读的 GitHub Packages Token 登录一次，然后按明确版本部署：

```bash
export SKILL_API_IMAGE=ghcr.io/zzz303999-debug/skill-api:v0.1.0
docker compose -f docker-compose.deploy.yml pull
docker compose -f docker-compose.deploy.yml up -d
```

生产环境不要使用 `latest`；回滚时把 `SKILL_API_IMAGE` 改回上一个版本并重新执行
`up -d`。`.env` 始终由龙虾环境维护，不进入 GitHub。

### 交付给部署平台（如"龙虾" Agent）

把整个 repo 目录打包交付即可。平台需要知道的信息：

| 项 | 值 |
|----|----|
| 镜像构建 | `docker build -t skill-api .` |
| 容器端口 | `8080` |
| 健康检查 | `GET /healthz` |
| 需注入的环境变量 | 见 `.env.example`（`OPENCLAW_*` 和 `API_KEY` 必填） |
| 持久化目录（可选） | `/app/storage` |

**敏感变量**（走平台 Secret）：`OPENCLAW_API_KEY`、`API_KEY`、`MINERU_API_KEY`（如启用）

**普通变量**（走 ConfigMap / 环境变量）：其余

## 接口

### `GET /skills`
返回所有已注册 skill 及元数据。

### `GET /healthz`
健康检查。

### `POST /skills/{skill_name}/extract`
运行指定 skill，返回结构化 JSON。

请求：`multipart/form-data`

- `file`：上传文件
- header：`X-API-Key: <API_KEY>`

示例：

```bash
curl -X POST http://localhost:8080/skills/tuoshu/extract \
  -H "X-API-Key: change-me-in-prod" \
  -F "file=@./order.pdf"
```

响应：

```json
{
  "skill": "tuoshu",
  "version": "0.1.0",
  "data": {},
  "meta": {
    "model": "openclaw",
    "usage": {},
    "parser": "mineru",
    "parser_fallback": false
  }
}
```

`parser` 和 `parser_fallback` 仅在启用 MinerU 且处理 PDF 时返回。

### `POST /skills/{skill_name}/batch-extract`

一次上传多个文件。服务会在全局并发限制内执行，逐文件返回 `result` 或结构化
`error`，单个文件失败不会中断其他文件。

上传限制由 `API_MAX_UPLOAD_BYTES`、`API_BATCH_MAX_FILES` 控制，Skill/LLM 总并发由
`SKILL_MAX_CONCURRENCY` 控制。

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
| `OPENCLAW_BASE_URL` | 龙虾网关地址，由部署环境注入；本地默认 `http://127.0.0.1:18789/v1` |
| `OPENCLAW_API_KEY` | 网关 Bearer Token |
| `LLM_MODEL_DEFAULT` | 龙虾网关模型或 agent 名，默认 `openclaw` |
| `API_KEY` | 客户端调用需要的 `X-API-Key` |
| `API_MAX_UPLOAD_BYTES` | 单文件最大字节数，默认 20 MiB |
| `API_BATCH_MAX_FILES` | 单批最大文件数，默认 10 |
| `SKILL_MAX_CONCURRENCY` | 单进程 Skill/LLM 最大并发数，默认 4 |
| `VISION_MAX_PDF_PAGES` | 扫描 PDF 最多渲染页数，默认 3 |
| `VISION_PDF_RENDER_SCALE` | 扫描 PDF 渲染倍率，默认 2.0 |
| `MINERU_ENABLED` | 是否让 PDF 优先使用 MinerU，默认 `false` |
| `MINERU_BASE_URL` | MinerU HTTP 服务地址 |
| `MINERU_ENDPOINT` | MinerU 解析接口路径，默认 `/file_parse` |
| `MINERU_API_KEY` | 可选 Bearer Token；留空时不发送鉴权头 |
| `MINERU_TIMEOUT_SECONDS` | 单次 MinerU 请求超时秒数，默认 120 |
| `MINERU_FALLBACK_ENABLED` | MinerU 失败时是否回退原有解析流程，默认 `true` |
| `MINERU_BACKEND` | MinerU 后端，默认 `pipeline` |
| `MINERU_PARSE_METHOD` | MinerU 解析方式，默认 `auto` |
| `MINERU_LANGUAGE` | MinerU 文档语言，默认 `ch` |

### 可选：接入 MinerU

MinerU 只接管 PDF 转 Markdown，后续的 LLM 抽取和业务 JSON schema 不变；Excel、
Word 和图片仍走原有流程。先确保 MinerU 的 HTTP 服务可从 `skill-api` 所在环境访问，
再配置。本项目提供 MinerU 客户端，不包含 MinerU 服务本身。

```env
MINERU_ENABLED=true
MINERU_BASE_URL=http://host.docker.internal:8000
MINERU_ENDPOINT=/file_parse
MINERU_API_KEY=
MINERU_TIMEOUT_SECONDS=120
MINERU_FALLBACK_ENABLED=true
MINERU_BACKEND=pipeline
MINERU_PARSE_METHOD=auto
MINERU_LANGUAGE=ch
```

`MINERU_BASE_URL` 取决于部署位置：

| skill-api | MinerU | 地址示例 |
|-----------|--------|----------|
| 本机运行 | 本机运行 | `http://127.0.0.1:8000` |
| Docker 容器 | macOS/Windows 宿主机 | `http://host.docker.internal:8000` |
| 同一 Compose 网络 | `mineru` 服务 | `http://mineru:8000` |
| 独立服务器 | 可达的 MinerU 主机 | `http://<mineru-host>:8000` |

当前客户端兼容常见的 `POST /file_parse` 接口：以 `files` 字段上传 PDF，并发送
`backend`、`parse_method`、`lang_list` 等表单参数。响应可以是 JSON（Markdown 字段为
`md_content`、`markdown_content` 或 `markdown`）、直接返回的 Markdown 文本，或包含
`.md` 文件的 ZIP。

PDF 解析链路如下：

1. `MINERU_ENABLED=false`：直接使用 `pdfplumber`；扫描件再交给 vision。
2. `MINERU_ENABLED=true`：优先调用 MinerU。
3. MinerU 调用失败、超时或未返回 Markdown，且 `MINERU_FALLBACK_ENABLED=true`：
   自动降级到 `pdfplumber`；若仍判定为扫描件，再交给 vision。
4. `MINERU_FALLBACK_ENABLED=false`：MinerU 失败时直接返回转换错误。

启用 MinerU 后，可通过响应的 `meta.parser` 判断最终使用的解析器：`mineru`、
`pdfplumber` 或 `vision`。`meta.parser_fallback=true` 表示本次请求发生过 MinerU 降级。
服务日志中的 `mineru_parse_succeeded` 和 `mineru_parse_fallback` 可用于排查调用情况。
