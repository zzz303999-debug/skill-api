
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
#   API_KEY            接口访问凭证（生产必须设置，见下方说明）
chmod 600 .env
```

- `.env` 含敏感信息（`LLM_API_KEY`、`MINERU_API_KEY`、`API_KEY`、订单凭据），**禁止提交仓库**，权限收紧至 600，仅部署用户可读
- **生产安全基线（fail-closed）**：`API_KEY` 必须设置；`deploy.sh` 部署 `docker-compose.deploy.yml`
  时会强制检查，未设置直接拒绝上线。限流不强制，但建议保持开启
  （`RATE_LIMIT_ENABLED=true`），可通过 `RATE_LIMIT_HEAVY_MAX_REQUESTS` 等阈值调宽避免误伤
- 启用 `API_KEY` 后，除 `/healthz`、`/docs` 等豁免路径外，所有接口（含 `/orders`、`/skills/*`、`/api/logs`）
  必须携带 `Authorization: Bearer <API_KEY>` 或 `X-API-Key: <API_KEY>`；
  日志页面 `/logs` 首次访问会提示输入 API Key 并保存在浏览器本地
- 完整变量清单见第 5 节；不用的功能（订单接口/MinerU）可保留默认值
- **`APP_ENV` 必须按环境设置（2026-08-31 事故教训）**：决定加载
  `config/fee_price_map.{env}.yaml`，默认 `test`；生产必须显式设
  `APP_ENV=prod`，否则费目映射表缺失/错位，账单导入
  （`/orders/bill/import`）会 500 `internal_error`
  （fail fast：`fee price map missing`）；
  同时箱型白名单、建档配置随镜像分发，缺失时会静默降级（见第 12 节）

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
| `API_KEY` | 服务接口访问凭证（生产必须设置；未设置则接口无鉴权，仅限可信内网） |
| `MINERU_API_KEY` | MinerU 服务鉴权 Key（启用且需要时） |

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
| `STORAGE_KEEP_HOURS` | `24` | 日志/临时文件保留小时数；按天日志文件按日志时间整文件清理（粒度天） |
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

云服务器对外建议由 Nginx 提供 HTTPS，`9000` 只对反向代理开放。
`docker-compose.deploy.yml` 已默认将端口映射改为 `127.0.0.1:9000:9000`（仅本机可访问），
仓库提供可直接使用的模板：`deploy/nginx/skill-api.conf`。**证书使用 certbot 自动签发，自动续期**：

```bash
# 1. 安装 certbot（CentOS 8+ / RHEL 系）
dnf install -y certbot python3-certbot-nginx

# 2. 替换模板中 server_name 为真实域名，复制到 conf.d 并重载
export DOMAIN=your-domain.example.com
sed -i "s/skill-api.example.com/${DOMAIN}/g" deploy/nginx/skill-api.conf
cp deploy/nginx/skill-api.conf /etc/nginx/conf.d/skill-api.conf
nginx -t && systemctl reload nginx

# 3. certbot 自动签发并写入证书配置（80 端口须已开放，HTTP-01 验证）
certbot --nginx -d ${DOMAIN} --redirect

# 4. 续期由 certbot.timer 自动完成；可手动验证：
certbot renew --dry-run
```

签发后 certbot 会自动写入证书路径（`/etc/letsencrypt/live/<域名>/`）、启用 443 并把 80 改为 301 跳转；
模板内已放行 `/.well-known/acme-challenge/` 验证路径，代理参数（X-Forwarded-For 追加语义、
`client_max_body_size 20m` 与 `API_MAX_UPLOAD_BYTES` 配套、`proxy_read_timeout 3600s` 等）在注释中给出参考。

**安全组对公网只开放 `80/443`，不开放 `9000`。**

如 LLM/MinerU 运行在宿主机而非 Compose 网络，需给 `skill-api` 增加 `extra_hosts: ["host.docker.internal:host-gateway"]`，并使用 `http://host.docker.internal:<port>`（容器内 `127.0.0.1` 只代表容器自身）。

## 10. 健康检查与监控

```
GET /healthz
```

```json
{
  "status": "ok",
  "skills": ["tuoshu"],
  "dependencies": {
    "llm": "ok | not_configured | unreachable | skipped",
    "mineru": "ok | disabled | unreachable | skipped",
    "order_api": "ok | not_configured | skipped"
  }
}
```

HTTP 200 且 `status=ok` 表示进程可响应。`dependencies` 反映核心依赖状态（`HEALTH_PROBE_ENABLED` 关闭时全部为 `skipped`）：

- `llm`：`GET {LLM_BASE_URL}/models` 可达性探测（只验可达，**不调 chat，不计费**）；缺 `LLM_API_KEY` 时显示 `not_configured` 且不发起请求
- `mineru`：未启用（`MINERU_ENABLED=false`）为 `disabled`；启用后探测根路径可达性，它是可降级依赖，`unreachable` 不影响整体健康判定
- `order_api`：**仅配置级检查**（`ORDER_API_URL` 是否已配置）。上游是 POST 下单端点，请求即下单，**绝不实际探测**；请求仅携带业务数据、`userId`（由上游经 `/orders` 透传）与 `roomId`，无身份凭据

注意：探测失败不改变 HTTP 200（依赖挂了只影响 `dependencies` 展示，避免网络抖动误判容器不健康）；单依赖探测超时 `HEALTH_PROBE_TIMEOUT_SECONDS`（默认 2s），总耗时低于 Docker healthcheck 的 `timeout: 5s` 预算。

- Docker 镜像自带 HEALTHCHECK，K8s liveness/readiness probe 也用它
- 建议从另一台机器或云拨测每分钟请求 `/healthz`，连续失败发告警（企业微信等）
- 请求日志：按天写入 `storage/logs/requests-YYYY-MM-DD.jsonl`（服务重启仍可查近期历史，过期文件按日志时间自动清理），浏览器访问 `http://<host>:9000/logs` 查看，接口为 `GET /api/logs`

## 11. 接口调用示例（含鉴权）

启用 `API_KEY`（生产必须）后，除豁免路径外**所有接口**必须携带凭证，两种方式任选其一：

| 方式 | Header | 值 |
|------|--------|-----|
| 方式一 | `Authorization` | `Bearer <API_KEY>` |
| 方式二 | `X-API-Key` | `<API_KEY>` |

鉴权失败返回 `401`，错误码 `unauthorized`，响应结构与业务错误一致：

```json
{"error": {"code": "unauthorized", "message": "invalid or missing API key", "description": "未授权访问，请提供有效的访问凭证（API Key）", "details": null}}
```

### curl 示例

```bash
# 健康检查（豁免路径，无需凭证）
curl -fsS http://127.0.0.1:9000/healthz

# 单文件抽取
curl -X POST http://127.0.0.1:9000/skills/tuoshu/extract \
  -H "Authorization: Bearer <API_KEY>" \
  -F "file=@托书.pdf"

# 批量抽取
curl -X POST http://127.0.0.1:9000/skills/tuoshu/batch-extract \
  -H "X-API-Key: <API_KEY>" \
  -F "file=@a.pdf" -F "file=@b.pdf"

# 附件转下单字段（只解析不下单）
curl -X POST http://127.0.0.1:9000/orders/parse-document \
  -H "Authorization: Bearer <API_KEY>" \
  -F "file=@做箱通知.docx"

# 文本转订单（JSON body；会真实创建订单，测试勿用生产数据）
curl -X POST http://127.0.0.1:9000/orders \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"content": "提单号：... 船名：...", "roomId": "room-1"}'

# 查询请求日志
curl -X GET "http://127.0.0.1:9000/api/logs?limit=50" \
  -H "Authorization: Bearer <API_KEY>"
```

### 豁免路径（无需凭证）

`/healthz`、`/skills`、`/docs`、`/redoc`、`/openapi.json`、`/favicon.ico` 及 `/logs` 页面本身。
`/api/logs`（日志数据接口，含 PII）**不在豁免内**，必须带凭证。

### Postman / Apifox 配置

1. 在接口的 **Headers** 标签添加一行：Key=`Authorization`，Value=`Bearer <API_KEY>`（`Bearer` 后有空格）
2. 也可将 `<API_KEY>` 存入工具的环境变量（如 `{{apiKey}}`），所有接口统一引用

### 日志页面（浏览器）

生产启用 `API_KEY` 后，浏览器访问 `/logs` 页面首次查询 `/api/logs` 会弹窗要求输入 API Key，
输入后保存在浏览器 localStorage，之后自动携带。

## 12. 持久化与数据

- 当前版本**无强持久化需求**：存储目录 `/app/storage`（compose 已挂载 `./storage`），用于临时文件与请求日志
- `STORAGE_KEEP_HOURS=24` 自动清理过期临时文件；请求日志按天分文件（`requests-YYYY-MM-DD.jsonl`），过期文件按日志时间整文件清理
- `config/`（费目映射/箱型白名单/建档配置）与 `templates/`（账单模板库）随镜像分发（Dockerfile COPY）；`docker-compose.deploy.yml` 另挂载 `./templates` 使 L3 固化模板跨重启持久——**挂载要求部署目录为 git 检出**（templates/ 内容与镜像一致，勿用空目录覆盖）；未挂载场景（如 K8s）容器重启后固化模板丢失，自动回退 AI 映射，功能不坏
- 建议定期备份：`./storage` 目录 + `.env` 文件

## 13. 上线验收

1. 存活检查必须返回 `status=ok` 且包含 `tuoshu`（豁免路径，无需凭证）：

   ```bash
   curl -fsS http://127.0.0.1:9000/healthz
   ```

2. 用非生产样例文档做端到端抽取（生产启用 API_KEY 后需带凭证）：

   ```bash
   curl -fsS -X POST http://127.0.0.1:9000/skills/tuoshu/extract \
     -H "Authorization: Bearer <API_KEY>" \
     -F "file=@./sample.pdf"
   ```

3. 确认返回中存在 `data`、`meta.model`、`meta.parser` 和 `content`。
4. 如启用 MinerU，确认 `meta.parser` 或 `meta.page_routes[].parser` 中存在 `mineru`；如出现 `mineru_failed`/`mineru_low_confidence`，查看网络、版本和 MinerU 日志。
5. 如使用 `/orders`，用测试账号单独验证一次；该接口会真实创建订单，**不得用生产数据反复重试**。

## 14. 常见排查

### 服务起不来
- 检查环境变量是否齐全（尤其 `LLM_API_KEY`）
- 看容器日志：`docker logs <container>` 或 `journalctl -u skill-api -n 200`

### 调用返回 502
- LLM 服务不通：检查 `LLM_BASE_URL` 网络可达
- LLM 服务鉴权失败：检查 `LLM_API_KEY` 是否有效

### 调用返回 401
- 服务已启用 `API_KEY` 鉴权：确认请求头携带 `Authorization: Bearer <API_KEY>` 或 `X-API-Key: <API_KEY>`（`Bearer` 后有空格）
- 确认 `.env` 中 `API_KEY` 与调用方使用的值一致，修改后需重启容器生效
- `/healthz` 等豁免路径不受影响（见第 11 节）

### 账单导入返回 500 internal_error
- 日志搜 `unhandled_error`：若为 `fee price map missing: fee_price_map.<env>.yaml`，
  即镜像缺 `config/`（2026-08-31 修复前镜像的已知缺陷）或 `.env` 的 `APP_ENV`
  与镜像内文件不匹配——确认镜像已更新 + `APP_ENV` 设置正确
- 确认配置齐全：`docker exec <container> ls /app/config /app/templates`；
  日志出现 `box_whitelist_unavailable`/`templates_dir_missing`/`master_data_config_missing`
  即对应配置缺失（白名单失效/模板识别退化/建档禁用），按上一条处理

### 调用超时
- LLM 抽取本身耗时 10-60s 属正常
- 反向代理 / Ingress 的超时时间要 >= 3600s，否则会被截断（见第 9 节）——竞品账单 create
  模式逐单串行创建，实测 1850 单 ≈ 54 分钟；更大账单请调用方分批导入或按需再调大

### `.doc` 文件转换失败（返回 `SCAN_OR_IMAGE_HINT`）
- 镜像内置 LibreOffice，确认容器内 `soffice` 可用；非 Docker 部署需安装 LibreOffice + `fonts-noto-cjk`
- 检查临时目录可写性（`$TMPDIR`）与文件权限，错误 `details.reason` 中有具体原因

### MinerU 不可用
- `MINERU_BASE_URL` 网络可达；版本与 `MINERU_EXPECTED_VERSION` 一致
- `MINERU_SHM_SIZE` 不足时增大 `shm_size`
- 模型下载失败时切换 `MINERU_MODEL_SOURCE`
