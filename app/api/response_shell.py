"""统一响应外壳（{code, msg, data}）的错误路径注册与错误体构造。

成功路径外壳语义（200/204/409 判定）在各 orders 域 import_response.py（bill/
manifest）；本模块只服务 api 层：错误常发生在路由之前的中间
件/异常处理器（401/413/422/429/500），彼时只有 URL 路径可用，故按路径注册表
决定错误响应是否套统一外壳；未注册路径保持旧 {error:...} 结构不变。
"""

from __future__ import annotations

# 已适配统一外壳的路径（新接口适配：路径加入此集合即可）
_UNIFIED_RESPONSE_PATHS = frozenset(
    {"/orders/bill/import", "/orders/manifest/import", "/skills/tuoshu/extract"}
)


def _use_unified_response(path: str) -> bool:
    """该路径错误响应是否使用统一外壳；去尾斜杠匹配（中间件先于路由执行，
    尾斜杠请求 307 重定向前不归一，会回退旧结构致同接口两结构并存）。"""
    return path.rstrip("/") in _UNIFIED_RESPONSE_PATHS


def _unified_error_body(code: str, msg: str, details: dict | None = None) -> dict:
    """构造统一外壳错误体（code 机器可读、msg 可直接展示、details 并入 data）。"""
    return {"code": code, "msg": msg, "data": details or None}
