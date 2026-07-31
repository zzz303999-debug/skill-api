# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ARG MINERU_VERSION=2.5.4
ARG MINERU_MODEL_SOURCE=modelscope

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MINERU_MODEL_SOURCE=${MINERU_MODEL_SOURCE}

# MinerU pipeline needs OpenCV/PDF runtime libraries and CJK fonts. This is a
# CPU-compatible base; production GPU hosts can replace this base/runtime with
# a CUDA image while keeping the same mineru-api command.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        poppler-utils \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "mineru[core]==${MINERU_VERSION}"

# Bake the pipeline models into the image so the first production request does
# not spend its API timeout downloading model weights.
RUN mineru-models-download -s "${MINERU_MODEL_SOURCE}" -m pipeline

EXPOSE 8888

CMD ["mineru-api", "--host", "0.0.0.0", "--port", "8888"]
