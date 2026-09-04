"""LLM 外部适配器层门面（分层定位见 docs/架构说明.md §0）。

业务域与表现层只经本包调用 LLM（achat / achat_json），禁止绕过；
2026-09 第二波同步链清理后仅保留 async 入口（同步版已退役）。
"""

from app.llm.client import achat, achat_json, image_to_data_url

__all__ = [
    "achat",
    "achat_json",
    "image_to_data_url",
]
