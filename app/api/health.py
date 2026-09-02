"""健康检查：依赖配置检查 + 可达性探测（不调 chat、不触发下单）。"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter

from app.config import settings
from app.core import registry

router = APIRouter()

# /healthz 的 dependencies：只验可达性，不调 chat/completions（避免计费与推理消耗）；
# 订单上游接口为 POST 下单端点（请求即下单），绝不实际探测，仅做配置级检查。

_LLM_PROBE_PATH = "/models"


def _probe_order_config() -> str:
    """订单上游仅做配置级检查：接口地址已配置即视为可下单。"""
    return "ok" if settings.order_api_url else "not_configured"


async def _http_probe(base_url: str, path: str) -> str:
    """通用可达性探测：任何 HTTP 响应（含 401/4xx）都视为网关可达，
    仅连接失败/超时视为 unreachable。
    """
    try:
        async with httpx.AsyncClient(timeout=settings.health_probe_timeout_seconds) as client:
            await client.get(f"{base_url.rstrip('/')}{path}")
        return "ok"
    except (httpx.HTTPError, OSError):
        return "unreachable"


async def _probe_llm() -> str:
    """探测 LLM 网关：GET {base_url}/models；未配置（缺 key）不发起请求。"""
    if not settings.llm_api_key or not settings.llm_base_url:
        return "not_configured"
    return await _http_probe(settings.llm_base_url, _LLM_PROBE_PATH)


async def _probe_mineru() -> str:
    """探测 MinerU：GET {base_url}/；未启用（可降级依赖）返回 disabled。"""
    if not settings.mineru_enabled or not settings.mineru_base_url:
        return "disabled"
    return await _http_probe(settings.mineru_base_url, "/")


async def _probe_dependencies() -> dict[str, str]:
    """并行探测三个依赖；探测关闭时全部标记 skipped（测试隔离/自定义探活）。"""
    if not settings.health_probe_enabled:
        return {"llm": "skipped", "mineru": "skipped", "order_api": "skipped"}
    llm_status, mineru_status = await asyncio.gather(_probe_llm(), _probe_mineru())
    return {
        "llm": llm_status,
        "mineru": mineru_status,
        "order_api": _probe_order_config(),
    }


@router.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    """存活检查 + 依赖状态。

    status 恒为 "ok"（进程存活，Docker healthcheck 与鉴权豁免语义不变）；
    dependencies 反映 LLM/MinerU/订单上游的配置与可达状态，供监控与人工
    排障；探测失败不影响 HTTP 200，避免网络抖动误判容器不健康。
    """
    return {
        "status": "ok",
        "skills": [s.name for s in registry.all_skills()],
        "dependencies": await _probe_dependencies(),
    }
