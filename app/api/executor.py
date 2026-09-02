"""执行器：Skill.run 同步契约的有界线程池执行 + 路由异步包装（Phase 3 起）。

Phase 3 路由异步化后职责收缩：
- _run_skill / _run_in_executor：skill 同步契约继续走有界线程池（并发上限与
  排队 503 语义不变）；
- _publish_order / _parse_document_to_order：改为直连异步下游（不再占用线程），
  经 app.main 接缝供路由调用（测试替身签名兼容，fake 本为 async）；
- _extract_order_text：纯规则 CPU（毫秒级），直接同步执行。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from app.config import settings
from app.core.skill_base import SkillBase
from app.errors import ServiceBusyError
from app.orders import extract_order_text
from app.orders.client import publish_create_order_async
from app.orders.document import parse_document_to_order_async

_skill_executor = ThreadPoolExecutor(
    max_workers=settings.skill_max_concurrency,
    thread_name_prefix="skill-runner",
)
# 在途任务信号量：与线程池 worker 数一致，防止无界排队导致内存膨胀
_inflight_semaphore = asyncio.Semaphore(settings.skill_max_concurrency)


async def _run_in_executor(call: Callable[[], Any]) -> Any:
    """在有界线程池中执行同步调用，避免阻塞事件循环。

    在途任务数受 skill_max_concurrency 限制：超限的新请求最多排队
    skill_queue_wait_seconds 秒，仍无空位则返回 503 server_busy，
    防止 LLM/转换任务（持有大文件字节）无界堆积耗尽内存。
    """
    try:
        await asyncio.wait_for(
            _inflight_semaphore.acquire(),
            timeout=settings.skill_queue_wait_seconds,
        )
    except TimeoutError:
        raise ServiceBusyError("server is busy, too many concurrent tasks") from None
    except ValueError:
        # wait_for 超时取消与 release() 的竞争：等待者 future 已被弹出，
        # acquire 未成功，按繁忙处理（避免裸 500）
        raise ServiceBusyError("server is busy, too many concurrent tasks") from None
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_skill_executor, call)
    finally:
        _inflight_semaphore.release()


async def _run_skill(skill: SkillBase, content: bytes, filename: str) -> dict:
    return await _run_in_executor(
        partial(
            skill.run,
            file_bytes=content,
            filename=filename,
            options=None,
        )
    )


async def _publish_order(
    order_data: dict[str, Any],
    *,
    room_id: str,
    user_id: str,
) -> dict[str, Any]:
    """下单直连异步下游（Phase 3 起不再占用线程；错误分类与同步版一致）。"""
    return await publish_create_order_async(order_data, room_id=room_id, user_id=user_id)


async def _extract_order_text(text: str):
    """自由文本抽取为纯规则 CPU（毫秒级），直接执行（保持 async 签名供路由 await）。"""
    return extract_order_text(text)


async def _parse_document_to_order(file_bytes: bytes, filename: str):
    """文档解析直连异步编排（转换段入线程池 + LLM 真异步，见 document.py）。"""
    return await parse_document_to_order_async(file_bytes, filename)
