# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# LibreOffice：.doc 转换需要；不装则 .doc 会走 SCAN_OR_IMAGE_HINT 分支
# 如果确定不接 .doc 可去掉这段，镜像会小很多（~800MB）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core libreoffice-writer libreoffice-calc \
        fonts-noto-cjk \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖利用缓存
COPY pyproject.toml README.md /app/
RUN pip install --upgrade pip && \
    pip install "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0" \
                "pydantic>=2.9.0" "pydantic-settings>=2.6.0" \
                "python-multipart>=0.0.12" "httpx>=0.27.0" \
                "openai>=1.54.0" \
                "openpyxl>=3.1.5" "xlrd>=2.0.1" \
                "python-docx>=1.1.2" "pdfplumber>=0.11.4" \
                "python-json-logger>=2.0.7"

COPY app /app/app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
