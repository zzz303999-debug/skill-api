"""LLM 外部适配器层门面（分层定位见 docs/架构说明.md §0）。

业务域与表现层只经本包调用 LLM（achat / achat_json），禁止绕过；
同步版 chat/chat_json 保留供 to_thread 解析段内同步调用（ai_header 表头识别）。
"""

from app.llm.client import achat, achat_json, chat, chat_json, image_to_data_url

__all__ = [
    "achat",
    "achat_json",
    "chat",
    "chat_json",
    "image_to_data_url",
]
