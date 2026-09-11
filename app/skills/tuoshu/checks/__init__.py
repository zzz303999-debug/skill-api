"""托书校验子包（P4-2 自 post_checks/post_common 包化，行为零变更）。

- common.py     校验公共工具层：issue 管理、显式字段提取、标签/结构识别、
                承运人前缀表与候选识别（原 post_common.py）
- validators.py 校验修复实现层：单据、容器、发货人、grounding、MBL 冲突、
                日期清洗等（原 post_checks.py）；消费 common 工具
消费方式：finalize 等编排方直接 `from .checks.validators import ...` /
`from .checks.common import ...`（符号为模块私有约定，包门面不做二次转发）。
"""
