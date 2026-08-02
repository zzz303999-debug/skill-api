"""请求访问日志：记录到 JSONL 文件（持久化）+ 内存环形缓冲（查询），并提供查询。

- `record()` 由 FastAPI 中间件调用，追加一条请求日志；
- `query()` 供 `GET /api/logs` 查询，只读内存缓冲（最新在前），避免每次读文件；
- 服务重启后首次使用时会把文件尾部加载进内存，保证历史可查。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

# 内存环形缓冲上限：只保留最近条目供查询，避免内存无限增长
_MAX_MEMORY_ENTRIES = 5000
# 日志文件超过该大小后轮转（归档为 requests.jsonl.1 并清理过期归档）
_MAX_LOG_FILE_BYTES = 64 * 1024 * 1024
# 每写入多少条检查一次文件大小，避免每次请求都 stat
_ROTATE_CHECK_INTERVAL = 1000
# 首次加载时只从文件尾部读取这么多字节，避免读全量历史文件
_LOAD_TAIL_BYTES = 4 * 1024 * 1024

_entries: deque[dict[str, Any]] = deque(maxlen=_MAX_MEMORY_ENTRIES)
_lock = threading.Lock()
_loaded = False
_write_count = 0

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
                # 只读尾部，避免首次请求同步读取无限增长的全量文件
                with path.open("rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    start = max(0, size - _LOAD_TAIL_BYTES)
                    fh.seek(start)
                    data = fh.read().decode("utf-8", errors="replace")
                lines = data.splitlines()
                if start > 0:
                    # 从文件中间开始的第一行可能不完整
                    lines = lines[1:]
                for line in lines:
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


def _rotate_log_file(path: Path) -> None:
    """归档当前日志文件并清理超过 storage_keep_hours 的旧归档。"""
    try:
        archive = path.with_suffix(path.suffix + ".1")
        if archive.exists():
            archive.unlink()
        path.rename(archive)
    except OSError as exc:
        log.warning("request_log_rotate_failed", extra={"error": str(exc)})
        return
    keep_seconds = max(settings.storage_keep_hours, 1) * 3600
    now = time.time()
    for candidate in path.parent.glob(path.name + ".*"):
        try:
            if now - candidate.stat().st_mtime > keep_seconds:
                candidate.unlink()
        except OSError:
            continue


def _maybe_rotate(path: Path) -> None:
    global _write_count
    _write_count += 1
    if _write_count % _ROTATE_CHECK_INTERVAL != 0:
        return
    try:
        if path.stat().st_size < _MAX_LOG_FILE_BYTES:
            return
    except OSError:
        return
    _rotate_log_file(path)


def record(entry: dict[str, Any]) -> None:
    """追加一条请求日志：写 JSONL 持久化 + 进内存环形缓冲。

    文件写入失败只记 warning，不影响请求本身。文件超过大小阈值时
    自动轮转并清理过期归档（受 storage_keep_hours 控制）。
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
        _maybe_rotate(path)

def query(
    *,
    limit: int = 200,
    offset: int = 0,
    path: str | None = None,
    file: str | None = None,
    status: int | None = None,
    request_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """查询请求日志，按时间倒序（最新在前）。

    支持按路径、文件名（子串）、状态码、请求 ID、客户端 IP 过滤，返回分页结果。
    """
    _ensure_loaded()
    needle_path = path.strip().lower() if path else None
    needle_file = file.strip().lower() if file else None
    needle_ip = ip.strip().lower() if ip else None
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
    if needle_ip:
        items = [e for e in items if needle_ip in str(e.get("ip", "")).lower()]
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": page,
    }
