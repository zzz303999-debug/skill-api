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
LLM 层      →  app/llm/client.py（唯一出口）
```

### LLM 运行环境

本服务通过 OpenAI-compatible API 调用外部 LLM。服务地址、API Key、模型名和并发数
必须通过部署环境变量注入，不要把真实凭证或固定环境地址写进代码和 `.env.example`。

`SkillBase.run()` 保持同步契约，但 FastAPI handler **不得直接调用同步 run()**。
`app/main.py` 会统一把 Skill 放进有界线程池执行：

- 避免文件转换和最长数分钟的 LLM 请求阻塞 asyncio 事件循环
- `SKILL_MAX_CONCURRENCY` 限制单进程同时运行的 Skill 数量
- 新接口应复用 `_run_skill()`，不要自行创建无限制线程池或 `asyncio.gather`
- `.venv` 是运行环境产物，不跨机器复制，不纳入项目交付

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

每个 Skill 同时会挂载：

- `POST /skills/{name}/extract`：单文件抽取，返回强类型统一响应
- `POST /skills/{name}/batch-extract`：多文件抽取，逐文件返回结果或结构化错误

上传限制由 `API_MAX_UPLOAD_BYTES`、`API_BATCH_MAX_FILES` 控制。批量接口的并发仍受
全局 `SKILL_MAX_CONCURRENCY` 约束，禁止绕过限制直接并发调用 LLM。

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

注意：`run()` 的参数是 keyword-only。任何框架层调用都必须写成：

```python
skill.run(file_bytes=content, filename=filename, options=None)
```

禁止以位置参数调用，否则所有 Skill 都会触发 `TypeError`。

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
- `model_validate()` 的 `ValidationError` 必须转换为 `ParseError`，不要让原始异常变成 500
- LLM JSON 顶层必须是 object；数组、字符串、null 都视为 `ParseError`
- 批量响应禁止直接返回 `str(exception)`，避免泄露网关地址、凭证或内部实现

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

模型默认名以 `app/config.py` 和部署环境的 `LLM_MODEL_DEFAULT` 为准。
文档中不要假定部署环境一定使用某个特定 LLM 供应商。
需要指定其他模型时向 `chat()` / `chat_json()` 传 `model=`，不要直接读取不存在的配置项。

### 图片与扫描 PDF

- 普通图片直接转 data URL 走 vision
- 带文本层的 PDF 先转 Markdown
- 文本层过短的扫描 PDF 会完整渲染为 PNG；超过 `VISION_MAX_PDF_PAGES` 时直接报错，禁止截断
- `VISION_PDF_RENDER_SCALE` 控制渲染清晰度和内存占用
- `.doc` 无法通过 LibreOffice 转换时返回 `ConvertError`，不要只把 `SCAN_OR_IMAGE_HINT` 文本交给 LLM

多页视觉输入必须受页数和上传大小限制，禁止把无限页 PDF 全量展开进模型上下文。

## 错误处理

统一错误契约：`app/errors.py` 的 `SkillAPIError` 基类 + `main.py` 全局异常处理器，
对外固定输出 `{"error": {"code", "message", "description", "details"}}`。
**完整「公开错误码速查表」（HTTP 状态 / code / 中文说明）见 `app/errors.py` 模块 docstring**，
新增错误码时必须同步更新。

### 新增错误码 checklist（强制）

1. **继承**：定义 `class XxxError(SkillAPIError)`，设置 `http_status` 与 `code`；业务错误可定义在业务模块（如 `orders/client.py` 的 `OrderUpstreamError`）
2. **登记中文说明**：在 `ERROR_CODE_DESCRIPTIONS` 注册，否则 description 会 fallback 为英文 message，调用方无法判断含义
3. **同步映射表**：更新 `app/errors.py` docstring 速查表 + 本节，避免 HTTP 状态码与 code 语义漂移

### 私有异常模式（解析/转换模块）

解析/转换模块（如 `document_parsers/mineru.py` 的 `MinerUError`）内部可用私有异常
（继承普通 Exception），但必须在模块边界被上层捕获并转换为公开 `SkillAPIError`
（如 `parse_error` / `convert_error`）。**禁止私有异常直接穿透到 API 层**，
其他解析/转换模块必须遵循同样模式。

### 内置错误类型

- `BadRequestError` (400) — 客户端输入问题
- `SkillNotFoundError` (404)
- `ConvertError` (422) — 格式转换失败
- `LLMError` (502) — 网关/模型问题
- `ParseError` (502) — 模型输出无法解析
- `ServiceBusyError` (503) — 并发满/排队超时
- 中间件直出（不经 SkillAPIError）：`unauthorized` (401)、`payload_too_large` (413)、`rate_limited` (429)

批量接口对单文件错误使用同样的 `code/message/details` 结构，但整体请求可以继续处理
其他文件。意外异常只记录服务端日志，对外统一返回 `internal_error`，不得暴露原始堆栈。

全局 handler 已在 `main.py` 注册，抛出即可。

## 不做什么

- **不在 skill 里绕过 `app.llm` 直连模型**
- **不共享 skill 之间的 references**：每个 skill 的知识库放在自己目录，避免耦合
- **不做 per-template 的 if/else 硬编码**：模板变化交给 LLM + few-shot
- **不合并多柜/多行**：一票多条时展开为数组
- **不打包独立 OCR 引擎**：图片和扫描 PDF 直接走 vision 模型；如需专门 OCR skill，另建子包
- **不在同步接口里扩展超长任务**：现有 LLM 请求由有界线程池承载；需要多阶段、长批次处理时另建异步任务方案

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
