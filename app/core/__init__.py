"""core 横切设施：只被依赖，不 import 任何业务域（orders/skills）与表现层（api）。

- 全局设施：config / errors / logging_conf
- skill 插件框架：skill_base（契约）/ skill_registry（注册发现）/ executor（并发控制）
- 存储服务：access_log_store / third_party_log_store（被表现层与业务域两侧消费）
- 日志支撑：log_support（request_id 请求上下文——审计↔第三方日志串联；时间过滤工具）
"""
