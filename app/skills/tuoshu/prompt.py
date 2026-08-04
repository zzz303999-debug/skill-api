"""托书抽取 prompt 组装。

在启动时把 references/*.md 全部加载到内存，避免每次读盘。
few-shot 样例按需选一部分附上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .schema import TuoshuOutput

REF_DIR = Path(__file__).parent / "references"
EX_DIR = REF_DIR / "examples"


@dataclass(frozen=True)
class PromptRoute:
    """Cheap local routing result used to select prompt context."""

    doc_type: str
    template_hint: str | None = None


_TEMPLATE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("neizhuang_booking", ("内装箱委托书", "船公司", "要求进港时间")),
    ("bingsheng_transport", ("1sha044022", "bsse2105280058")),
    ("xilinmen_grid", ("浙江经茂国际货运代理", "箱单备注", "装箱工厂喜临门")),
    ("yuhai_table", ("上海育海国际货运有限公司", "育海编号")),
    ("wxhyc_transport_para", ("运输委托书", "我司编号:wxhyc", "拆装箱日期")),
    ("bolian_segway", ("江苏倍联现代物流有限公司", "常州赛格威做箱通知")),
    ("bolian_tegewei", ("倍联业务编号", "特格威", "保税区")),
    ("xinyijie_paira", ("上海欣一捷", "货物配舱通知书")),
    ("xinjie_truck", ("嘉兴新捷国际货运代理有限公司车队装箱通知单",)),
    ("qide_door", ("启德物流有限公司", "门点做箱通知")),
    ("sanrenxing_booking", ("上海三人行供应链管理", "装箱委托单")),
    ("zuoxiang_std_esff", ("做箱通知书", "我司业务编号", "船名航次", "esff")),
)

_TEMPLATE_EXAMPLES = {
    "neizhuang_booking": "zuoxiang_std_esff",
    "bingsheng_transport": "bingsheng_transport",
    "zuoxiang_std_esff": "zuoxiang_std_esff",
    "xilinmen_grid": "xilinmen_grid",
    "yuhai_table": "yuhai_table",
    "wxhyc_transport_para": "wxhyc_transport_para",
    "bolian_segway": "bolian_tegewei_1",
    "bolian_tegewei": "bolian_tegewei_1",
    "xinjie_truck": "xinjie_truck",
}

_DOC_TYPE_EXAMPLES = {
    "PACKING_NOTICE": "zuoxiang_std_esff",
    "TRANSPORT_ORDER": "wxhyc_transport_para",
    "TRUCKING_ORDER": "xinjie_truck",
    "BOOKING_NOTE": "bolian_tegewei_1",
    "UNKNOWN": "zuoxiang_std_esff",
}

_TEMPLATE_DOC_TYPES = {
    "neizhuang_booking": "BOOKING_NOTE",
    "bingsheng_transport": "TRANSPORT_ORDER",
    "xilinmen_grid": "PACKING_NOTICE",
    "yuhai_table": "PACKING_NOTICE",
    "wxhyc_transport_para": "TRANSPORT_ORDER",
    "bolian_segway": "PACKING_NOTICE",
    "bolian_tegewei": "BOOKING_NOTE",
    "xinyijie_paira": "TRANSPORT_ORDER",
    "xinjie_truck": "TRUCKING_ORDER",
    "qide_door": "TRUCKING_ORDER",
    "sanrenxing_booking": "BOOKING_NOTE",
    "zuoxiang_std_esff": "PACKING_NOTICE",
}


def _normalize_route_text(text: str) -> str:
    return re.sub(r"[\s:：]", "", text).lower()


def detect_prompt_route(text: str) -> PromptRoute:
    """Route a document without spending an extra LLM call."""
    normalized = _normalize_route_text(text)
    template_hint = next(
        (
            template
            for template, markers in _TEMPLATE_MARKERS
            if all(_normalize_route_text(marker) in normalized for marker in markers)
        ),
        None,
    )

    if any(
        marker in normalized
        for marker in (
            "派车托书",
            "拖车托书",
            "拖车委托书",
            "车队托书",
            "车队装箱通知单",
            "门点装箱通知",
            "车队将于",
            "到以下地址装箱",
        )
    ):
        doc_type = "TRUCKING_ORDER"
    elif any(marker in normalized for marker in ("运输委托书", "货物配舱通知书")):
        doc_type = "TRANSPORT_ORDER"
    elif any(
        marker in normalized
        for marker in (
            "做箱委托单",
            "做箱委托书",
            "装柜托书",
            "装箱委托单",
            "内装箱委托书",
            "配舱通知",
            "bookingamendment",
            "放舱说明",
        )
    ):
        doc_type = "BOOKING_NOTE"
    elif any(
        marker in normalized
        for marker in (
            "做箱通知书",
            "做箱通知",
            "装箱通知",
            "装箱通知书",
            "装箱出运通知书",
        )
    ):
        # Some trucking templates use a generic title; operational fields win.
        trucking_fields = ("提箱", "进港时间", "司机")
        doc_type = (
            "TRUCKING_ORDER"
            if sum(marker in normalized for marker in trucking_fields) >= 2
            else "PACKING_NOTICE"
        )
    else:
        doc_type = "UNKNOWN"

    if doc_type == "UNKNOWN" and template_hint is not None:
        doc_type = _TEMPLATE_DOC_TYPES.get(template_hint, doc_type)

    return PromptRoute(doc_type=doc_type, template_hint=template_hint)


@lru_cache(maxsize=16)
def load_examples(names: tuple[str, ...]) -> list[tuple[str, str]]:
    """Load only the few-shot examples selected for this document."""
    out: list[tuple[str, str]] = []
    for name in names:
        md = EX_DIR / f"{name}.md"
        js = EX_DIR / f"{name}.json"
        if md.exists() and js.exists():
            out.append((md.read_text(encoding="utf-8"), js.read_text(encoding="utf-8")))
    return out


SYSTEM_PROMPT_TEMPLATE = """你是海运托书结构化抽取助手。读取 Markdown/图片正文，只输出符合 JSON Schema 的英文 key 对象；不输出解释或另一份摘要。拿不准填 null。

# 字段来源表（唯一目标）
| 原文标签/版面角色 | 字段 | 缺失处理 |
|---|---|---|
| `TO`/`致`/非空`ATTN` | `recipient` | null |
| `FM` 后的原文值；缺失时取明确客户栏、`客户简称+装箱/做箱通知`抬头或正文抬头公司 | `customer` | null + blocking `missing_customer` |
| 正文抬头或落款公司 | `shipper_agent` | null |
| 正文明示`发货人`/`托运人`/`SHIPPER` | `shipper_company` | null |
| `FROM`/`FM` 联系人 | `sender_contact` | null |
| `日期`/`DATE`（含相邻碎片） | `doc_date` | null |
| 无结构字段可承载的原文 | `remark` | null |

`bingsheng_transport` 模板的标题下抬头公司同时作为 `shipper_agent` 和 `shipper_company`。
`bolian_segway` 模板的“江苏倍联现代物流有限公司”抬头同时作为
`shipper_agent` 和 `shipper_company`。
其他模板不得把工厂、抬头货代当作托运人（`zuoxiang_std_esff` 的做箱工厂
只是门点工厂，只写入 `factory.name`，禁止写入 `shipper_company`）。
结构化后的收件方、公司、联系人不得重复进`remark`/`c_note`。

缺失复核提示（`review_issues`）仅限以下必提取字段缺失时生成：提单号 `mbl_no`、
箱型 `containers[].type`、客户 `customer`、地址 `factory.address`、做箱日期
`loading_time`、件数 `containers[].packages`、毛重 `containers[].gross_weight_kg`、
体积 `containers[].volume_cbm`；其余字段（承运人、船名航次、ETD、工厂名等）缺失一律
填 null，不生成任何缺失提示。`review_issues` 的 `code` 只能使用上述缺失提示及
本提示已明确给出的复核 code（如 `ungrounded_text`、`unknown_container_type`、
`conflicting_container_data`、`carrier_by_mbl` 等），严禁自创 `missing_*` 等
未定义的 code；`field` 必须使用标准字段路径，禁止 `containers[]` 这类无索引占位。
每一项必须严格按下表字段输出，字段缺失、类型不符都会导致整份结果作废重试：
`code`(非空字符串)、`field`(非空字符串)、`message`(非空字符串)、`source_values`(字符串数组，无候选时输出 [])、`blocking`(必须 true 或 false)。
完整格式示例：{{"code":"carrier_by_mbl","field":"carrier","message":"承运人由提单号前缀推断，需人工确认","source_values":["HLCUSHA2111JWDA1"],"blocking":true}}。
写不出某项的完整字段时就丢弃该项，严禁输出缺字段、null 字段或字符串形式的 source_values。

# 抽取规则
1. 编号和人名逐字复制，严禁改大小写、形近字或 O/0、I/1；图片中的红章、水印、logo、品牌图及其 OCR 一律忽略。
2. `我司编号/业务编号→internal_ref`，`报关单号/关单号→customs_declaration_no`，`提单号→mbl_no`，PO/订单号进对应 `containers[].po_no/customer_ref`。提单号必须至少 8 位且只能由数字或英文字母数字组成，不得保留空格、连字符或其他符号；不符合时填 null。
3. `船名航次→vessel+voyage`；`船 公 司` 等标签先去空白再匹配，船公司原文值（包括 `EMC CPS` 这类全称）优先于提单号推断；`中转港（卸港）` 归入 `transit_port`；其中“见设备交接单/见设”等待查原文必须保留；`开港时间`不是 `etd`。
4. `loading_time` 取做箱/装箱日期；日期为 `YYYY-MM-DD`，时间为 `YYYY-MM-DDTHH:MM:SS`；原文有时分不得降精度。MinerU 相邻单元格 `日期：20` + `21.5.28` 必须拼为 `2021-05-28`。缺年按文档日期、文件名/业务号年份推断，否则 null。
5. `factory.address` 只取可用于到达门点的详细街道地址，保留省市区县、道路、门牌号和园区/楼栋信息；不要把公司名、联系人或电话并入地址。
6. `packages/gross_weight_kg/volume_cbm` 只清洗单位和千分位，不求和；件数单位 `CTNS`、毛重单位 `KGS`、体积单位 `CBM` 都可省略；吨转 KG。毛重和体积按原文精度保留 2-3 位小数，超过 3 位时四舍五入到 3 位，不补无意义的尾零。任一值 ≤0 清空；件数为空但原文有 CTNS/PKGS 等单位时仍保留 `packages_unit`；件数和体积均缺失时加 blocking `missing_container_measurements`。
7. 标准短箱型为 4 位：箱长 `20`/`25`/`40` 加两位字母后缀，如 `GP/HC/HQ/RF/OT/TK/FR/PL/OH/RH/UT/VH`；必须与原文一致，禁止在 `HQ/HC/DV/GP` 等代码之间改写。`3*40HQ→type=40HQ,qty=3`，`3*40HC→type=40HC,qty=3`；表外或既有长代码同样保留原文并加 non-blocking `unknown_container_type`；混合箱型分行。同一表单若“总箱量”含多个重叠/残留值，但货物明细“箱型”栏只有一个明确值，以明细“箱型”栏为准，不把总箱量中的额外残留值建柜或报冲突。
8. 同柜多组件数/重量/体积时保留明确主值，全部候选写柜备注，并加 blocking `conflicting_container_data`。
9. `container_no` 仅 4 大写字母+7 数字；`seal_no` 无空格且仅字母数字 `./-`。`28GSHEN S` 等图章 OCR 填 null 并加 blocking issue。
10. `carrier` 只有原文明示承运人/船公司才是直接值；由主单前缀或船名推断时加 blocking `carrier_by_mbl/carrier_by_vessel` 并列依据；原文明示值优先，只有原文缺失时才使用前缀/船名兜底。
11. 港口州/国家修饰信息不得丢弃，`COLUMBUS(OH)` 归一为 `COLUMBUS, OH`。`source` 使用用户给出的 file/doc_format/extracted_at；所有复核项只写 `review_issues`，每个 code 只允许一条且禁止 `unstructured_review_issue`。每项必须完整包含非空字符串 `code`、`field`、`message`，以及字符串数组 `source_values` 和布尔值 `blocking`。
12. `remark`、`containers[].remark`、`seal_no` 等自由文本必须能在来源中找到依据；禁止补写原文没有的操作要求、术语或语句。图片输入时，MinerU 文本只是 OCR 辅助，原图可见文字才是最终依据；OCR 中出现但图片上看不到的词句必须剔除，并写 blocking `ungrounded_text`。`customer` 优先逐字取 `FM` 后的值（公司名称、简称或其他原文称呼），缺失时依次取明确的客户栏、`海丰装箱通知` 这类“客户简称+装箱/做箱通知”抬头和正文抬头公司；仍缺失时填 null，并添加 blocking `missing_customer` 说明。`sender_contact` 仅在 `FM/FROM` 值明确是人名时填写，页脚“联系人/我司联系人”不得填入。
13. 老式 `.doc` 等文档转换后可能被展平为 `_pN_` 段落流（`_pN: (empty)_` 是空单元格）：标签与值分属不同段落、中间隔着多个空段，值甚至可能出现在标签之前。此时把标签后第一个非空、非标签（不以冒号结尾、不含冒号）的段落当作该标签的值；`提单号/主提单号` 的 8+ 位纯字母数字值可按格式特征在全文中定位，但排除纯数字（电话/日期）、纯字母（船名/人名）以及 `数字+单位`（如 `1100CTNS`）形式的词。

`doc_type` 仅 PACKING_NOTICE/TRANSPORT_ORDER/TRUCKING_ORDER/BOOKING_NOTE/UNKNOWN。标题优先；“做箱通知”若以提箱、进港、司机为主则 TRUCKING_ORDER。本地提示：doc_type={route_doc_type}，template_hint={route_template_hint}；与原文冲突时以原文为准。
"""


def build_system_prompt(route: PromptRoute | None = None) -> str:
    route = route or PromptRoute(doc_type="UNKNOWN")
    return SYSTEM_PROMPT_TEMPLATE.format(
        route_doc_type=route.doc_type,
        route_template_hint=route.template_hint or "null",
    )


def build_few_shot_messages(route: PromptRoute | None = None) -> list[dict]:
    """Select one relevant example instead of attaching the whole example set."""
    route = route or PromptRoute(doc_type="UNKNOWN")
    example_name = _TEMPLATE_EXAMPLES.get(route.template_hint or "")
    if example_name is None:
        example_name = _DOC_TYPE_EXAMPLES[route.doc_type]

    msgs: list[dict] = []
    for md, js in load_examples((example_name,)):
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
    image_data_urls: str | list[str],
    filename: str,
    doc_format: str,
    extracted_at: str,
    parsed_text: str | None = None,
    image_detail: str = "high",
) -> list[dict]:
    urls = [image_data_urls] if isinstance(image_data_urls, str) else image_data_urls
    parsed_section = ""
    if parsed_text:
        parsed_section = (
            "\n\n以下是 MinerU OCR 辅助文本，不是独立事实来源。所有自由文本字段必须在图片中"
            "肉眼可见；OCR 中存在但图片上看不到的词句必须剔除并写 blocking "
            "ungrounded_text。图片与文本冲突时以图片为准，并写入 review_issues，不得静默选择：\n"
            "===== 已解析文本开始 =====\n"
            f"{parsed_text}\n"
            "===== 已解析文本结束 ====="
        )
    return [
        {
            "type": "text",
            "text": (
                f"待抽取托书（图片/扫描件）。\n"
                f"file={filename}\n"
                f"doc_format={doc_format}\n"
                f"extracted_at={extracted_at}\n\n"
                f"请直接从图片正文中提取，忽略红色图章、水印、logo 和其他图片区域中的文字，"
                f"输出 JSON。source 字段用上述元数据。"
                f"{parsed_section}"
            ),
        },
        *[
            {
                "type": "image_url",
                "image_url": {"url": url, "detail": image_detail},
            }
            for url in urls
        ],
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
    "carrier": "船公司",
    "pol": "起运港",
    "pod": "目的港",
    "transit_port": "中转港",
    "terminal": "港区",
    "etd": "船期",
    "si_cutoff": "截单时间",
    "customs_cutoff": "截关时间",
    "loading_time": "做箱时间",
    "customer": "客户",
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
            if key == "carrier":
                val = f"{val}（接口原始值）"
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
