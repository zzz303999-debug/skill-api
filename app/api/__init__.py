"""HTTP 层：应用组装、中间件、异常处理器、路由与测试接缝（bridges）。

分层约定（自外向内）：
- app/main.py：入口，create_app() 组装
- app/api/app_factory.py：create_app()（中间件注册顺序即层级契约）
- app/api/middleware/：限流 / 鉴权 / 访问日志
- app/api/error_handlers.py：全局异常处理器（SkillAPIError / RequestValidationError / 兜底）
- app/api/routes/：路由模块（APIRouter，按业务域拆分）
- app/api/bridges.py：路由→orders 域适配（下单/文档解析/文本抽取；app.main 测试接缝符号宿主）
- app/core/executor.py：Skill.run 异步契约的并发上限与排队 503 语义（见 app/core/ 分层）
- app/api/response_shell.py：统一响应外壳 {code, msg, data}
- app/api/uploads.py：上传文件读取与校验（各上传路由共用）
"""
