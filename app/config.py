"""全局配置：读环境变量，其他模块统一从这里拿。"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    api_key: str = "change-me"

    # OpenClaw
    openclaw_base_url: str = "http://192.168.0.130:18789/v1"
    openclaw_api_key: str = ""

    # LLM
    llm_model_default: str = "openclaw"
    llm_timeout_seconds: int = 180
    llm_max_retries: int = 2

    # 存储
    storage_dir: Path = Path("./storage")
    storage_keep_hours: int = 24

    # 日志
    log_level: str = "INFO"


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
