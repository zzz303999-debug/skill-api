# syntax=docker/dockerfile:1.7

# uv 二进制（南大镜像加速 ghcr.io；如需官方源改回 ghcr.io/astral-sh/uv:0.11.7）
FROM ghcr.nju.edu.cn/astral-sh/uv:0.11.7 AS uv

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple \
    UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/

# 阿里云镜像源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true


RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core libreoffice-writer \
        fonts-wqy-microhei \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock README.md /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY app /app/app
COPY config /app/config
COPY templates /app/templates
RUN uv sync --frozen --no-dev --no-editable

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
