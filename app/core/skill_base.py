"""Skill 抽象基类。

新增 skill 只需：
1. 在 `app/skills/<name>/` 下建目录
2. 定义 `class XxxSkill(SkillBase)` 并实现 `run()`
3. 在 `app/skills/__init__.py` 中 import 一次，或依赖 loader 自动发现
4. framework 会自动挂载到 `POST /skills/{name}/extract`
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel


class SkillMeta(BaseModel):
    name: str
    version: str
    description: str
    accepts: list[str]


class SkillResponse(BaseModel):
    """所有 skill 的统一外层响应壳。`data` 是各 skill 自己的强类型 schema。"""

    skill: str
    version: str
    data: Any
    meta: dict[str, Any] = {}


class SkillBase(ABC):
    """所有 skill 继承此类。"""

    # 子类必填
    name: ClassVar[str]
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = ""
    # 支持的文件扩展名（小写、带点），比如 [".xlsx", ".pdf"]
    accepts: ClassVar[list[str]] = []
    # 强类型输出模型，供 OpenAPI 生成精确 schema
    output_model: ClassVar[type[BaseModel] | None] = None

    @classmethod
    def meta(cls) -> SkillMeta:
        return SkillMeta(
            name=cls.name,
            version=cls.version,
            description=cls.description,
            accepts=cls.accepts,
        )

    @abstractmethod
    def run(self, *, file_bytes: bytes, filename: str, options: dict | None = None) -> dict:
        """核心方法：给定文件字节 → 返回符合 `output_model` 的 dict。"""
        raise NotImplementedError
