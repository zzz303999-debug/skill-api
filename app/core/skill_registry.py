"""Skill 注册中心 + 自动发现。"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable

from app.core.errors import SkillNotFoundError
from app.core.logging_conf import get_logger
from app.core.skill_base import SkillBase

log = get_logger(__name__)

_registry: dict[str, SkillBase] = {}


def register(skill: SkillBase) -> None:
    if skill.name in _registry:
        raise RuntimeError(f"duplicate skill name: {skill.name}")
    _registry[skill.name] = skill
    log.info("skill_registered", extra={"skill": skill.name, "version": skill.version})


def get(name: str) -> SkillBase:
    if name not in _registry:
        raise SkillNotFoundError(f"skill not found: {name}")
    return _registry[name]


def all_skills() -> Iterable[SkillBase]:
    return list(_registry.values())


def discover(package: str = "app.skills") -> None:
    """扫描 app.skills 下所有子包，导入触发注册。

    每个 skill 子包在自己的 `__init__.py` 里做 `register(XxxSkill())` 即可。
    单个 skill 导入失败只跳过该 skill（记录错误日志），不让整个应用启动失败。
    """
    pkg = importlib.import_module(package)
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.ispkg:
            full = f"{package}.{m.name}"
            try:
                importlib.import_module(full)
            except Exception as e:
                log.error(
                    "skill_import_failed",
                    extra={"skill": m.name, "error_type": type(e).__name__, "error": str(e)},
                    exc_info=True,
                )
