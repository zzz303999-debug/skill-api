# skill-api

统一的 skill 抽取 API 服务。把散落的 Claude/OpenCode skill 封装成 HTTP 接口，供业务系统调用。

## 特性

- **多 skill 可扩展**：每个 skill 一个子目录，启动自动发现并挂路由
- **每个 skill 独立强类型契约**：OpenAPI 文档里能看到各 skill 精确的输入输出 schema
- **统一 LLM 出口**：所有 skill 通过 `app.llm` 调 OpenClaw 网关（OpenAI 兼容协议）
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
├── scripts/deploy.sh
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

### 交付给部署平台（如"龙虾" Agent）

把整个 repo 目录打包交付即可。平台需要知道的信息：

| 项 | 值 |
|----|----|
| 镜像构建 | `docker build -t skill-api .` |
| 容器端口 | `8080` |
| 健康检查 | `GET /healthz` |
| 需注入的环境变量 | 见 `.env.example`（`OPENCLAW_*` 和 `API_KEY` 必填） |
| 持久化目录（可选） | `/app/storage` |

**敏感变量**（走平台 Secret）：`OPENCLAW_API_KEY`、`API_KEY`
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

响应：
```json
{
  "skill": "tuoshu",
  "version": "0.1.0",
  "data": { /* skill 自己的 output_model */ },
  "meta": { "model": "deepseek-v4-flash", "usage": {...} }
}
```

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
| `OPENCLAW_BASE_URL` | 网关地址，默认 `http://192.168.0.130:18789/v1` |
| `OPENCLAW_API_KEY` | 网关 Bearer Token |
| `LLM_MODEL_DEFAULT` | 默认模型，`deepseek-v4-flash` |
| `API_KEY` | 客户端调用需要的 `X-API-Key` |
