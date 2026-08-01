# 部署交接文档

给部署平台或运维接手时看的文档。

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

- **容器端口**：`9000`
- **启动命令**：镜像 `CMD` 已配置为 `uvicorn app.main:app --host 0.0.0.0 --port 9000`，直接 `docker run` 或 K8s Deployment 即可
- **资源建议**：CPU 1c / 内存 1Gi 起步。抽取任务本身不吃 CPU（等 LLM 网关返回），主要是内存要够存 references 和上传文件
- **MinerU 资源建议**：CPU 模式建议从 8c / 16Gi 内存 / 30Gi 可用磁盘起步，并按真实文档压测调整；模型在首次镜像构建时下载

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
| `LLM_API_KEY` | OpenAI-compatible LLM 服务的 Bearer Token |

### 必填（普通环境变量）

| 变量 | 说明 |
|------|------|
| `LLM_BASE_URL` | OpenAI-compatible API 地址，如 `https://api.example.com/v1` |
| `LLM_MODEL_DEFAULT` | 服务端支持的模型名，默认 `gpt-5.4` |

### 可选（有默认值）

| 变量 | 默认 | 说明 |
|------|------|------|
| `API_HOST` | `0.0.0.0` | 监听地址 |
| `API_PORT` | `9000` | 监听端口 |
| `LLM_TIMEOUT_SECONDS` | `180` | LLM 调用超时 |
| `LLM_MAX_RETRIES` | `2` | LLM 调用重试次数 |
| `LLM_THINKING_MODE` | `disabled` | 思考模式 `disabled`/`enabled`；disabled 降低 reasoning token 与耗时，模型不支持时自动降级 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `STORAGE_DIR` | `./storage` | 本地存储目录（当前版本未强用） |

## 持久化

当前版本**无强持久化需求**。如果后续加了文件缓存/结果留档，会写到 `/app/storage/`。届时挂一个 volume 即可。

## 部署方式建议

### Docker Compose（简易内网单机）

仓库根目录有 `docker-compose.yml`，会同时构建 `skill-api` 和 MinerU 容器。生产服务器
推荐直接使用：

```bash
cp .env.example .env
# 编辑 .env，填写 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL_DEFAULT
chmod +x scripts/deploy.sh
./scripts/deploy.sh deploy
```

脚本默认拉取 `origin/knight`，并在构建完成后停止旧容器、启动新容器、等待两个容器健康。
常用运维命令为 `./scripts/deploy.sh {status|logs|restart|stop|start}`。MinerU 模型随镜像发布，
不依赖运行时 Docker volume；更新 MinerU 版本或模型源时重新构建镜像即可。

默认 MinerU 镜像为仓库内 Dockerfile 构建的 CPU pipeline。若服务器使用 NVIDIA GPU，
应按实际 CUDA 镜像要求替换 `mineru/Dockerfile` 并安装 NVIDIA Container Toolkit；
只要镜像仍提供同一 `mineru-api --host 0.0.0.0 --port 8888` 命令，API 无需修改。

## 触发方式

希望：**`knight` 分支 push 后自动构建 + 部署**。

如果需要手动 build 触发（比如从其他分支临时发一版），支持传 tag 参数。

## 常见排查

### 服务起不来
- 检查环境变量是否齐全（尤其 `LLM_API_KEY`）
- 看容器日志：`docker logs <container>` 或 `kubectl logs <pod>`

### 调用返回 502
- LLM 服务不通：检查 `LLM_BASE_URL` 网络可达
- LLM 服务鉴权失败：检查 `LLM_API_KEY` 是否有效

### 调用超时
- LLM 抽取本身耗时 10-60s 属正常
- 反向代理 / Ingress 的超时时间要 >= 180s，否则会被截断
