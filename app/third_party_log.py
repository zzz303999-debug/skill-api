"""第三方接口调用日志：按天 JSONL 文件（持久化）+ 内存环形缓冲（查询）。

与请求访问日志（access_log）同模式但独立文件，互不污染：
- `record()` 由 http_client 调用，按日志时间写入当日文件
  `third-party-YYYY-MM-DD.jsonl`（一行一条结构化 JSON）；
- `query()` 供 `GET /api/third-party-logs` 查询，只读内存缓冲（最新在前），
  支持按事件（message）、endpoint、状态码、级别过滤；
- 服务重启后首次使用时回填各按天文件尾部进内存，保证近期历史可查；
- 记录失败（磁盘满等）只记 warning，不影响下游调用与响应。

文件不含轮转/过期清理（第三方调用量远小于请求量）：按天命名天然
限长，历史文件由运维按 storage_keep_hours 口径手动清理或复用 access_log
的清理策略。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from collections import deque
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

# 内存环形缓冲上限：只保留最近条目供查询，避免内存无限增长
_MAX_MEMORY_ENTRIES = 5000
# 首次加载时从各文件尾部读取的总字节预算
_LOAD_TAIL_BYTES = 4 * 1024 * 1024
# 每写入多少条检查一次过期文件清理
_CLEAN_CHECK_INTERVAL = 1000
# 按天日志文件名：third-party-2026-08-24.jsonl
_DAILY_FILE_RE = re.compile(r"^third-party-(\d{4}-\d{2}-\d{2})\.jsonl$")

_entries: deque[dict[str, Any]] = deque(maxlen=_MAX_MEMORY_ENTRIES)
_lock = threading.Lock()
_loaded = False
_write_count = 0

# 测试可替换的日志目录；None 时使用 settings.storage_dir / logs
_log_dir_override: Path | None = None


def _log_dir() -> Path:
    if _log_dir_override is not None:
        return _log_dir_override
    return settings.storage_dir / "logs"


def _daily_file(day: date) -> Path:
    return _log_dir() / f"third-party-{day.isoformat()}.jsonl"


def _read_tail(path: Path, max_bytes: int) -> list[str]:
    """读取文件尾部最多 max_bytes 字节并切分为行（首行截断时丢弃）。"""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # 从文件中间开始的第一行可能不完整
        return lines
    except OSError as exc:
        log.warning("third_party_log_load_failed", extra={"error": str(exc)})
        return []


def _ensure_loaded() -> None:
    """首次使用时把各按天日志文件尾部回填进内存（进程内只加载一次）。"""
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        files = sorted(_log_dir().glob("third-party-*.jsonl"))
        remaining = _LOAD_TAIL_BYTES
        chunks: list[list[str]] = []
        for path in reversed(files):  # 最新文件优先，保证近期日志可查
            if remaining <= 0:
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= 0:
                continue
            chunks.append(_read_tail(path, remaining))
            remaining -= min(size, remaining)
        for lines in reversed(chunks):  # 时间正序回填，最新自然排到缓冲末尾
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    _entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        _loaded = True


def _cleanup_expired() -> None:
    """按日志 ts（文件名日期）删除超过 storage_keep_hours 的按天文件。

    与 access_log 同口径（keep_hours 向上取整到天），保证第三方日志
    保留时长与请求日志一致，不无限累积。
    """
    keep_hours = max(settings.storage_keep_hours, 1)
    keep_days = max(1, math.ceil(keep_hours / 24))
    cutoff = datetime.now(UTC).date() - timedelta(days=keep_days)
    for candidate in _log_dir().glob("third-party-*.jsonl"):
        m = _DAILY_FILE_RE.match(candidate.name)
        if m is None:
            continue
        try:
            if datetime.strptime(m.group(1), "%Y-%m-%d").date() < cutoff:
                candidate.unlink()
        except OSError:
            continue


def record(
    message: str,
    level: str,
    *,
    endpoint: str,
    url: str,
    **extra: Any,
) -> None:
    """追加一条第三方调用日志：内存缓冲 + 当日 JSONL 文件。

    写入失败只记 warning，不影响下游调用与响应（防御：日志不阻断业务）。
    周期性（每 _CLEAN_CHECK_INTERVAL 次）按 storage_keep_hours 清理过期文件。
    """
    global _write_count
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "level": level,
        "message": message,
        "endpoint": endpoint,
        "url": url,
        **extra,
    }
    _ensure_loaded()
    day = entry["ts"][:10]
    path = _daily_file(date.fromisoformat(day))
    with _lock:
        _entries.append(entry)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            # 序列化失败（extra 含不可序列化值）与写盘失败同样不阻断业务
            log.warning("third_party_log_write_failed", extra={"error": str(exc)})
        _write_count += 1
        if _write_count % _CLEAN_CHECK_INTERVAL == 0:
            _cleanup_expired()


def query(
    *,
    limit: int = 200,
    offset: int = 0,
    message: str | None = None,
    endpoint: str | None = None,
    status: int | None = None,
    status_min: int | None = None,
    status_max: int | None = None,
    level: str | None = None,
) -> dict[str, Any]:
    """查询第三方调用日志，按时间倒序（最新在前）。

    支持按事件（third_party_request/response/error）、endpoint、状态码
    （精确或范围）、级别过滤，返回分页结果。
    """
    _ensure_loaded()
    needle_message = message.strip().lower() if message else None
    needle_endpoint = endpoint.strip().lower() if endpoint else None
    needle_level = level.strip().lower() if level else None
    items = list(_entries)
    items.reverse()
    if needle_message:
        items = [e for e in items if needle_message in str(e.get("message", "")).lower()]
    if needle_endpoint:
        items = [e for e in items if needle_endpoint in str(e.get("endpoint", "")).lower()]
    if status is not None:
        items = [e for e in items if e.get("status_code") == status]
    if status_min is not None:
        # 范围过滤只匹配有状态码的记录（request/error 事件无状态码不参与）
        items = [
            e
            for e in items
            if e.get("status_code") is not None and e["status_code"] >= status_min
        ]
    if status_max is not None:
        items = [
            e
            for e in items
            if e.get("status_code") is not None and e["status_code"] <= status_max
        ]
    if needle_level:
        items = [e for e in items if needle_level in str(e.get("level", "")).lower()]
    total = len(items)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items[offset : offset + limit],
    }
