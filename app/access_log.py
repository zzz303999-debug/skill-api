"""请求访问日志：记录到 JSONL 文件（持久化）+ 内存环形缓冲（查询），并提供查询。

- `record()` 由 FastAPI 中间件调用，追加一条请求日志；
- `query()` 供 `GET /api/logs` 查询，只读内存缓冲（最新在前），避免每次读文件；
- 服务重启后首次使用时会把文件尾部加载进内存，保证历史可查。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

# 内存环形缓冲上限：只保留最近条目供查询，避免内存无限增长
_MAX_MEMORY_ENTRIES = 5000

_entries: deque[dict[str, Any]] = deque(maxlen=_MAX_MEMORY_ENTRIES)
_lock = threading.Lock()
_loaded = False

# 测试可替换的日志文件路径；None 时使用 settings.storage_dir / logs / requests.jsonl
_request_log_path: Path | None = None


def _log_file() -> Path:
    if _request_log_path is not None:
        return _request_log_path
    return settings.storage_dir / "logs" / "requests.jsonl"


def _ensure_loaded() -> None:
    """首次使用时把历史日志文件尾部加载进内存（进程内只加载一次）。"""
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        path = _log_file()
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            _entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError as exc:
                log.warning("request_log_load_failed", extra={"error": str(exc)})
        _loaded = True


def record(entry: dict[str, Any]) -> None:
    """追加一条请求日志：写 JSONL 持久化 + 进内存环形缓冲。

    文件写入失败只记 warning，不影响请求本身。
    """
    if "ts" not in entry:
        entry["ts"] = datetime.now(UTC).isoformat(timespec="milliseconds")
    # 先加载历史再追加，避免重启后首条记录在内存中重复
    _ensure_loaded()
    with _lock:
        _entries.append(entry)
        try:
            path = _log_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("request_log_write_failed", extra={"error": str(exc)})


def query(
    *,
    limit: int = 200,
    offset: int = 0,
    path: str | None = None,
    file: str | None = None,
    status: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """查询请求日志，按时间倒序（最新在前）。

    支持按路径、文件名（子串）、状态码、请求 ID 过滤，返回分页结果。
    """
    _ensure_loaded()
    needle_path = path.strip().lower() if path else None
    needle_file = file.strip().lower() if file else None
    items = list(_entries)  # 倒序展示
    items.reverse()
    if needle_path:
        items = [e for e in items if needle_path in str(e.get("path", "")).lower()]
    if needle_file:
        items = [e for e in items if needle_file in str(e.get("file", "")).lower()]
    if status is not None:
        items = [e for e in items if e.get("status") == status]
    if request_id:
        items = [e for e in items if request_id == e.get("request_id")]
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": page,
    }
