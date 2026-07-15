"""托书抽取 prompt 组装。

在启动时把 references/*.md 全部加载到内存，避免每次读盘。
few-shot 样例按需选一部分附上。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

REF_DIR = Path(__file__).parent / "references"
EX_DIR = REF_DIR / "examples"


@lru_cache(maxsize=1)
def load_references() -> dict[str, str]:
    """一次性把 references/*.md 载入。"""
    out: dict[str, str] = {}
    for p in sorted(REF_DIR.glob("*.md")):
        out[p.stem] = p.read_text(encoding="utf-8")
    return out


@lru_cache(maxsize=1)
def load_examples(limit: int = 3) -> list[tuple[str, str]]:
    """加载 few-shot：返回 [(markdown_input, expected_json), ...]。

    limit 控制随 prompt 附带的样例数，避免上下文爆炸。挑选覆盖不同类型的：
    - zuoxiang_std_esff：单柜表格
    - wxhyc_transport_para：段落式一票多柜
    - yuhai_table：吨→KG 换算
    """
    picks = ["zuoxiang_std_esff", "wxhyc_transport_para", "yuhai_table"]
    out: list[tuple[str, str]] = []
    for name in picks[:limit]:
        md = EX_DIR / f"{name}.md"
        js = EX_DIR / f"{name}.json"
        if md.exists() and js.exists():
            out.append((md.read_text(encoding="utf-8"), js.read_text(encoding="utf-8")))
    return out


SYSTEM_PROMPT_TEMPLATE = """你是海运托书结构化抽取助手。

# 任务
从用户提供的托书文档（已转成 markdown 文本，或直接是图片）中，抽取结构化 JSON。

# 硬性规则
1. **精确复制**：`booking_no`/`customer_ref`/`mbl_no`/`hbl_no`/`po_no`/`container_no`/`seal_no` 必须从原文逐字符复制，不改字符大小写、不改 O/0、I/1、B/8。
2. **数字不计算**：`packages`/`gross_weight_kg`/`volume_cbm` 只做格式清洗（去空格、去单位、去千分位逗号），不求和。原文吨/T 时换算为 KG，并在对应 remark 说明。
3. **港口/箱型/船公司归一**：严格按 references 中的映射表，不自由发挥。
4. **多柜展开**：`3*40HC` → 一条 qty=3；`1*40HQ + 2*40GP` → 两条；每柜有独立明细/单号时每柜一条。
5. **空值一律 null**：不用空字符串、不用 0（除非原文明确 0）、不用占位符（`/`、`无`、`见附件`）。
6. **日期格式**：date 用 `YYYY-MM-DD`，datetime 用 `YYYY-MM-DDTHH:MM:SS`。无法确定年份则 null。
7. **中转港的"见设"规则**：原文出现 `见设` / `见设备单` / `见设备交接单` → transit_port=null。
8. **不臆造**：拿不准就 null。

# 输出格式
只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字。
必须包含 `source` 对象，字段由调用方指定（file / doc_format / extracted_at）。
`raw_text_snippet` 填原文前 200 字符便于溯源。

# 业务知识（references）

## schema
{schema_md}

## 字段别名
{field_aliases_md}

## 箱型归一
{container_types_md}

## 船公司前缀
{carriers_md}

## 港口清洗
{ports_md}

## 分类
{classification_md}
"""


def build_system_prompt() -> str:
    refs = load_references()
    return SYSTEM_PROMPT_TEMPLATE.format(
        schema_md=refs.get("schema", ""),
        field_aliases_md=refs.get("field-aliases", ""),
        container_types_md=refs.get("container-types", ""),
        carriers_md=refs.get("carriers", ""),
        ports_md=refs.get("ports", ""),
        classification_md=refs.get("classification", ""),
    )


def build_few_shot_messages() -> list[dict]:
    """few-shot 转成 messages（user/assistant 对话形式）。"""
    msgs: list[dict] = []
    for md, js in load_examples():
        msgs.append({"role": "user", "content": f"输入托书文本：\n\n{md}"})
        msgs.append({"role": "assistant", "content": js})
    return msgs


def build_user_message_text(markdown: str, filename: str, doc_format: str, extracted_at: str) -> str:
    return (
        f"待抽取托书。\n"
        f"file={filename}\n"
        f"doc_format={doc_format}\n"
        f"extracted_at={extracted_at}\n\n"
        f"===== 文档内容开始 =====\n"
        f"{markdown}\n"
        f"===== 文档内容结束 =====\n\n"
        f"请输出 JSON，source 字段用上述元数据填充。"
    )


def build_user_message_vision(
    image_data_url: str, filename: str, doc_format: str, extracted_at: str
) -> list[dict]:
    return [
        {
            "type": "text",
            "text": (
                f"待抽取托书（图片/扫描件）。\n"
                f"file={filename}\n"
                f"doc_format={doc_format}\n"
                f"extracted_at={extracted_at}\n\n"
                f"请直接从图片内容中提取，输出 JSON。source 字段用上述元数据。"
            ),
        },
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]
