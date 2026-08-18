"""统一错误类型。

所有服务内错误通过 ``SkillAPIError`` 子类抛出，最终由全局异常处理器
统一转换为 ``{"error": {"code", "message", "description", "details"}}``。

- ``code``：机器可读的错误码（英文 snake_case），下游可据此做分支处理
- ``message``：技术性错误信息（英文），定位用
- ``description``：错误码的中文说明（含常见原因），供调用方直接展示/判断
- ``details``：可选的补充上下文（状态码、字段、预览等）

新增错误码时必须同步在 ``ERROR_CODE_DESCRIPTIONS`` 登记中文说明，
否则调用方无法判断错误含义。
"""

from __future__ import annotations

# 错误码 → 中文说明注册表。
# 接口层所有错误码必须在此登记；description 会随 API 响应返回，
# 说明需写清"发生了什么 + 常见原因"，方便调用方直接判断。
ERROR_CODE_DESCRIPTIONS: dict[str, str] = {
    # ---- 通用 ----
    "internal_error": "服务内部错误，请稍后重试或联系管理员",
    "bad_request": "请求参数或上传内容不合法，请检查后重试",
    # ---- 上传/文件校验 ----
    "file_too_large": "上传文件超过大小限制，请压缩或拆分后重试",
    "payload_too_large": "请求体超过大小限制，请压缩或拆分后重试",
    "empty_file": "上传文件为空，请重新上传有效文件",
    "text_too_large": "文本内容超过大小限制，请精简后重试",
    "empty_batch": "批量接口未收到任何文件，请至少上传一个文件",
    "too_many_files": "批量上传文件数量超过限制，请分批处理",
    "unsupported_image_content": "图片内容不是支持的格式（jpg/png/bmp/tiff/gif/webp），请转换后重试",
    "file_format_mismatch": "文件扩展名与实际内容格式不符，请检查文件是否正确",
    "header_mapping_rejected": "账单表头 AI 映射未通过校验（必映射字段缺失/重复映射/抽样不达标），请人工确认表头或补充模板",
    "skill_not_found": "请求的技能不存在，请检查接口路径",
    # ---- 文档转换 ----
    "convert_error": "文档转换失败：文件可能已损坏或格式不受支持，请转换格式后重试",
    "pdf_page_limit_exceeded": "PDF 页数超过单次处理上限，请拆分后重试",
    "pdf_parse_failed": "PDF 解析失败：文件可能已损坏或加密，请检查文件",
    "empty_converted_content": "文档转换后无有效内容，文件可能为空白或纯图片扫描件",
    # ---- 订单 ----
    "order_not_ready": "订单必填信息不完整，无法下单，请补充字段后重试",
    "order_text_parse_failed": "文本订单解析结果校验失败，请检查输入文本格式",
    "order_upstream_error": "订单系统拒绝了请求或不可达，请稍后重试",
    # ---- 限流 ----
    "rate_limited": "请求过于频繁，已被限流，请稍后重试",
    "server_busy": "服务繁忙（并发处理任务已满），请稍后重试",
    # ---- 鉴权 ----
    "unauthorized": "未授权访问，请提供有效的访问凭证（API Key）",
    # ---- Vision ----
    "vision_image_too_large": "图片超过 vision 直传上限且无 OCR 文本可用，请压缩或拆分图片后重试",
    # ---- 内容解析 ----
    "parse_error": "内容解析失败（LLM 未返回有效 JSON 或结构不匹配），请检查文件内容",
    "llm_error": "LLM 调用失败（模型未返回有效内容）；常见原因：文件类型不受支持、内容无法解析、模型异常，请检查上传文件",
    "llm_upstream": "LLM 网关拒绝了请求（限流、网关故障或参数问题），请稍后重试",
    "llm_response_format_unsupported": "LLM 网关不支持结构化输出，已自动降级处理",
    "llm_network": "无法连接 LLM 网关（超时或网络故障），请稍后重试",
}


class SkillAPIError(Exception):
    """所有服务内错误的基类，带 http_status 和 code。"""

    http_status: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict | None = None,
        description: str | None = None,
    ):
        super().__init__(message)
        if code:
            self.code = code
        self.message = message
        self.details = details or {}
        # 中文说明：显式传入优先，否则查错误码注册表；查不到则回退为英文 message
        self.description = description or ERROR_CODE_DESCRIPTIONS.get(self.code, message)


class BadRequestError(SkillAPIError):
    """400：请求参数或上传内容不合法。"""

    http_status = 400
    code = "bad_request"


class SkillNotFoundError(SkillAPIError):
    """404：请求的技能不存在。"""

    http_status = 404
    code = "skill_not_found"


class ConvertError(SkillAPIError):
    """422：文档转换失败。"""

    http_status = 422
    code = "convert_error"


class LLMError(SkillAPIError):
    """502：LLM 调用失败。"""

    http_status = 502
    code = "llm_error"


class ServiceBusyError(SkillAPIError):
    """503：服务繁忙（在途任务已满，排队超时）。"""

    http_status = 503
    code = "server_busy"


class ParseError(SkillAPIError):
    """502：内容解析失败。"""

    http_status = 502
    code = "parse_error"
