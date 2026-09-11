"""skill-api 应用包：FastAPI 单体（Modular Monolith）。

分层与依赖方向纪律见 docs/架构说明.md §0——main/api/core/域/适配器五层，
core 只被依赖不依赖域，业务域自包含，跨域仅经门面。
"""
