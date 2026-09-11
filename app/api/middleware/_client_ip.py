"""客户端 IP 解析（限流 / 访问日志中间件共用，独立成模块避免中间件间横向引用）。"""

from __future__ import annotations

from fastapi import Request

from app.core.config import settings


def _resolve_client_ip(request: Request) -> tuple[str | None, str | None]:
    """解析客户端 IP，返回 (客户端IP, 原始X-Forwarded-For头)。

    云服务前面通常有 nginx/负载均衡，access_log_trust_proxy 开启时：
    - X-Forwarded-For 存在时取**最后一个**地址（nginx `$proxy_add_x_forwarded_for`
      是追加语义，最后一个即离本服务最近的代理看到的真实客户端 IP）；
      客户端自行伪造的前缀地址被忽略，限流与审计 IP 不可被污染；
    - 其次 X-Real-IP；均不存在或未开启信任时回退到直连地址。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if settings.access_log_trust_proxy:
        if forwarded:
            parts = [part.strip() for part in forwarded.split(",") if part.strip()]
            if parts:
                return parts[-1], forwarded
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip(), forwarded
    host = request.client.host if request.client else None
    return host, forwarded
