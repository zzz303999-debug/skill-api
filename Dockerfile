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
RUN uv sync --frozen --no-dev --no-editable

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
