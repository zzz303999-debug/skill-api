"""托书确定性映射子包（P4-3 自 finalize/deterministic_mapper 包化，行为零变更）。

- finalize.py              结果确定性订单映射与人工复核校验编排（原 finalize.py）
- deterministic_mapper.py  模板指纹命中时的字段确定性取值（原文件）
消费方式：skill.py 编排直接 `from .mapping.finalize import ...` 等子模块路径。
"""
