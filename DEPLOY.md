# 部署交接文档

给龙虾（部署平台）或运维接手时看的文档。

## 基础信息

| 项 | 值 |
|----|----|
| 项目名 | `skill-api` |
| 仓库 | `http://gitlab.jxt56.cn/knight/skill-api.git` |
| 部署分支 | `knight` |
| 语言 | Python 3.11 |
| 服务类型 | HTTP API（内网） |

## 构建

方式：Docker（`Dockerfile` 在仓库根目录）

```bash
docker build -t skill-api:<tag> .
```

镜像自带：
- Python 3.11 运行时
- FastAPI + uvicorn
- LibreOffice（用于 `.doc` 文件转换，可选）
- 中日韩字体 `fonts-noto-cjk`

镜像大小约 800MB（含 LibreOffice）。如果确定不接 `.doc` 文件，可以在 Dockerfile 里去掉 LibreOffice 安装那段，镜像会小到 ~200MB。

## 运行

- **容器端口**：`8080`
- **启动命令**：镜像 `CMD` 已配置为 `uvicorn app.main:app --host 0.0.0.0 --port 8080`，直接 `docker run` 或 K8s Deployment 即可
- **资源建议**：CPU 1c / 内存 1Gi 起步。抽取任务本身不吃 CPU（等 LLM 网关返回），主要是内存要够存 references 和上传文件

## 健康检查

```
GET /healthz
```

返回：
```json
{"status": "ok", "skills": ["tuoshu"]}
```

HTTP 200 表示正常。K8s liveness/readiness probe 都用这个。

## 环境变量

完整清单在仓库根目录 `.env.example`。

### 必填（敏感，走 Secret）

| 变量 | 说明 |
|------|------|
| `OPENCLAW_API_KEY` | openclaw 网关的 Bearer Token |
| `API_KEY` | 客户端调用本服务时需要携带的 `X-API-Key` header |

### 必填（普通环境变量）

| 变量 | 说明 |
|------|------|
| `OPENCLAW_BASE_URL` | openclaw 网关地址，如 `http://192.168.0.130:18789/v1` |
| `LLM_MODEL_DEFAULT` | 网关模型名，默认 `openclaw` |

### 可选（有默认值）

| 变量 | 默认 | 说明 |
|------|------|------|
| `API_HOST` | `0.0.0.0` | 监听地址 |
| `API_PORT` | `8080` | 监听端口 |
| `LLM_TIMEOUT_SECONDS` | `180` | LLM 调用超时 |
| `LLM_MAX_RETRIES` | `2` | LLM 调用重试次数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `STORAGE_DIR` | `./storage` | 本地存储目录（当前版本未强用） |

## 持久化

当前版本**无强持久化需求**。如果后续加了文件缓存/结果留档，会写到 `/app/storage/`。届时挂一个 volume 即可。

## 部署方式建议

### K8s（推荐）

- Deployment + Service + Ingress
- ConfigMap 存普通变量
- Secret 存 `OPENCLAW_API_KEY` 和 `API_KEY`
- liveness/readiness probe 打 `/healthz`
- 副本数 1-2 起步（服务无状态，可以随意扩）

### Docker Compose（简易内网单机）

仓库根目录有 `docker-compose.yml`，改一下 `env_file` 或直接写 `environment:` 也行。

## 触发方式

希望：**`knight` 分支 push 后自动构建 + 部署**。

如果需要手动 build 触发（比如从其他分支临时发一版），支持传 tag 参数。

## 常见排查

### 服务起不来
- 检查环境变量是否齐全（尤其 `OPENCLAW_API_KEY`）
- 看容器日志：`docker logs <container>` 或 `kubectl logs <pod>`

### 调用返回 401
- 客户端漏传 `X-API-Key` header，或值和 `API_KEY` 环境变量不匹配

### 调用返回 502
- 网关不通：检查 `OPENCLAW_BASE_URL` 网络可达
- 网关鉴权失败：检查 `OPENCLAW_API_KEY` 是否有效

### 调用超时
- LLM 抽取本身耗时 10-60s 属正常
- 反向代理 / Ingress 的超时时间要 >= 180s，否则会被截断

## 联系

代码问题：仓库 issues 或直接找当前维护者
