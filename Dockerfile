# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.11.7 AS uv

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/

# LibreOffice：.doc 转换需要；不装则 .doc 会走 SCAN_OR_IMAGE_HINT 分支
# 如果确定不接 .doc 可去掉这段，镜像会小很多（~800MB）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core libreoffice-writer libreoffice-calc \
        fonts-noto-cjk \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先按锁文件安装生产依赖，以便复用 Docker 缓存。
COPY pyproject.toml uv.lock README.md /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY app /app/app
# 运行时配置与账单模板库：fee_price_map 是 fail fast（缺文件直接 500），
# box_whitelist/master_data 缺失会静默降级（白名单失效/建档禁用），templates
# 缺失则模板识别全走 LLM——三者都必须随镜像分发（2026-08-31 生产事故根因）
COPY config /app/config
COPY templates /app/templates
RUN uv sync --frozen --no-dev --no-editable

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
