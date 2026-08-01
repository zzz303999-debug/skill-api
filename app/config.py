"""全局配置：读环境变量，其他模块统一从这里拿。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 使用基于 __file__ 的绝对路径，避免工作目录不同导致 .env 无法加载
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 9000
    api_max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    api_batch_max_files: int = Field(default=10, ge=1, le=100)
    skill_max_concurrency: int = Field(default=4, ge=1, le=64)

    # 订单创建接口
    order_api_url: str = "https://pre-s3.jxt56.com/Car/publishCreateOrder"
    order_api_ext_app_id: str = ""
    order_api_ext_user_id: str = ""
    order_api_jxt_open_id: str = ""
    order_api_user_id: str = ""
    order_api_order_info: list[dict[str, Any]] = Field(
        default_factory=lambda: [{"test": 1}]
    )
    order_api_timeout_seconds: int = Field(default=30, ge=1, le=300)

    # OpenAI-compatible LLM
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model_default: str = "gpt-5.4"
    llm_timeout_seconds: int = 180
    llm_max_retries: int = 2

    # Vision
    vision_max_pdf_pages: int = Field(default=10, ge=1, le=10)
    vision_pdf_render_scale: float = Field(default=2.0, gt=0, le=4.0)
    # 图片高置信时跳过 LLM vision 交叉核验，只用 MinerU OCR 文本走 LLM 以提速
    image_vision_skip_when_confident: bool = True

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

    # 存储
    storage_dir: Path = Path("./storage")
    storage_keep_hours: int = 24

    # 日志
    log_level: str = "INFO"


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
