"""HTTP 中间件：限流（rate_limit）、鉴权（auth）、访问日志（access_log）；
_client_ip.py 为中间件共用工具（客户端 IP 解析）。

注册顺序即层级契约（后注册者为外层），见 app/api/app_factory.py：
rate_limit → auth → access_log，执行序 access_log → auth → rate_limit。
"""
