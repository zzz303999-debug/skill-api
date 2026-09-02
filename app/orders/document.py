"""附件文档 → 下单接口字段转换（不实际下单）。

流程：上传文件 → 复用 tuoshu 转换链得到 markdown/vision 内容 → LLM 按
`OrderDocumentExtraction` schema 抽取 → 归一化并校验必填字段 → 组装成
下单接口的 order_data 返回，调用方自行决定是否提交。
"""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.document_parsers import mineru
from app.errors import BadRequestError, ConvertError, ParseError
from app.llm import achat_json, image_to_data_url
from app.logging_conf import get_logger
from app.skills.tuoshu.convert_service import (
    SUPPORTED_EXTS,
    convert_image_to_parse_result,
    convert_to_markdown,
    is_image,
    render_pdf_pages,
)

# 规则族 re-export（拆分兼容层：纯函数本体在 document_rules；测试与历史
# import 路径保持有效）
from .document_rules import (  # noqa: F401
    _BILL_NO_RE,
    _CN_INTERNAL_SPACE_RE,
    _COMPANY_TOKEN_RE,
    _CONTAINER_SUFFIXES,
    _CONTAINER_TYPE_RE,
    _CUSTOMER_CELL_RE,
    _CUSTOMER_LABEL_RE,
    _CUSTOMS_DECLARATION_SHIELD_RE,
    _CUSTOMS_NO_LABEL_RE,
    _CUTOFF_TIME_LABEL_RE,
    _DATE_LIKE_RE,
    _DATE_RE,
    _DEST_PORT_LABEL_RE,
    _DOC_HEADING_RE,
    _DOC_HEADING_SUFFIXES,
    _FROM_FM_RE,
    _HEADER_LINES,
    _LABEL_WORDS_RE,
    _LOADING_TIME_LABEL_RE,
    _MARKDOWN_STRIP_RE,
    _MASTER_BILL_LABEL_RE,
    _MEASUREMENT_FIELDS,
    _MEASUREMENT_LEAD,
    _MISSING_REASON_INVALID,
    _MISSING_REASON_NOT_FOUND,
    _MISSING_REASON_NOT_IN_ROW,
    _PLACEHOLDER_PORT_VALUE_RE,
    _PORT_LABEL_START_RE,
    _PORT_LABEL_TOKEN_RE,
    _TRANSIT_PORT_LABEL_RE,
    _clean_port_value,
    _customer_cell_value,
    _extract_company_name,
    _extract_customs_no,
    _extract_header_company,
    _extract_number,
    _extract_port_values,
    _format_decimal,
    _header_company_lines,
    _is_header_company,
    _is_label_cell,
    _looks_like_company,
    _missing_field_reasons,
    _missing_fields,
    _normalize_b_date_time_start,
    _normalize_bill_no,
    _normalize_boxes,
    _normalize_c_title,
    _normalize_container_type,
    _normalize_data_items,
    _normalize_date,
    _normalize_packages,
    _normalize_text,
    _normalize_volume,
    _normalize_weight,
    _revise_bill_no,
    _revise_c_title_to_value,
    _revise_loading_time,
    _revise_port_fields,
    _strip_port_label_token,
    normalize_document_extraction,
)
from .schema import OrderDocumentExtraction

log = get_logger(__name__)

def _clean_json_schema(schema: dict) -> dict:
    """去除 Pydantic 生成的 $defs / anyOf，转成模型友好的简化 schema。"""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def _resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref and ref.startswith("#/$defs/"):
                return _resolve(defs.get(ref[len("#/$defs/"):], {}))
            result: dict[str, Any] = {}
            for key, value in node.items():
                if key in ("title", "default", "$schema"):
                    continue
                if key == "anyOf":
                    options = [_resolve(opt) for opt in value]
                    non_null = [opt for opt in options if opt.get("type") != "null"]
                    has_null = len(non_null) != len(options)
                    if has_null and len(non_null) == 1:
                        nullable_type = non_null[0].get("type")
                        if isinstance(nullable_type, str):
                            non_null[0]["type"] = [nullable_type, "null"]
                        elif isinstance(nullable_type, list):
                            non_null[0]["type"] = [*nullable_type, "null"]
                        result.update(non_null[0])
                    else:
                        result["anyOf"] = options
                elif key == "properties":
                    result[key] = {pk: _resolve(pv) for pk, pv in value.items()}
                elif key == "items":
                    result[key] = _resolve(value)
                else:
                    result[key] = value
            return result
        return node

    return _resolve(schema)

_SYSTEM_PROMPT = """你是海运托书/做箱通知结构化抽取助手。读取 Markdown/图片正文，只输出符合 JSON Schema 的英文 key 对象；不输出解释或另一份摘要。拿不准填 null。

# 字段来源表（唯一目标）
| 原文标签/版面角色 | 字段 | 缺失处理 |
|---|---|---|
| `关单号`/`提单号`/`主提单号`/`主单号`/`B/L NO`/`MBL NO`（关单号即提单号；与`运编号`并存时取关单号） | `order_num1` | null |
| 文档抬头公司（顶部公司名称）或 `FM`/`FROM` 后的公司名称（发件人/托运人，仅取公司名部分，剔除同行联系人姓名与电话） | `c_title` | null |
| 做箱/装箱地址（详细街道地址，含省市区县、道路、门牌号，**并附原文现场联系人/电话**） | `factory_bei` | null |
| `做箱日期`/`装箱日期`（日期格式） | `b_date` | null |
| `做箱时间`/`装箱时间`（时间描述，如 `早上8点`/`9:00`） | `b_date_time_start` | null |
| `开船时间`/`开航时间`/`ETD` | `b_open_ship_time` | null |
| 件数（数字 + `CTNS`，单位可省略） | `packages` | null |
| 毛重（数字 + `KGS`，单位可省略，保留 2-3 位小数） | `gross_weight` | null |
| 体积（数字 + `CBM`，单位可省略，保留 2-3 位小数） | `volume` | null |
| 箱型箱量（如 `3*40HQ`、`40HQ*2`、`1x20GP+1x40HQ`） | `box` | null |
| `船名`/`VESSEL`；`船名航次`合写时拆开 | `b_ship_name` | null |
| `航次`/`船次`/`VOY`/`VOYAGE` | `b_ship_num` | null |
| `船公司`/`CARRIER` | `b_ship_company` | null |
| `目的港`/`卸货港`/`PORT OF DISCHARGE`（**最终卸货港**，不含中转港） | `b_end_dock` | null |
| `中转港`/`转运港`/`中转港代码`/`TRANSSHIPMENT PORT`（待查描述如 `见设备单` 逐字保留） | `b_end_port` | null |
| `港区`/做箱港区 | `b_wharf` | null |
| `启运港`/`装货港`/`PORT OF LOADING` | `b_start_dock` | null |
| 门点简称/`工厂名称` | `factory_name` | null |
| `装箱备注` | `b_factory_not` | null |
| `联系人`/`现场联系人`/`装箱联系人` | `c_name` | null |
| `电话`/`手机`/`TEL`（随联系人出现） | `c_phone` | null |
| `内部编号`/`运编号`/`业务编号`/`我司业务编号`（内部编号，**禁止**填入 order_num1） | `c_sn` | null |
| `备注`/`注意事项`/`REMARK`/`NOTE` | `c_note` | null |
| 货物明细行（表格中每个数据行：提单号+件数+毛重+体积，一票多客户/多提单号时每行一条） | `data[]` | null |
| 货物明细行中的 `货名`/`货物名称` | `data[].hh` | null |
| 货物明细行中的 `唛头`/`MARKS`/`N/M` | `data[].mt` | null |

# 抽取规则
1. 编号逐字复制，严禁改大小写、形近字或 O/0、I/1；图片中的红章、水印、logo、品牌图及其 OCR 一律忽略。
2. `order_num1` 必须至少 8 位且仅由数字或英文字母数字组成（纯数字也允许）；不得保留空格、连字符或其他符号；纯字母串（船名/人名）不算提单号，不符合时填 null。**关单号即提单号**；`报关单号`/`报关号` 不是提单号，禁止作为 `order_num1`；`运编号`/`业务编号`/`我司业务编号` 是内部编号（归 `c_sn`），禁止作为 `order_num1`；关单号与运编号并存时取关单号。
3. `box[].b_type` 必须是 4 位：箱长 `20`/`25`/`40` 加两位字母后缀（`GP/HC/HQ/RF/OT/TK/FR/PL/OH/RH/UT/VH` 等），与原文一致，禁止在 HQ/HC/DV/GP 等代码间改写；`box[].box_num` 为箱量（`3*40HQ` → box_num=3）。多个箱型分多条输出；**多数据行同为相同箱型时 box_num 必须累加**（如 3 个数据行各 `1*40HC` → `[{"b_type": "40HC", "box_num": 3}]`），禁止只取第一行的箱量。
4. `c_title` 取文档抬头公司（顶部公司名称）或 `FM：`/`FROM：` 后的公司名称（一般为公司名称或简称）；同行含联系人姓名/电话时只取公司名部分；都无 → 填 null（人工确认）。`TO:`/`ATTN:`/`致:` 后的值是收件/通知对象，**禁止**作为 c_title；文件名不是原文，**禁止**从文件名前缀推断 c_title。
5. `factory_bei` 取门点详细街道地址（保留省市区县、道路、门牌号和园区/楼栋信息），并**附上原文的现场联系人姓名与电话**——地址同行或独立的联系人/电话行都要并入（格式如 `金泰路转诚泰路17号 朱劲松 13776121224`，对齐订单创建接口文档：门点地址含现场联系人、电话）；不要把公司名并入地址（公司名在 `factory_name`）。
6. `b_date` 输出 `YYYY-MM-DD`；原文缺年时按文档日期、文件名年份推断，无法推断填 null。`b_date_time_start` 只取 `做箱时间`/`装箱时间` 标签后的时间描述（如 `早上8点`/`9:00`/`下午2点`），值为日期格式时归 `b_date` 而非 `b_date_time_start`；**`截单时间`/`截关时间` 等不是装箱时间，禁止填入 `b_date_time_start`**。
7. `packages`/`gross_weight`/`volume` 只清洗单位和千分位：件数为整数；毛重、体积按原文精度保留 2-3 位小数，超过 3 位四舍五入到 3 位，不补无意义的尾零；单位（CTNS/KGS/CBM）可省略。任一值 ≤0 填 null。
8. 老式 `.doc` 等文档转换后可能被展平为 `_pN_` 段落流（`_pN: (empty)_` 是空单元格）：标签与值分属不同段落，把标签后第一个非空、非标签的段落当作该标签的值；`提单号` 的 8+ 位纯数字或字母数字值可按格式特征在全文中定位。
9. `b_ship_name`/`b_ship_num`/`b_ship_company`/`b_start_dock`/`b_end_port`/`b_end_dock`/`b_wharf`/`b_open_ship_time`/`b_date_time_start`/`factory_name`/`b_factory_not`/`c_name`/`c_phone`/`c_sn`/`c_note` 等可选字段只在原文明确出现时逐字抽取；原文未给出时填 null，禁止填 `未知`/`待定`/`看设备单上`/`还未知`/`无` 等占位表述（`b_end_port` 中转港标签后明确写出的待查描述除外，见规则 13）。
10. `b_open_ship_time`/`b_date`/`b_date_time_start` 是三个不同字段：前者是开船时间，`b_date` 是做箱/装箱**日期**（`YYYY-MM-DD`），`b_date_time_start` 是做箱/装箱**时间描述**（如 `早上8点`/`9:00`），按标签与值格式严格区分，禁止混填。
11. `data` 为货物明细列表，**必须列出文档中每一个数据行**（表格数据行/按客户编号或提单号分组的行），禁止只取第一行或把多行合并成一行：每条含该行提单号 `b_order_num`（无提单号的行填 null，禁止填整票提单号）、件数 `j`、毛重 `m`、体积 `t`；某行三项（件数/毛重/体积）不全时跳过该行。`data[].hh`（货名）与 `data[].mt`（唛头）可选，原文有则逐字保留，无则 null。`packages`/`gross_weight`/`volume` 单值字段填第一条数据行的值（与 `data[0]` 一致）；文档只有一行数据时 `data` 同样输出一条。文档整体无货物明细行（件数/毛重/体积均未出现）时，`data` 输出一条仅含整票提单号的行（`b_order_num` 填 `order_num1`，件数/毛重/体积/货名/唛头填 null）。
12. `b_end_dock` 只取**最终卸货港**（`目的港`/`卸货港`/`PORT OF DISCHARGE` 标签后的值）。带 `中转港`/`转运港`/`中转港代码`/`TRANSSHIPMENT PORT` 等标签或其旁注含"中转/转运/transship"字样的港口**禁止**填入 `b_end_dock`（如"中转港：INCHON"时 INCHON 不是目的港，应填入 `b_end_port`）；原文未明确给出最终目的港（只有中转港或中转描述）时 `b_end_dock` 填 null（人工确认），禁止用中转港冒充目的港。
13. `b_end_port` 只取**中转港**（`中转港`/`转运港`/`中转港代码`/`TRANSSHIPMENT PORT` 标签后的值），与 `b_end_dock`（目的港）严格区分；**中转港标签后明确写出**的待查描述（如 `见设备单`/`见设`/`待定`）逐字保留，其他位置出现的占位词仍按规则 9 填 null，只有原文真正缺失时才是 null。
"""

def _build_system_prompt() -> str:
    return _SYSTEM_PROMPT

def _build_user_message_text(markdown: str, filename: str, doc_format: str) -> str:
    return (
        f"待抽取单据。\n"
        f"file={filename}\n"
        f"doc_format={doc_format}\n\n"
        f"===== 文档内容开始 =====\n"
        f"{markdown}\n"
        f"===== 文档内容结束 =====\n\n"
        f"请输出 JSON。"
    )

def _build_user_message_vision(
    image_data_urls: str | list[str],
    filename: str,
    doc_format: str,
    parsed_text: str | None = None,
) -> list[dict]:
    urls = [image_data_urls] if isinstance(image_data_urls, str) else image_data_urls
    parsed_section = ""
    if parsed_text:
        parsed_section = (
            "\n\n以下是 MinerU OCR 辅助文本，不是独立事实来源。所有自由文本字段必须在图片中"
            "肉眼可见；OCR 中存在但图片上看不到的词句必须剔除并填 null。"
            "图片与文本冲突时以图片为准：\n"
            "===== 已解析文本开始 =====\n"
            f"{parsed_text}\n"
            "===== 已解析文本结束 ====="
        )
    return [
        {
            "type": "text",
            "text": (
                f"待抽取单据（图片/扫描件）。\n"
                f"file={filename}\n"
                f"doc_format={doc_format}\n\n"
                f"请直接从图片正文中提取，忽略红色图章、水印、logo 和其他图片区域中的文字，"
                f"输出 JSON。{parsed_section}"
            ),
        },
        *[
            {
                "type": "image_url",
                "image_url": {"url": url, "detail": "high"},
            }
            for url in urls
        ],
    ]

def _convert_file(
    file_bytes: bytes, filename: str
) -> tuple[str | None, str, dict[str, Any], list[dict] | str]:
    """把附件转成 LLM 可用的输入。

    返回 (source_text, doc_format, conversion_meta, user_content)。
    user_content 为字符串（纯文本）或 list[dict]（vision 消息）。
    """
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise BadRequestError(
            f"unsupported extension: {ext}",
            details={"supported": SUPPORTED_EXTS},
        )

    doc_format = ext.lstrip(".")
    source_text: str | None = None
    conversion_meta: dict[str, Any] = {}

    if is_image(ext):
        parse_result = convert_image_to_parse_result(file_bytes, filename)
        doc_format = parse_result.input_format
        conversion_meta = parse_result.meta()
        source_text = parse_result.markdown or None
        # 当前 LLM 无视觉能力（llm_vision_enabled=False）时一律跳过 vision
        skip_vision = (
            not settings.llm_vision_enabled
            or (
                settings.image_vision_skip_when_confident
                and parse_result.parser == "mineru"
                and not parse_result.parser_fallback
            )
        )
        if parse_result.vision_images and not skip_vision:
            images = list(parse_result.vision_inputs)
            total_image_bytes = sum(len(image) for image, _ in images)
            if total_image_bytes > settings.vision_max_image_bytes:
                if not source_text:
                    # 无 OCR 文本可降级时，绝不能把空文档喂给 LLM——模型会输出
                    # 整份捏造数据。直接拒绝并提示压缩/拆分图片。
                    raise ConvertError(
                        "image exceeds the vision upload limit and no OCR text is "
                        "available; compress or split the image and retry",
                        code="vision_image_too_large",
                        details={
                            "file": Path(filename).name,
                            "bytes": total_image_bytes,
                            "max_bytes": settings.vision_max_image_bytes,
                        },
                    )
                conversion_meta["vision_skipped_reason"] = "image_too_large"
                user_content = _build_user_message_text(
                    source_text or "", filename, doc_format
                )
            else:
                data_urls = [
                    image_to_data_url(image, mime=mime)
                    for image, mime in images
                ]
                user_content = _build_user_message_vision(
                    data_urls,
                    filename,
                    doc_format,
                    parsed_text=source_text,
                )
        else:
            if skip_vision:
                conversion_meta["vision_cross_check"] = "skipped_confident"
            if not source_text:
                # 无 OCR 文本可降级且模型无视觉时，绝不能把空文档喂给 LLM——
                # 模型会输出整份捏造数据。直接拒绝并提示检查 MinerU 服务。
                raise ConvertError(
                    "image has no OCR text and the current LLM model has no "
                    "vision capability; check the MinerU service or use a "
                    "vision-capable model",
                    code="vision_disabled_no_ocr",
                    details={"file": Path(filename).name},
                )
            user_content = _build_user_message_text(
                source_text, filename, doc_format
            )
        return source_text, doc_format, conversion_meta, user_content

    markdown = convert_to_markdown(file_bytes, filename)
    parse_result = getattr(markdown, "parse_result", None)
    if parse_result is not None:
        conversion_meta = parse_result.meta()
    else:
        parser = getattr(markdown, "parser", None)
        if parser:
            conversion_meta = {"parser": parser}

    if markdown.startswith("SCAN_OR_IMAGE_HINT:"):
        # 扫描件 PDF：无视觉模型时交 MinerU OCR，否则整本转图片走 vision
        if ext != ".pdf":
            raise ConvertError(
                "document has no extractable text; convert it to PDF/image",
                details={
                    "file": Path(filename).name,
                    "convert_hint": markdown.partition("#")[2].strip(),
                },
            )
        if not settings.llm_vision_enabled:
            # 扫描 PDF：模型无视觉，改交 MinerU OCR 解析而不是直接拒绝
            try:
                scanned = mineru.parse_document(
                    file_bytes, filename, mime_type="application/pdf"
                )
            except Exception as exc:
                raise ConvertError(
                    "scan PDF has no extractable text and MinerU OCR failed; "
                    "check the MinerU service or use a text-based PDF",
                    code="vision_disabled_no_ocr",
                    details={
                        "file": Path(filename).name,
                        "mineru_error": f"{exc.__class__.__name__}: {exc}",
                    },
                ) from exc
            if not scanned.markdown.strip():
                raise ConvertError(
                    "scan PDF has no extractable text and MinerU OCR returned "
                    "empty; check the MinerU service or use a text-based PDF",
                    code="vision_disabled_no_ocr",
                    details={"file": Path(filename).name},
                )
            # MinerU OCR 成功：走纯文本抽取；扫描件无独立文本层可交叉核验
            conversion_meta = {
                "parser": "mineru",
                "parser_fallback": True,
                "input_format": "pdf",
                "ocr_unverified": True,
            }
            return (
                scanned.markdown,
                doc_format,
                conversion_meta,
                _build_user_message_text(scanned.markdown, filename, doc_format),
            )
        page_images = render_pdf_pages(
            file_bytes,
            max_pages=settings.vision_max_pdf_pages,
            scale=settings.vision_pdf_render_scale,
        )
        total_image_bytes = sum(len(image) for image in page_images)
        if total_image_bytes > settings.vision_max_image_bytes:
            # 渲染出的 PNG 总字节同样受 vision 直传上限约束，超限时报错提示拆分
            raise ConvertError(
                "rendered scan pages exceed the vision upload limit; "
                "split the PDF into smaller parts and retry",
                code="vision_image_too_large",
                details={
                    "file": Path(filename).name,
                    "bytes": total_image_bytes,
                    "max_bytes": settings.vision_max_image_bytes,
                },
            )
        data_urls = [image_to_data_url(image, mime="image/png") for image in page_images]
        conversion_meta = {
            "parser": "vision",
            "parser_fallback": True,
            "input_format": "pdf",
        }
        return None, doc_format, conversion_meta, _build_user_message_vision(
            data_urls, filename, doc_format
        )

    source_text = str(markdown)
    if (
        parse_result is not None
        and parse_result.vision_images
        and settings.llm_vision_enabled
    ):
        images = list(parse_result.vision_inputs)
        total_image_bytes = sum(len(image) for image, _ in images)
        if total_image_bytes > settings.vision_max_image_bytes:
            conversion_meta["vision_skipped_reason"] = "image_too_large"
            user_content = _build_user_message_text(source_text, filename, doc_format)
        else:
            data_urls = [
                image_to_data_url(image, mime=mime) for image, mime in images
            ]
            user_content = _build_user_message_vision(
                data_urls,
                filename,
                doc_format,
                parsed_text=source_text,
            )
    else:
        user_content = _build_user_message_text(source_text, filename, doc_format)
    return source_text, doc_format, conversion_meta, user_content

def build_document_order_data(
    extracted: OrderDocumentExtraction,
    *,
    customer_id: str = "",
) -> dict[str, Any]:
    """组装下单接口 data 参数（不调用上游）。

    字段结构对齐标准订单格式（与 /orders 自由文本下单的 order_data 一致）；
    缺失的字段保持 null，做箱日期缺失时 driver 显示空对象 [{}]（对齐标准格式）。

    注意：本函数不做必填校验，缺失字段保持空值；由调用方通过
    `_missing_fields` 判断是否需要人工确认。
    """
    data = {
        "order_num1": extracted.order_num1,
        "type": 1,
        "c_title": extracted.c_title,
        "c_name": extracted.c_name,
        "c_phone": extracted.c_phone,
        "b_ship_name": extracted.b_ship_name,
        "b_ship_num": extracted.b_ship_num,
        "b_ship_company": extracted.b_ship_company,
        "factory_name": extracted.factory_name,
        "factory_bei": extracted.factory_bei,
        "b_factory_not": extracted.b_factory_not,
        "b_start_dock": extracted.b_start_dock,
        "b_end_port": extracted.b_end_port,
        "b_end_dock": extracted.b_end_dock,
        "b_wharf": extracted.b_wharf,
        "b_open_ship_time": extracted.b_open_ship_time,
        "c_sn": extracted.c_sn,
        "c_note": extracted.c_note,
        "data": (
            [
                {
                    # 行内无提单号时回填主提单号（对齐自由文本 mapper 契约：下游每行非空）
                    "b_order_num": item.b_order_num or extracted.order_num1,
                    "j": item.j,
                    "m": item.m,
                    "t": item.t,
                    "hh": item.hh,
                    "mt": item.mt,
                }
                for item in extracted.data
            ]
            if extracted.data
            else None
        ),
        "box": [
            {"b_type": item.b_type, "box_num": item.box_num}
            for item in extracted.box
        ],
        "driver": (
            _build_driver_entries(extracted)
        ),
    }
    # 客户 ID 已无配置来源，仅在显式传入时注入（缺省不发送）
    if customer_id:
        data["c_id"] = customer_id
    return data

def _build_driver_entries(extracted: OrderDocumentExtraction) -> list[dict[str, str]]:
    """组装 driver 行：装箱日期 b_date 与装箱时间 b_date_time_start 都有则
    合并为一条；都无则返回 [{}]（对齐标准订单格式）。"""
    entry: dict[str, str] = {}
    if extracted.b_date:
        entry["b_date"] = extracted.b_date
    if extracted.b_date_time_start:
        entry["b_date_time_start"] = extracted.b_date_time_start
    return [entry] if entry else [{}]

# ---- 主流程 ----

async def parse_document_to_order_async(
    file_bytes: bytes,
    filename: str,
    *,
    customer_id: str = "",
) -> dict[str, Any]:
    """parse_document_to_order（2026-09 异步化改造后为生产唯一入口）（Phase 3 路由异步化）：

    - 转换段（_convert_file：LibreOffice 转换/PDF 渲染 CPU 密集 + MinerU 网络）
      整体入线程池——不阻塞事件循环；MinerU 的异步化需拆分 _convert_file
      内部管线（~200 行混合 CPU/网络），留待后续迭代（此处注释标记）；
    - LLM 抽取（最长等待段，timeout 180s）走 achat_json 真异步；
    - 兑底修复/归一化/组装段。
    """
    import asyncio

    source_text, doc_format, conversion_meta, user_content = await asyncio.to_thread(
        _convert_file, file_bytes, filename
    )
    extracted_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    system = _build_system_prompt()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    output_schema = _clean_json_schema(OrderDocumentExtraction.model_json_schema())
    raw, llm_meta = await achat_json(
        messages, temperature=0.0, json_schema=output_schema
    )
    if not isinstance(raw, dict):
        raise ParseError("LLM output must be a JSON object")

    # 兑底修复：以下
    if raw_value := raw.get("c_title"):
        raw["c_title"] = _revise_c_title_to_value(raw_value, source_text)
    elif source_text:
        raw["c_title"] = _extract_header_company(source_text)
    raw["b_end_port"], raw["b_end_dock"] = _revise_port_fields(
        raw.get("b_end_port"), raw.get("b_end_dock"), source_text
    )
    raw["order_num1"] = _revise_bill_no(raw.get("order_num1"), source_text)
    if raw.get("b_date_time_start"):
        raw["b_date_time_start"] = _revise_loading_time(
            raw.get("b_date_time_start"), source_text
        )

    extracted = normalize_document_extraction(raw)
    missing = _missing_fields(extracted)
    missing_reasons = _missing_field_reasons(raw, extracted)
    order_data = build_document_order_data(extracted, customer_id=customer_id)

    safe_meta = {key: llm_meta.get(key) for key in ("model", "usage") if key in llm_meta}
    safe_meta.update(conversion_meta)
    safe_meta.update(
        {
            "extracted_at": extracted_at,
            "doc_format": doc_format,
            "source_sha256": hashlib.sha256(file_bytes).hexdigest(),
            "source_bytes": len(file_bytes),
            "order_created": False,
        }
    )
    vision_degraded = "vision_skipped_reason" in conversion_meta
    return {
        "file": filename,
        "extracted": extracted.model_dump(),
        "order_data": order_data,
        "needs_manual_confirmation": bool(missing) or vision_degraded,
        "missing_fields": missing,
        "missing_reasons": missing_reasons,
        "meta": safe_meta,
    }
