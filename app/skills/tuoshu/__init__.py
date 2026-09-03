"""tuoshu skill 注册入口。loader 扫到本子包时会 import 这里，触发 register()。"""

from app.core.skill_registry import register

from .skill import TuoshuSkill

register(TuoshuSkill())
