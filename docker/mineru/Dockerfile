# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ARG MINERU_VERSION=2.5.4
ARG MINERU_MODEL_SOURCE=modelscope
ARG TORCH_VERSION=2.6.0
ARG TORCHVISION_VERSION=0.21.0
ARG TORCH_INDEX_URL=https://mirror.nju.edu.cn/pytorch/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    MINERU_MODEL_SOURCE=${MINERU_MODEL_SOURCE}

RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        poppler-utils \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
        "torch==${TORCH_VERSION}+cpu" \
        "torchvision==${TORCHVISION_VERSION}+cpu" \
        --index-url "${TORCH_INDEX_URL}" \
    && python -m pip install --no-cache-dir "mineru[core]==${MINERU_VERSION}"

RUN mineru-models-download -s "${MINERU_MODEL_SOURCE}" -m pipeline

EXPOSE 8888

CMD ["mineru-api", "--host", "0.0.0.0", "--port", "8888"]
