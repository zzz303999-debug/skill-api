"""执行器：skill 异步契约的并发控制与路由异步包装。

Phase 4 契约统一后：SkillBase.run 为 async 契约（转换段内部 to_thread），
线程池退役；在途任务信号量保留——skill_max_concurrency 继续控制并发、
排队超时返回 503 server_busy（语义与线程池时代一致）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from app.core.skill_base import SkillBase
from app.errors import ServiceBusyError
from app.orders import extract_order_text
from app.orders.client import publish_create_order_async
from app.orders.document import parse_document_to_order_async

# 在途任务信号量：防止无界排队导致内存膨胀（与线程池时代同语义）
_inflight_semaphore = asyncio.Semaphore(settings.skill_max_concurrency)


async def _run_skill(skill: SkillBase, content: bytes, filename: str) -> dict:
    """skill 执行：并发上限 + 排队 503 语义包裹异步契约调用。"""
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
        return await skill.run(file_bytes=content, filename=filename, options=None)
    finally:
        _inflight_semaphore.release()


async def _publish_order(
    order_data: dict[str, Any],
    *,
    room_id: str,
    user_id: str,
) -> dict[str, Any]:
    """下单直连异步下游（Phase 3 起不再占用线程；错误分类）。"""
    return await publish_create_order_async(order_data, room_id=room_id, user_id=user_id)


async def _extract_order_text(text: str):
    """自由文本抽取为纯规则 CPU（毫秒级），直接执行（保持 async 签名供路由 await）。"""
    return extract_order_text(text)


async def _parse_document_to_order(file_bytes: bytes, filename: str):
    """文档解析直连异步编排（转换段入线程池 + LLM 真异步，见 document.py）。"""
    return await parse_document_to_order_async(file_bytes, filename)
