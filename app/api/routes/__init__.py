"""路由模块：按业务域拆分的 APIRouter 与 skill 动态路由注册。

挂载顺序见 app/api/app_factory.py（保持既有定义顺序，
OpenAPI paths 顺序不变）：health → meta → orders → bill_import →
manifest_import → skills（动态注册）。
"""
