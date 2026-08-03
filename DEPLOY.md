
# 部署文档

本文档是运维/部署平台的**唯一交接依据**。开发细节与接口说明见 README.md。

## 1. 服务概览

| 项 | 值 |
|----|----|
| 项目名 | `skill-api` |
| 代码仓库 | `https://github.com/zzz303999-debug/skill-api.git` |
| 部署分支 | `dev`（**手动部署**，无 CI 自动部署） |
| 语言/运行时 | Python 3.11（镜像内自带） |
| 服务类型 | HTTP API（内网/受控访问） |
| 服务端口 | `9000` |
| 健康检查 | `GET /healthz` |

### 外部依赖

```text
调用方
  └── skill-api:9000
        ├── OpenAI-compatible LLM（托书抽取必需）
        ├── MinerU（同栈容器，提高图片/扫描 PDF 解析质量）
        └── 订单接口（仅 POST /orders 需要）
```

| 组件 | 是否在本仓库 | 是否必需 | 不可用时的影响 |
|------|--------------|----------|----------------|
| `skill-api` | 是 | 是 | 整个 API 不可用 |
| LLM 服务 | 否 | 必需 | 文件转换可能成功，但 LLM 抽取失败 |
| MinerU | 是（独立容器） | 扫描件高质量解析需要 | 关闭时图片/扫描件改走 vision |
| 订单接口 | 否 | 仅 `/orders` 必需 | `/orders` 返回 `502/503`，托书抽取不受影响 |

## 2. 部署方式选择

| 方式 | 适用场景 | 优点 | 缺点 |
|------|----------|------|------|
| **A. 镜像部署（推荐）** | 可访问 GitHub/GHCR | 升级回滚快、服务器不构建 API | 需先打 tag 发布镜像 |
| **B. 源码构建** | 内网单机、无法访问 GHCR | 一条命令全自动、带回滚 | 服务器需构建，耗时较长 |
| **C. uv + systemd** | 不想用 Docker | 轻量、无容器层 | 需手动装系统依赖，无自动回滚 |

**推荐方式 A**；若服务器无法访问 GitHub/GHCR，用方式 B。

## 3. 服务器要求

- OS：Linux x86_64（Ubuntu 22.04+ / Debian 12+ 已验证系）
- 软件：Docker Engine + Compose plugin（方式 A/B）；Git（方式 B）
- 资源：
  - `skill-api`：CPU 1c / 内存 1Gi 起步。抽取任务本身不吃 CPU（等 LLM 网关返回），主要是内存要存 references 和上传文件
  - MinerU（CPU 模式）：8c / 16Gi 内存 / 30Gi 可用磁盘起步，按真实文档压测调整；模型在首次镜像构建时下载
- 出站网络：放通到 LLM API、MinerU（同机容器无需）、订单接口（仅 `/orders` 时）
- 若启用 MinerU 且服务器使用 NVIDIA GPU：需按实际 CUDA 镜像要求替换 `mineru/Dockerfile` 并安装 NVIDIA Container Toolkit；只要镜像仍提供 `mineru-api --host 0.0.0.0 --port 8888`，API 无需修改

## 4. 首次部署

### 4.1 准备 `.env`（两种方式都需要）

```bash
cp .env.example .env
# 编辑 .env，至少填写：
#   LLM_BASE_URL       LLM API 地址，如 https://<llm-host>/v1
#   LLM_API_KEY        Bearer Token（敏感）
#   LLM_MODEL_DEFAULT  服务端支持的模型名
chmod 600 .env
```

- `.env` 含敏感信息（`LLM_API_KEY`、`MINERU_API_KEY`、订单凭据），**禁止提交仓库**，权限收紧至 600，仅部署用户可读
- 完整变量清单见第 5 节；不用的功能（订单接口/MinerU）可保留默认值

### 4.2 方式 A：镜像部署

先由开发侧发布镜像到 GHCR（见第 6 节），然后在服务器执行：

```bash
# 私有 GHCR 包需先用只读 Packages Token 登录
echo "$GHCR_READONLY_TOKEN" | docker login ghcr.io -u <用户名> --password-stdin

cd <仓库检出目录>   # 需包含 docker-compose.deploy.yml 与 mineru/Dockerfile
export SKILL_API_IMAGE=ghcr.io/zzz303999-debug/skill-api:v0.1.0

docker compose -f docker-compose.deploy.yml config --quiet
docker compose -f docker-compose.deploy.yml build --pull mineru   # 首次需构建 MinerU
docker compose -f docker-compose.deploy.yml up -d
docker compose -f docker-compose.deploy.yml ps
```

- **生产禁止使用 `latest` 标签**，`SKILL_API_IMAGE` 必须显式指定具体版本（缺省时 compose 会直接报错）
- MinerU 镜像在服务器本地构建一次，模型随镜像发布；之后更新 MinerU 版本或模型源时重新构建即可
- 仓库检出目录只需保持 compose 文件与 `.env` 最新，可 `git pull` 更新

### 4.3 方式 B：源码构建（deploy.sh）

服务器首次部署：安装 Git、Docker Engine 和 Compose plugin，然后：

```bash
git clone https://github.com/zzz303999-debug/skill-api.git
cd skill-api
git checkout dev
cp .env.example .env
# 编辑 .env，填入生产地址和 Secret
chmod +x scripts/deploy.sh
./scripts/deploy.sh deploy
```

脚本流程：`git pull --ff-only origin/dev` → `compose build --pull` → 停旧容器 → 起新容器 → 健康检查，**新容器健康检查失败时自动回滚到部署前的镜像**。要求：

- 生产目录的 tracked 文件必须没有本地修改（脚本会拒绝执行）
- 只在 `dev` 分支执行（脚本会校验）
- 同时构建并启动 MinerU（`docker-compose.yml`），API 通过 `http://mineru:8888` 访问

### 4.4 方式 C：uv + systemd（非 Docker，备选）

服务器安装 Python 3.11、uv、LibreOffice、`fonts-noto-cjk` 和 `curl`：

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

`/etc/systemd/system/skill-api.service`：

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

`Restart=always` 处理进程退出，但不能代替外部监控（见第 10 节）。

## 5. 环境变量清单（完整）

与仓库根目录 `.env.example` 一一对应。

### 必填（敏感，走 Secret 管理）

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | LLM 服务的 Bearer Token |
| `MINERU_API_KEY` | MinerU 服务鉴权 Key（启用且需要时） |
| `ORDER_API_EXT_APP_ID`、`ORDER_API_EXT_USER_ID`、`ORDER_API_JXT_OPEN_ID`、`ORDER_API_USER_ID` | 订单接口凭据（仅使用 `/orders` 时必填） |

### 必填（普通环境变量）

| 变量 | 说明 |
|------|------|
| `LLM_BASE_URL` | OpenAI-compatible API 地址，如 `https://api.example.com/v1` |
| `LLM_MODEL_DEFAULT` | 服务端支持的模型名，默认 `deepseek-v4-flash` |
| `ORDER_API_URL` | 订单接口地址（仅使用 `/orders` 时必填，需覆盖默认的预发地址） |

### 可选（有默认值）

| 变量 | 默认 | 说明 |
|------|------|------|
| `API_HOST` | `0.0.0.0` | 监听地址 |
| `API_PORT` | `9000` | 监听端口 |
| `API_MAX_UPLOAD_BYTES` | `20971520` | 单文件上传上限（20MB），须与反代 `client_max_body_size` 一致 |
| `API_BATCH_MAX_FILES` | `10` | 批量抽取单次最大文件数 |
| `SKILL_MAX_CONCURRENCY` | `4` | skill 并发数 |
| `LLM_TIMEOUT_SECONDS` | `180` | LLM 调用超时 |
| `LLM_MAX_RETRIES` | `2` | LLM 调用重试次数 |
| `LLM_THINKING_MODE` | `disabled` | 思考模式 `disabled`/`enabled`；disabled 降低 reasoning token 与耗时，模型不支持时自动降级 |
| `VISION_MAX_PDF_PAGES` | `10` | 扫描 PDF 视觉识别最大页数 |
| `VISION_PDF_RENDER_SCALE` | `2.0` | 扫描 PDF 渲染缩放 |
| `IMAGE_VISION_SKIP_WHEN_CONFIDENT` | `true` | 图片 OCR 高置信时跳过 LLM vision 交叉核验（提速） |
| `PARSER_TEXT_MIN_CHARS` | `50` | PDF 文本层页级质量探测最小字符数 |
| `PARSER_GARBLED_RATIO_THRESHOLD` | `0.05` | PDF 文本层乱码比例阈值 |
| `MINERU_ENABLED` | `true`（.env.example） | 是否启用 MinerU；代码默认 `false`，Compose 场景需显式 `true` |
| `MINERU_BASE_URL` | `http://mineru:8888` | MinerU 服务地址（Compose 同栈） |
| `MINERU_ENDPOINT` | `/file_parse` | MinerU 解析端点 |
| `MINERU_EXPECTED_VERSION` | `2.5.4` | 期望的 MinerU 版本，不匹配时降级并告警 |
| `MINERU_TIMEOUT_SECONDS` | `300` | MinerU 调用超时 |
| `MINERU_FALLBACK_ENABLED` | `true` | MinerU 失败时降级到内置解析器 |
| `MINERU_OCR_CONCURRENCY` | `4` | MinerU OCR 并发度 |
| `MINERU_VERSION` | `2.5.4` | MinerU 镜像版本（compose 构建参数） |
| `MINERU_IMAGE` | `skill-api-mineru:2.5.4` | MinerU 本地镜像标签 |
| `MINERU_MODEL_SOURCE` | `modelscope` | MinerU 模型下载源（`modelscope`/`huggingface`） |
| `MINERU_SHM_SIZE` | `8g` | MinerU 容器共享内存 |
| `MINERU_COMMAND` | `mineru-api --host 0.0.0.0 --port 8888` | MinerU 容器启动命令 |
| `STORAGE_DIR` | `./storage` | 本地存储目录（容器内为 `/app/storage`） |
| `STORAGE_KEEP_HOURS` | `24` | 临时文件保留小时数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `SKILL_API_IMAGE` | 无默认 | 镜像部署时指定 API 镜像版本（见 4.2），**禁止 latest** |

## 6. 镜像发布（手动，开发侧执行）

代码推送 `dev` 分支**不会**触发任何构建或部署。需要发版时：

```bash
git tag v0.1.0
git push origin v0.1.0
```

GitHub Actions（`Publish image`）自动构建并推送 `ghcr.io/zzz303999-debug/skill-api:v0.1.0`（同时打 sha 标签）。推送后通知运维按 4.2 更新即可。

## 7. 日常运维命令

### 方式 B（deploy.sh）

```bash
./scripts/deploy.sh status    # 查看容器状态
./scripts/deploy.sh logs      # 跟踪日志（tail 200）
./scripts/deploy.sh restart   # 重启并等待健康检查
./scripts/deploy.sh stop      # 停止全部容器
./scripts/deploy.sh start     # 启动并等待健康检查
```

### 方式 A（镜像模式）

```bash
cd <仓库检出目录>
docker compose -f docker-compose.deploy.yml ps
docker compose -f docker-compose.deploy.yml logs --tail=200 skill-api
docker compose -f docker-compose.deploy.yml restart
docker compose -f docker-compose.deploy.yml down
docker compose -f docker-compose.deploy.yml up -d
```

## 8. 更新与回滚

### 方式 B

```bash
./scripts/deploy.sh deploy
```

脚本拉取 `dev` 最新代码并本地构建；健康检查失败时自动回滚到部署前的镜像。

### 方式 A

```bash
# 更新：改 .env 中 SKILL_API_IMAGE 为新版本，然后
docker compose -f docker-compose.deploy.yml pull skill-api
docker compose -f docker-compose.deploy.yml up -d

# 回滚：改回上一个已验证版本，重新 pull + up -d
```

## 9. HTTPS 反向代理（推荐）

云服务器对外建议由 Nginx/Caddy 提供 HTTPS，`9000` 只对反向代理开放。Compose 端口映射改为 `127.0.0.1:9000:9000`，避免绕过 HTTPS 直接访问 Uvicorn。Nginx 至少应包含：

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
        proxy_read_timeout 210s;   # 必须 >= LLM_TIMEOUT_SECONDS + 余量
        proxy_send_timeout 210s;
    }
}
```

证书路径由部署环境的 ACME/证书管理工具生成并按实际域名替换。**安全组对公网只开放 `80/443`，不开放 `9000`。**

如 LLM/MinerU 运行在宿主机而非 Compose 网络，需给 `skill-api` 增加 `extra_hosts: ["host.docker.internal:host-gateway"]`，并使用 `http://host.docker.internal:<port>`（容器内 `127.0.0.1` 只代表容器自身）。

## 10. 健康检查与监控

```
GET /healthz
```

```json
{"status": "ok", "skills": ["tuoshu"]}
```

HTTP 200 且 `status=ok` 表示进程可响应。**注意：`/healthz` 不请求 LLM、MinerU 或订单接口**，`200` 只表示 FastAPI 进程存活，不表示端到端可用。

- Docker 镜像自带 HEALTHCHECK，K8s liveness/readiness probe 也用它
- 建议从另一台机器或云拨测每分钟请求 `/healthz`，连续失败发告警（企业微信等）
- 请求日志：写入 `storage/logs/requests.jsonl`（服务重启仍可查），浏览器访问 `http://<host>:9000/logs` 查看，接口为 `GET /api/logs`

## 11. 持久化与数据

- 当前版本**无强持久化需求**：存储目录 `/app/storage`（compose 已挂载 `./storage`），用于临时文件与请求日志
- `STORAGE_KEEP_HOURS=24` 自动清理过期临时文件
- 建议定期备份：`./storage` 目录 + `.env` 文件

## 12. 上线验收

1. 存活检查必须返回 `status=ok` 且包含 `tuoshu`：

   ```bash
   curl -fsS http://127.0.0.1:9000/healthz
   ```

2. 用非生产样例文档做端到端抽取：

   ```bash
   curl -fsS -X POST http://127.0.0.1:9000/skills/tuoshu/extract \
     -F "file=@./sample.pdf"
   ```

3. 确认返回中存在 `data`、`meta.model`、`meta.parser` 和 `content`。
4. 如启用 MinerU，确认 `meta.parser` 或 `meta.page_routes[].parser` 中存在 `mineru`；如出现 `mineru_failed`/`mineru_low_confidence`，查看网络、版本和 MinerU 日志。
5. 如使用 `/orders`，用测试账号单独验证一次；该接口会真实创建订单，**不得用生产数据反复重试**。

## 13. 常见排查

### 服务起不来
- 检查环境变量是否齐全（尤其 `LLM_API_KEY`）
- 看容器日志：`docker logs <container>` 或 `journalctl -u skill-api -n 200`

### 调用返回 502
- LLM 服务不通：检查 `LLM_BASE_URL` 网络可达
- LLM 服务鉴权失败：检查 `LLM_API_KEY` 是否有效

### 调用超时
- LLM 抽取本身耗时 10-60s 属正常
- 反向代理 / Ingress 的超时时间要 >= 210s，否则会被截断（见第 9 节）

### `.doc` 文件转换失败（返回 `SCAN_OR_IMAGE_HINT`）
- 镜像内置 LibreOffice，确认容器内 `soffice` 可用；非 Docker 部署需安装 LibreOffice + `fonts-noto-cjk`
- 检查临时目录可写性（`$TMPDIR`）与文件权限，错误 `details.reason` 中有具体原因

### MinerU 不可用
- `MINERU_BASE_URL` 网络可达；版本与 `MINERU_EXPECTED_VERSION` 一致
- `MINERU_SHM_SIZE` 不足时增大 `shm_size`
- 模型下载失败时切换 `MINERU_MODEL_SOURCE`
