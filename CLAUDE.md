# skill-api — 项目约束（给 AI 协作者读）

本文件是给 Claude / OpenCode 等 AI 协作者的开发指南。人类同事请先看 `README.md`。

## 项目定位

把 `jxt/skills/` 下的 Claude Skills 封装成**独立部署的 HTTP API 服务**。核心诉求：

1. **一个服务承载多个 skill**：一次部署，多种抽取能力
2. **每个 skill 独立强类型契约**：调用方拿到精确 OpenAPI schema，不同 skill 输出结构不同
3. **薄脚本 + 厚 LLM**：延续 Claude Skill 的架构，脚本只做无损格式转换，语义抽取全部走 LLM
4. **同步优先**：先做同步接口跑通，后续可加异步任务队列（Redis+RQ / SQLite BackgroundTasks）

## 架构原则

### 分层

```
HTTP 层     →  app/main.py（路由自动注册）
调度层      →  app/core/{skill_base, registry}
Skill 层    →  app/skills/<name>/
LLM 层      →  app/llm/openclaw.py（唯一出口）
```

**LLM 调用必须走 `app.llm` 模块**。禁止在 skill 里直接 `import openai` 或写 http 请求。这样：
- 模型/网关/超时/重试策略集中调整
- 日志和用量统计统一
- 未来替换 provider 只改一个文件

### 每个 skill = 一个子包

```
app/skills/<skill_name>/
├── __init__.py         # 必须：调用 register(XxxSkill()) 触发注册
├── skill.py            # 必须：class XxxSkill(SkillBase) 实现 run()
├── schema.py           # 必须：class XxxOutput(BaseModel)，OpenAPI 强类型
├── prompt.py           # 建议：system prompt / few-shot / 用户消息组装
├── converter.py or *   # 可选：格式转换（bytes → markdown / image）
└── references/         # 可选：业务知识 md + few-shot examples
```

**启动流程**：`registry.discover()` 扫描 `app.skills.*` 包，import 触发每个子包的 `__init__.py` 里的 `register()`。然后 `main.py` 为每个已注册 skill 挂 `POST /skills/{name}/extract`。

## 新增 skill 的标准流程

以"运价表抽取"为例：

### 1. 建目录

```
app/skills/freight_rate/
├── __init__.py
├── skill.py
├── schema.py
├── prompt.py
└── references/
    └── (对应业务知识文件)
```

### 2. 写 schema.py

```python
from pydantic import BaseModel

class FreightRateOutput(BaseModel):
    origin_port: str | None = None
    dest_port: str | None = None
    rates: list[dict]
    # ...
```

### 3. 写 skill.py

```python
from app.core.skill_base import SkillBase
from app.llm import chat_json
from .schema import FreightRateOutput
from .prompt import build_system_prompt, build_user_message

class FreightRateSkill(SkillBase):
    name = "freight-rate"
    version = "0.1.0"
    description = "海运运价表结构化抽取"
    accepts = [".xlsx", ".xls", ".pdf"]
    output_model = FreightRateOutput

    def run(self, *, file_bytes, filename, options=None):
        # 1. 转 markdown
        markdown = ...
        # 2. 组 prompt
        messages = [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": build_user_message(markdown, filename)},
        ]
        # 3. 调 LLM
        data, meta = chat_json(messages, temperature=0.0)
        # 4. Pydantic 校验
        validated = FreightRateOutput.model_validate(data)
        return {"result": validated.model_dump(), "meta": meta}
```

### 4. 写 `__init__.py`

```python
from app.core.registry import register
from .skill import FreightRateSkill

register(FreightRateSkill())
```

### 5. 重启服务

- `POST /skills/freight-rate/extract` 自动出现
- `/docs` 里能看到精确的 `FreightRateOutput` schema
- `/skills` 列表包含新 skill

**不需要改**：`main.py`、`registry.py`、任何全局路由配置。

## SkillBase 契约

```python
class SkillBase:
    name: str                     # 必填，用作 URL 路径
    version: str = "0.1.0"
    description: str = ""
    accepts: list[str] = []       # 支持的扩展名，如 [".xlsx", ".pdf"]
    output_model: type[BaseModel] # 必填，用于 OpenAPI

    def run(self, *, file_bytes: bytes, filename: str,
            options: dict | None = None) -> dict:
        """返回 {"result": <符合 output_model 的 dict>, "meta": <可选>}。"""
```

**约定**：
- `run` 抛 `app.errors.*` 里的异常，会被 FastAPI 全局处理器转成对应状态码
- 不要在 `run` 里做 I/O 写文件（要写走 `settings.storage_dir`）
- 结果必须过 `output_model.model_validate()` 校验

## LLM 使用

```python
from app.llm import chat, chat_json, image_to_data_url

# 一般文本抽取（要 JSON 输出）
data, meta = chat_json(messages, temperature=0.0)

# 图片 vision
data_url = image_to_data_url(image_bytes, mime="image/jpeg")
messages = [
    {"role": "system", "content": "..."},
    {"role": "user", "content": [
        {"type": "text", "text": "..."},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]},
]
```

模型默认从 `LLM_MODEL_DEFAULT`（`deepseek-v4-flash`，1M context + vision）。需要更强推理时传 `model=settings.llm_model_reasoning`。

## 错误处理

用 `app/errors.py` 里的类型：

- `BadRequestError` (400) — 客户端输入问题
- `UnauthorizedError` (401) — 鉴权失败
- `SkillNotFoundError` (404)
- `ConvertError` (422) — 格式转换失败
- `LLMError` (502) — 网关/模型问题
- `ParseError` (502) — 模型输出无法解析

全局 handler 已在 `main.py` 注册，抛出即可。

## 不做什么

- **不在 skill 里绕过 `app.llm` 直连模型**
- **不共享 skill 之间的 references**：每个 skill 的知识库放在自己目录，避免耦合
- **不做 per-template 的 if/else 硬编码**：模板变化交给 LLM + few-shot
- **不合并多柜/多行**：一票多条时展开为数组
- **不打包 OCR 依赖**：图片直接走 vision 模型；如需专门 OCR skill，另建子包
- **不在同步接口里做 >60s 的重任务**：太长的活儿等异步方案落地

## 后续路线

- [ ] 异步任务队列（Redis + RQ 或 SQLite + BackgroundTasks）
- [ ] Prometheus metrics（`/metrics`）
- [ ] 每个 skill 的独立测试样本 + 回归 CI
- [ ] freight-rate skill 迁入
- [ ] 结果持久化到对象存储（可选）

## 与源 skill 的关系

- 源目录：`/Users/zhangjiajun/jxt/skills/tuoshu-extractor/`
- 复用：`scripts/to_text.py` → `app/skills/tuoshu/converter.py`（未改逻辑）
- 复用：`references/*` → `app/skills/tuoshu/references/*`
- **references 的更新流程仍然按源目录 CLAUDE.md 的规范**（新样例、新船公司、新港口）。更新后同步到 API repo 对应目录即可。
