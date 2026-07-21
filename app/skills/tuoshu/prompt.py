"""托书抽取 prompt 组装。

在启动时把 references/*.md 全部加载到内存，避免每次读盘。
few-shot 样例按需选一部分附上。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .schema import TuoshuOutput

REF_DIR = Path(__file__).parent / "references"
EX_DIR = REF_DIR / "examples"


@lru_cache(maxsize=1)
def load_references() -> dict[str, str]:
    """一次性把 references/*.md 载入。"""
    out: dict[str, str] = {}
    for p in sorted(REF_DIR.glob("*.md")):
        if p.name.startswith("._"):
            continue
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
1. **精确复制**：`internal_ref`/`customs_declaration_no`/`customer_ref`/`mbl_no`/`hbl_no`/`po_no`/`container_no`/`seal_no` 以及所有人名字段必须从原文逐字符复制，不改字符大小写、不改形近字（如晔/晰）或 O/0、I/1、B/8。无法逐字确认的人名填 null，并写入 `review_issues`。
2. **数字不计算**：`packages`/`gross_weight_kg`/`volume_cbm` 只做格式清洗（去空格、去单位、去千分位逗号），不求和。原文吨/T 时换算为 KG，并在对应 remark 说明。
3. **港口/箱型/船公司归一**：严格按 references 中的映射表，不自由发挥。
4. **多柜展开**：`3*40HC` → 一条 qty=3；`1*40HQ + 2*40GP` → 两条；每柜有独立明细/单号时每柜一条。
5. **空值一律 null**：不用空字符串、不用 0（除非原文明确 0）。但有业务含义的待查描述不是空值，例如中转港的`见设备交接单`必须保留原文。
6. **日期格式**：date 用 `YYYY-MM-DD`，datetime 用 `YYYY-MM-DDTHH:MM:SS`。无法确定年份则 null。
7. **中转港待查信息保真**：原文出现 `见设` / `见设备单` / `见设备交接单` 等描述时，`transit_port` 逐字保留该描述，不能输出 null，避免下游误判为直达。
8. **不臆造**：拿不准就 null。
9. **字段名必须用英文**：JSON key 必须严格使用 schema.md 定义的英文 key（如 `mbl_no`、`carrier`、`pol`、`pod`、`etd`），禁止输出中文 key。中文只用于读取原文字段和最终展示，调用方会从校验后的英文 JSON 直接渲染。
10. **字段语义隔离**：`我司业务编号`/`我司编号`只进 `internal_ref`；只有明确的`报关单号`/`关单号`才进 `customs_declaration_no`。收件货代和个人发货人都不能充当 `shipper_company`，缺失就填 null。
11. **PO 不丢失**：PO 号填入对应 `containers[].po_no`；多个柜的 PO 全部保留。调用方会将其汇总到订单 `c_note`。
12. **冲突必须阻断**：同一柜出现两组件数/毛重/体积时，主值按原文明确标注选取，所有候选值写入该柜 `remark`，并新增 blocking 的 `review_issues`（code=`conflicting_container_data`，field=`containers[N]`，source_values 列出各组原值）。不得自行裁决为可下单。
13. **柜级信息不得上浮丢失**：`柜1：博特装柜`、`柜2备注：先装 A 厂` 等只属于某柜的描述，必须写入对应 `containers[N].remark`；可以同时汇总到顶层 `remark`，但不得只保留顶层。
14. **承运人交叉校验**：`carrier` 与主提单号前缀冲突时以 carriers.md 的主提单号前缀为准，并写入 blocking 的 `review_issues`，不得让摘要和 JSON 使用不同值。
15. **关键箱数据缺失必须复核**：同一柜的 `packages` 与 `volume_cbm` 均为空时，写入一个 blocking 的 `review_issues`（code=`missing_container_measurements`，field=`containers[N]`）。

# 中文字段映射
- `我司编号`/`业务编号` → `internal_ref`
- `提单号` → `mbl_no`
- `船名航次` → `vessel` + `voyage`；优先按 `/` 或 `V.` 拆分，无法可靠拆分时不得猜测
- `承运人`/`船公司` → `carrier`
- `中转港` → `transit_port`；`见XX文件`等描述逐字保留
- `目的港` → `pod`
- `开航时间`/`开船时间`/`船期` → `etd`；`开港时间`不是 ETD，放入 `remark`
- `做箱时间`/`装箱日期`/`拆装箱日期` → `loading_time`
- `件数`/`毛重`/`体积` → `containers[].packages`/`gross_weight_kg`/`volume_cbm`，去单位和千分位后输出数字
- `箱型箱量` → `containers[].type` + `qty`，例如 `3*40HC` → `type="40HC", qty=3`
- `门点地址` → `factory.address`；`工厂联系人`/`工厂电话` → `factory.contact`/`factory.phone`
- `收件方` → `recipient`；`发货人公司` → `shipper_company`，只有个人姓名时 `shipper_company=null`
- 其他无法可靠归类的信息 → `remark`

缺少年份的业务日期按“文档日期 → 文件名或业务编号中的年份 → 当前年份”依次推断；只有前两项均无年份线索时才可使用当前年份，仍有歧义则输出 null 并进入人工复核。
最终中文展示由调用方从校验后的 JSON 生成；不要另外生成一份中文摘要。

# 输出格式
只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字。
必须包含 `source` 对象，字段由调用方指定（file / doc_format / extracted_at）。
`raw_text_snippet` 由调用方从转换原文截取；不要改写或总结。

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

## 展示格式
{display_format_md}
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
        display_format_md=refs.get("display-format", ""),
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
    image_data_urls: str | list[str], filename: str, doc_format: str, extracted_at: str
) -> list[dict]:
    urls = [image_data_urls] if isinstance(image_data_urls, str) else image_data_urls
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
        *[{"type": "image_url", "image_url": {"url": url}} for url in urls],
    ]


# ---------------------------------------------------------------------------
# 用户可读文本展示 —— 中文标签严格按 schema.md 字段说明
# ---------------------------------------------------------------------------

# JSON 字段 → 中文标签映射表（严格按 schema.md 中的"说明"列）
_FIELD_LABELS: dict[str, str] = {
    "internal_ref": "我司业务编号",
    "customs_declaration_no": "报关单号",
    "customer_ref": "客户编号",
    "mbl_no": "提单号",
    "hbl_no": "子提单号",
    "vessel": "船名",
    "voyage": "航次",
    "carrier": "承运人",
    "pol": "起运港",
    "pod": "目的港",
    "transit_port": "中转港",
    "terminal": "港区",
    "etd": "船期",
    "si_cutoff": "截单时间",
    "customs_cutoff": "截关时间",
    "loading_time": "做箱时间",
    "shipper_company": "托运人公司",
    "shipper_agent": "委托公司",
    "recipient": "收件方",
    "doc_date": "日期",
    "sender": "发货方",
    "sender_contact": "发货联系人",
    "remark": "备注",
}

_CONTAINER_FIELD_LABELS: dict[str, str] = {
    "type": "箱型",
    "qty": "箱量",
    "container_no": "箱号",
    "seal_no": "铅封号",
    "packages": "件数",
    "packages_unit": "件数单位",
    "gross_weight_kg": "毛重",
    "volume_cbm": "体积",
    "po_no": "PO号",
    "mbl_no": "提单号",
    "remark": "箱型备注",
}

_FACTORY_FIELD_LABELS: dict[str, str] = {
    "name": "做箱工厂",
    "address": "地址",
    "contact": "联系人",
    "phone": "电话",
}


_DOC_TYPE_LABELS: dict[str, str] = {
    "PACKING_NOTICE": "做箱通知",
    "TRANSPORT_ORDER": "运输委托书",
    "TRUCKING_ORDER": "派车托书",
    "BOOKING_NOTE": "订舱托书",
    "UNKNOWN": "未知",
}


def _label(key: str, mapping: dict[str, str]) -> str:
    return mapping.get(key, key)


def format_to_chat_text(data: dict | TuoshuOutput) -> str:
    """将最终 TuoshuOutput JSON 展开为 `中文标签: 值` 的文本。

    输入必须能通过完整 schema 校验；展示值只读取校验后的 model dump。
    """
    validated = data if isinstance(data, TuoshuOutput) else TuoshuOutput.model_validate(data)
    data = validated.model_dump()
    lines: list[str] = []

    # ---- 顶级字段 ----
    for key, label in _FIELD_LABELS.items():
        val = data.get(key)
        if val is not None and val != "":
            lines.append(f"{label}: {val}")

    # ---- containers ----
    containers = data.get("containers")
    if isinstance(containers, list):
        for index, container in enumerate(containers, start=1):
            if not isinstance(container, dict):
                continue
            if len(containers) > 1:
                lines.append(f"柜 {index}:")
            for key, label in _CONTAINER_FIELD_LABELS.items():
                val = container.get(key)
                if val is not None and val != "":
                    lines.append(f"{label}: {val}")

    # ---- factory ----
    factory = data.get("factory")
    if isinstance(factory, dict):
        for key, label in _FACTORY_FIELD_LABELS.items():
            val = factory.get(key)
            if val is not None and val != "":
                lines.append(f"{label}: {val}")

    issues = data.get("review_issues")
    if isinstance(issues, list) and issues:
        lines.append("下单校验: 需人工复核")
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            # The validated review_issues list is the sole source for this
            # section; do not infer additional problems from other fields.
            message = issue.get("message", "")
            source_values = issue.get("source_values")
            suffix = f"（原文候选：{' / '.join(source_values)}）" if source_values else ""
            lines.append(f"复核项: {message}{suffix}")

    return "\n".join(lines)
