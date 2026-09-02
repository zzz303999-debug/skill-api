"""统一响应外壳（{code, msg, data}）的路径注册与错误体构造。"""

from __future__ import annotations

# ---- 统一响应外壳（{code, msg, data}）----
# 已适配路径全场景（成功/业务失败/请求错误）统一取 code/msg/data 三字段
# （code 机器可读、msg 可直接展示的中文说明、data 为补充详情）：
# - /orders/bill/import：账单录入（v2.2 起先行适配）
# - /orders/manifest/import：舱单录入（2026-09-01 适配，成功 msg 为
#   "请求成功"/"添加成功"/"204 具体原因"，错误场景 msg 为中文说明）
# - /skills/tuoshu/extract：托书单文件抽取（2026-08-31 适配，data 内为
#   {skill, version, result, meta, content?}，result 即原 data 抽取结果）
# 其余接口保持 {error: {code, message, description, details}} 结构不变。
# 新接口适配统一外壳：路径加入下方集合即可（错误响应由全局异常处理器套壳）。
_UNIFIED_RESPONSE_PATHS = frozenset(
    {"/orders/bill/import", "/orders/manifest/import", "/skills/tuoshu/extract"}
)


def _use_unified_response(path: str) -> bool:
    """该路径的错误响应是否使用统一外壳 {code, msg, data}。

    去尾斜杠后匹配（审查修正 2026-08-27）：中间件/422 处理器先于路由执行，
    尾斜杠请求（307 重定向前）若不归一，会回退旧 {error:...} 结构，
    同接口两种错误结构并存。
    """
    return path.rstrip("/") in _UNIFIED_RESPONSE_PATHS


def _unified_error_body(code: str, msg: str, details: dict | None = None) -> dict:
    """构造统一外壳错误体：code 机器可读、msg 可直接展示、details 并入 data。"""
    return {"code": code, "msg": msg, "data": details or None}
