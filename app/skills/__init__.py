"""skills 业务域：skill 插件集合。

每个 skill 在各自子包的 __init__.py 中调用 skill_registry.register() 自注册，
由 core/skill_registry 的 discover() 扫描 app.skills 下子包自动挂载路由。
"""
