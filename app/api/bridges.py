"""路由→编排适配层（api 表现层内部，2026-09 结构整理自 api/executor 拆分）。

收敛表现层与 orders 业务域的适配点（原 api/executor.py 的"路由异步包装"部分）：
- _publish_order / _parse_document_to_order / _extract_order_text 三个路由入口，
  直连 orders 域 async 编排；
- 本模块符号是 app.main 测试接缝的宿主（main.py re-export，路由在调用时经
  app.main 命名空间解析，测试以 setattr(main_module, ...) 注入替身）；
- 并发控制（_inflight_semaphore / _run_skill）已随结构整理迁至 app/core/executor.py
  （core 不依赖业务域，skill 框架职责与业务适配分离）。
"""

from __future__ import annotations

from typing import Any

from app.orders.document.service import parse_document_to_order_async
from app.orders.text.client import publish_create_order_async
from app.orders.text.extractor import extract_order_text


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
