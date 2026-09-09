"""全局配置：读环境变量，其他模块统一从这里拿。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 使用基于 __file__ 的绝对路径，避免工作目录不同导致 .env 无法加载
# 三级 parent：app/core/config.py → app/core → app → 项目根（迁移后目录深度 +1）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 运行环境（test/prod）：决定 config/fee_price_map.{env}.yaml 等按环境隔离的
    # 配置文件名；环境切换只换文件，代码零环境名/if。
    # validation_alias：环境变量/​.env 键名固定为 APP_ENV（2026-08-31 修复——
    # 此前无 alias，pydantic-settings 按字段名读 ENV，文档口径 APP_ENV 完全无效）
    env: str = Field(default="test", validation_alias="APP_ENV")

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 9000
    api_max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    # 竞品账单单次导入的最大数据行数（一柜一行），超出直接拒绝
    bill_import_max_rows: int = Field(default=10000, ge=1)
    api_batch_max_files: int = Field(default=10, ge=1, le=100)
    skill_max_concurrency: int = Field(default=4, ge=1, le=64)
    # 在途任务满时新请求排队等待的最长秒数，超时返回 503 server_busy
    skill_queue_wait_seconds: float = Field(default=10.0, ge=0, le=300)
    # 账单导入 create 模式并发下单上限（异键并行、同键串行；2026-09 异步化新增）：
    # 防止大批账单同时打满 TMS 下游；单失败隔离/不重试语义不受影响
    bill_create_concurrency: int = Field(default=4, ge=1, le=32)
    # 接口访问凭证（Bearer / X-API-Key）。生产必须设置；为空时不启用鉴权（仅限可信内网）
    api_key: str = ""

    # 订单创建接口
    order_api_url: str = "https://s3.jxt56.com/Car/publishCreateOrder"
    order_api_timeout_seconds: int = Field(default=30, ge=1, le=300)

    # 竞品账单导入下游（AddWork 端点 + sk 头鉴权；sk 由调用方登录 TMS 后经请求头透传）
    jxt_addwork_url: str = "https://s3.jxt56.com/Car/WorkOut/AddWork"
    jxt_timeout_seconds: int = Field(default=30, ge=1, le=300)
    # 舱单导入下游（addBill 端点，JSON body + sk 头；2026-08-19 抓包确认，
    # 与 AddWork 不同域名不同通道，超时复用 jxt_timeout_seconds 口径独立配置）。
    # 默认值固定为生产地址（生产 .env 零配置；本地联调在本地 .env 覆盖 test-service）
    jxt_manifest_addbill_url: str = "https://service.jxt56.com/crm/order/bill/addBill"
    # to_other 长尾监控阈值（T14）：某原费目名归并次数 ≥ 该值 → 对账报告 warning
    # 提示升级为显式映射（补映射 = 改模板 fees.mapping，零代码）
    fee_to_other_warning_threshold: int = Field(default=50, ge=1, le=10000)

    # OpenAI-compatible LLM
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model_default: str = "gpt-5.4"
    llm_timeout_seconds: int = 180
    llm_max_retries: int = 2
    # 关闭思考模式（disabled）可大幅降低 reasoning token 与响应耗时；
    # 结构化抽取任务默认关闭；模型不支持该参数时 client 会自动降级重试
    llm_thinking_mode: str = "disabled"

    # Vision
    # LLM 是否具备视觉（多模态）能力；当前生产模型无视觉时为 False：
    # 图片/扫描件一律只走 OCR 文本，禁止把原图发给 LLM
    llm_vision_enabled: bool = False
    vision_max_pdf_pages: int = Field(default=10, ge=1, le=10)
    vision_pdf_render_scale: float = Field(default=2.0, gt=0, le=4.0)
    # 图片高置信时跳过 LLM vision 交叉核验，只用 MinerU OCR 文本走 LLM 以提速
    image_vision_skip_when_confident: bool = True
    # 原图 base64 直传 LLM 的字节数上限；超过则跳过 vision 并标记人工复核
    # （base64 会膨胀约 1/3，避免超网关请求体限制）
    vision_max_image_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)

    # PDF 文本层页级质量探测
    parser_text_min_chars: int = Field(default=50, ge=1, le=1000)
    parser_garbled_ratio_threshold: float = Field(default=0.05, ge=0, le=1)

    # MinerU（PDF 结构化解析；失败时可降级到现有解析器）
    mineru_enabled: bool = False
    mineru_base_url: str = ""
    mineru_endpoint: str = "/file_parse"
    mineru_api_key: str = ""
    mineru_expected_version: str = "2.5.4"
    mineru_timeout_seconds: int = Field(default=120, ge=1, le=1800)
    mineru_fallback_enabled: bool = True
    mineru_ocr_concurrency: int = Field(default=4, ge=1, le=16)

    health_probe_enabled: bool = True
    health_probe_timeout_seconds: float = Field(default=2.0, ge=0.5, le=5.0)

    # 存储与配置目录
    storage_dir: Path = Path("./storage")
    # YAML 配置目录（fee_price_map.*/fee_alias_dictionary 等）
    config_dir: Path = Path("./config")
    # 审计场景建议调大（如 720 = 30 天），保证留痕可追溯
    storage_keep_hours: int = 24

    @field_validator("storage_dir")
    @classmethod
    def _resolve_storage_dir(cls, value: Path) -> Path:
        """相对路径统一基于项目根目录解析，避免依赖进程 CWD（容器内 CWD 可能变化）。"""
        path = Path(value)
        return path if path.is_absolute() else _PROJECT_ROOT / path

    @field_validator("config_dir")
    @classmethod
    def _resolve_config_dir(cls, value: Path) -> Path:
        """相对路径统一基于项目根目录解析（与 storage_dir 同规则）。"""
        path = Path(value)
        return path if path.is_absolute() else _PROJECT_ROOT / path

    # 请求访问日志（审计）
    # 是否记录 JSON 请求体内容；云服务审计建议保持开启
    access_log_record_body: bool = True
    # 请求体记录的最大字符数，超出截断并标记 body_truncated
    access_log_body_max_chars: int = Field(default=4096, ge=0, le=100_000)
    # 是否记录 JSON 响应体（输出结果）；前端 logs 页面展示用。
    # 注意：开启时 JSON 响应会被全量缓冲（内存峰值与响应体量相关，
    # 截断只影响落盘内容，客户端仍收到完整响应）
    access_log_record_response: bool = True
    # 响应体记录的最大字符数，超出截断并标记 response_truncated
    # （仅截断日志内容，客户端仍收到完整响应）
    access_log_response_max_chars: int = Field(default=64 * 1024, ge=0, le=1_000_000)
    # 响应摘要化：路径子串匹配（逗号分隔）的 JSON 响应，日志只记录排查摘要
    # （如 /orders/bill/import 丢弃 orders/canonical_orders 等大明细，保留
    # 文件/区间/计数/建单统计/模板与建档计数），避免审计日志过大、logs 页面
    # 难排查；置空则恢复完整记录。仅影响日志内容，客户端响应始终完整。
    access_log_summarize_paths: str = "/orders/bill/import"
    # 摘要化时完整响应体单独保留的最大字符数（response_full，导出/取证用，
    # 超限截断并标记 response_full_truncated）；仅摘要命中路径写入，
    # 不影响其他接口的日志体积。
    access_log_response_full_max_chars: int = Field(
        default=1024 * 1024, ge=0, le=5_000_000
    )
    # 是否信任反向代理头（X-Forwarded-For / X-Real-IP）；
    # 云服务前面有 nginx/负载均衡时开启，才能拿到真实客户端 IP
    access_log_trust_proxy: bool = True

    # 请求限流（内存滑动窗口，按客户端 IP 计；仅单进程部署下精确）
    # 生产开启；本地开发默认关闭
    rate_limit_enabled: bool = False
    # heavy 档（/orders、/skills/* 等 LLM/转换密集型接口）窗口内最大请求数
    rate_limit_heavy_max_requests: int = Field(default=10, ge=1, le=100_000)
    # heavy 档窗口秒数
    rate_limit_heavy_window_seconds: int = Field(default=60, ge=1, le=3600)
    # light 档（/api/logs 日志查询）窗口内最大请求数
    rate_limit_light_max_requests: int = Field(default=120, ge=1, le=1_000_000)
    # light 档窗口秒数
    rate_limit_light_window_seconds: int = Field(default=60, ge=1, le=3600)
    # 免限流 IP 白名单，逗号分隔（如内网网关）；留空则不豁免
    rate_limit_whitelist: str = ""

    # 日志
    log_level: str = "INFO"


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
