"""路由→编排适配层（api 表现层内部，拆分自 api/executor）。

收敛表现层与 orders 业务域的适配点（原 api/executor.py 的"路由异步包装"部分）：
- publish_order / parse_document_to_order / extract_order_text 三个路由入口，
  直连 orders 域 async 编排；
- 测试接缝：路由模块级 import 本模块符号并在自身命名空间
  调用，测试以 setattr(路由模块, 符号名, ...) 注入替身，不经 app.main 中转；
- 并发控制（_inflight_semaphore / _run_skill）已迁至 app/core/executor.py
  （core 不依赖业务域，skill 框架职责与业务适配分离）。
"""

from __future__ import annotations

from typing import Any

from app.orders.document.service import parse_document_to_order_async
from app.orders.text.client import publish_create_order_async

# 别名规避同名遮蔽：本地 wrapper 与底层实现同名（公开口径统一 extract_order_text）
from app.orders.text.extractor import extract_order_text as _extract_order_text_impl


async def publish_order(
    order_data: dict[str, Any],
    *,
    room_id: str,
    user_id: str,
) -> dict[str, Any]:
    """下单直连异步下游（不再占用线程；错误分类）。"""
    return await publish_create_order_async(order_data, room_id=room_id, user_id=user_id)


async def extract_order_text(text: str):
    """自由文本抽取为纯规则 CPU（毫秒级），直接执行（保持 async 签名供路由 await）。"""
    return _extract_order_text_impl(text)


async def parse_document_to_order(file_bytes: bytes, filename: str):
    """文档解析直连异步编排（转换段入线程池 + LLM 真异步，见 document.py）。"""
    return await parse_document_to_order_async(file_bytes, filename)
