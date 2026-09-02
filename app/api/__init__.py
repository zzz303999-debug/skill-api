"""HTTP 层：应用组装、中间件、异常处理器、路由与执行器。

分层约定（自外向内）：
- app/main.py：入口，create_app() 组装并保留拆分前的 app.main 命名空间（测试接缝）
- app/api/app_factory.py：create_app()（中间件注册顺序即层级契约）
- app/api/middleware/：限流 / 鉴权 / 访问日志
- app/api/routes/：路由模块（APIRouter，按业务域拆分）
- app/api/executor.py：Skill.run 同步契约的有界线程池执行
- app/api/response_shell.py：统一响应外壳 {code, msg, data}
- app/api/uploads.py：上传文件读取与校验（各上传路由共用）
"""
