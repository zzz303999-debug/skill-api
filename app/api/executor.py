"""同步业务的有界线程池执行：Skill.run 同步契约的异步桥接。

Skill.run 是同步契约，统一放到有界线程池，避免文件转换和 LLM 请求阻塞事件循环，
同时限制对 LLM 服务的并发压力；在途任务满时新请求排队，超时返回 503 server_busy。
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
from app.orders import (
    extract_order_text,
    parse_document_to_order,
    publish_create_order,
)

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
    return await _run_in_executor(
        partial(publish_create_order, order_data, room_id=room_id, user_id=user_id)
    )


async def _extract_order_text(text: str):
    return await _run_in_executor(partial(extract_order_text, text))


async def _parse_document_to_order(file_bytes: bytes, filename: str):
    return await _run_in_executor(
        partial(
            parse_document_to_order,
            file_bytes,
            filename,
        )
    )
