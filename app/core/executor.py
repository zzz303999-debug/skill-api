"""LLM 长任务在途闸（core 横切设施，2026-09 结构整理自 api/executor 迁入）。

职责：对 **LLM 长调用链**（skill 抽取、附件文档解析，单次最长 180s）做进程级
并发上限与排队 503 背压——在途任务数超过 skill_max_concurrency 时排队，
等待超过 skill_queue_wait_seconds 返回 503 server_busy，防止并发打满 LLM 网关
与内存膨胀（与拆分前线程池时代语义一致）。

并发模型（2026-09 用户拍板）：CPU/短网络请求（preview、文本下单等）**不设
请求闸**；真实下单的下游并发由 orders/bill/client.py 的进程级共享信号量约束。

依赖方向：仅依赖 core 内部（config/errors），不 import 业务域与表现层。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.errors import ServiceBusyError
from app.core.skill_base import SkillBase

# 在途任务信号量：防止无界排队导致内存膨胀（与线程池时代同语义）
_inflight_semaphore = asyncio.Semaphore(settings.skill_max_concurrency)


@asynccontextmanager
async def _inflight_guard() -> AsyncIterator[None]:
    """进程级在途任务闸：排队超时抛 503 server_busy（async with 包裹业务段）。

    注意：acquire 的取消竞态依赖 3.12 标准库行为（超时竞争中自行归还信号量
    值后抛 TimeoutError）；Dockerfile/pyproject 已锁定 >=3.12，勿降级。
    """
    try:
        await asyncio.wait_for(
            _inflight_semaphore.acquire(),
            timeout=settings.skill_queue_wait_seconds,
        )
    except TimeoutError:
        raise ServiceBusyError("server is busy, too many concurrent tasks") from None
    except ValueError:
        # 防御性兜底：理论上不可达（3.12 标准库在超时竞争中自行归还信号量值后
        # 抛 TimeoutError），保留以防运行时版本差异导致裸 500
        raise ServiceBusyError("server is busy, too many concurrent tasks") from None
    try:
        yield
    finally:
        _inflight_semaphore.release()


async def _run_skill(skill: SkillBase, content: bytes, filename: str) -> dict:
    """skill 执行：进程级并发闸 + 排队 503 语义包裹异步契约调用。"""
    async with _inflight_guard():
        return await skill.run(file_bytes=content, filename=filename, options=None)
