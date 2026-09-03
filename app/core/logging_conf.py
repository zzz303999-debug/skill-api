"""结构化日志配置。"""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger import jsonlogger

from app.core.config import settings


def setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    fmt = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
