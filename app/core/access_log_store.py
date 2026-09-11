"""日志存储服务：请求访问日志（按天 JSONL 持久化 + 内存环形缓冲查询）。

分层定位（docs/架构说明.md §0）：存储服务独立于业务域——由 api/middleware/
access_log.py 写入、由 api/routes/meta.py 读取；本模块不含 HTTP 逻辑。

- `record()` 由 FastAPI 中间件调用，按日志时间写入当日文件
  `requests-YYYY-MM-DD.jsonl`（单日超限轮转归档为 `.1/.2/...`，编号递增不覆盖）；
- `query()` 供 `GET /api/logs` 查询，只读内存缓冲（最新在前），避免每次读文件；
- 服务重启后首次使用时回填各按天文件尾部进内存，保证近期历史可查；
- 过期按天文件按日志 ts（文件名日期）整文件删除，不依赖 mtime；
  旧版单文件格式（requests.jsonl*，文件名无日期）按 mtime 兜底清理完成迁移。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.log_support import entry_in_time_range, normalize_ts_bound
from app.core.logging_conf import get_logger

log = get_logger(__name__)

# 内存环形缓冲上限：只保留最近条目供查询，避免内存无限增长
_MAX_MEMORY_ENTRIES = 5000
# 单日日志文件超过该大小后轮转（归档为 requests-YYYY-MM-DD.jsonl.N）
_MAX_LOG_FILE_BYTES = 64 * 1024 * 1024
# 每写入多少条检查一次文件大小与过期清理，避免每次请求都 stat
_ROTATE_CHECK_INTERVAL = 1000
# 首次加载时从各文件尾部读取的总字节预算，避免读全量历史文件
_LOAD_TAIL_BYTES = 4 * 1024 * 1024
# 按天日志文件名：requests-2026-08-17.jsonl / requests-2026-08-17.jsonl.1
_DAILY_FILE_RE = re.compile(r"^requests-(\d{4}-\d{2}-\d{2})\.jsonl(?:\.(\d+))?$")

_entries: deque[dict[str, Any]] = deque(maxlen=_MAX_MEMORY_ENTRIES)
_lock = threading.Lock()
_loaded = False
_write_count = 0

# 测试可替换的日志目录；None 时使用 settings.storage_dir / logs
_request_log_dir: Path | None = None


def _log_dir() -> Path:
    if _request_log_dir is not None:
        return _request_log_dir
    return settings.storage_dir / "logs"


def _daily_file(day: date) -> Path:
    return _log_dir() / f"requests-{day.isoformat()}.jsonl"


def _day_of(ts: Any) -> date:
    """从日志时间戳（ISO 字符串）解析 UTC 日期；无时区按原样取日期，解析失败回退今天。"""
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            return dt.date()
        return dt.astimezone(UTC).date()
    except (ValueError, TypeError):
        return datetime.now(UTC).date()


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


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
        log.warning("request_log_load_failed", extra={"error": str(exc)})
        return []


def _sorted_log_files(log_dir: Path) -> list[Path]:
    """按日志时间升序返回按天日志文件（同日归档编号在前、主文件最后）。"""
    found: list[tuple[date, int, Path]] = []
    for p in log_dir.glob("requests-*.jsonl*"):
        m = _DAILY_FILE_RE.match(p.name)
        if m is None:
            continue
        day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        seg = int(m.group(2)) if m.group(2) else 10**9  # 主文件视为同日最新
        found.append((day, seg, p))
    return [p for _, _, p in sorted(found)]


def _ensure_loaded() -> None:
    """首次使用时把各按天日志文件尾部回填进内存（进程内只加载一次）。"""
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        log_dir = _log_dir()
        files = _sorted_log_files(log_dir)
        remaining = _LOAD_TAIL_BYTES
        chunks: list[list[str]] = []
        for path in reversed(files):  # 最新文件优先，保证近期日志可查
            if remaining <= 0:
                break
            size = _file_size(path)
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


def _rotate_log_file(path: Path) -> None:
    """当日文件超限时归档为 requests-YYYY-MM-DD.jsonl.N（编号递增，不覆盖旧归档）。"""
    try:
        n = 1
        while path.with_name(f"{path.name}.{n}").exists():
            n += 1
        path.rename(path.with_name(f"{path.name}.{n}"))
    except OSError as exc:
        log.warning("request_log_rotate_failed", extra={"error": str(exc)})


def _cleanup_expired(log_dir: Path) -> None:
    """按日志 ts（文件名日期）删除过期的按天文件与归档，不依赖 mtime。

    旧版单文件格式（requests.jsonl*，文件名无日期）按 mtime 兜底清理，
    保证老部署升级后旧文件不会永久残留。
    """
    keep_hours = max(settings.storage_keep_hours, 1)
    keep_days = max(1, math.ceil(keep_hours / 24))
    cutoff = datetime.now(UTC).date() - timedelta(days=keep_days)
    expiry_seconds = keep_hours * 3600
    now = time.time()
    for candidate in log_dir.glob("requests*.jsonl*"):
        try:
            m = _DAILY_FILE_RE.match(candidate.name)
            if m is not None:
                if datetime.strptime(m.group(1), "%Y-%m-%d").date() < cutoff:
                    candidate.unlink()
            elif now - candidate.stat().st_mtime > expiry_seconds:
                candidate.unlink()
        except OSError:
            continue


def _maybe_maintain(path: Path) -> None:
    """周期性（每 N 次写入）检查：当日文件超限轮转 + 过期按天文件清理。"""
    global _write_count
    _write_count += 1
    if _write_count % _ROTATE_CHECK_INTERVAL != 0:
        return
    try:
        if path.stat().st_size >= _MAX_LOG_FILE_BYTES:
            _rotate_log_file(path)
    except OSError:
        pass
    _cleanup_expired(path.parent)


def record(entry: dict[str, Any]) -> None:
    """追加一条请求日志：按日志时间写入当日 JSONL 文件 + 进内存环形缓冲。

    文件写入失败只记 warning，不影响请求本身。单日文件超过大小阈值时
    自动轮转归档（编号递增），并周期性按 storage_keep_hours 清理过期文件。
    """
    if "ts" not in entry:
        entry["ts"] = datetime.now(UTC).isoformat(timespec="milliseconds")
    # 先加载历史再追加，避免重启后首条记录在内存中重复
    _ensure_loaded()
    day = _day_of(entry["ts"])
    path = _daily_file(day)
    with _lock:
        _entries.append(entry)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("request_log_write_failed", extra={"error": str(exc)})
        _maybe_maintain(path)


def _response_code(entry: dict[str, Any]) -> str | None:
    """从审计条目的响应文本（JSON 字符串）提取业务码 code（统一外壳）。

    非 JSON / 截断后不可解析 / 无 code 字段时返回 None（不匹配任何 code 过滤）。
    """
    text = entry.get("response")
    if not isinstance(text, str) or not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and data.get("code") is not None:
        return str(data["code"])
    return None


def query(
    *,
    limit: int = 200,
    offset: int = 0,
    path: str | None = None,
    file: str | None = None,
    status: int | None = None,
    status_min: int | None = None,
    status_max: int | None = None,
    code: str | None = None,
    method: str | None = None,
    min_duration_ms: float | None = None,
    ts_from: datetime | None = None,
    ts_to: datetime | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    include_full: bool = False,
) -> dict[str, Any]:
    """查询请求日志，按时间倒序（最新在前）。

    支持按路径、文件名（子串）、HTTP 状态码（精确或范围）、统一外壳业务码
    （response.code）、方法、最短耗时、时间范围（ts_from/ts_to，naive 边界
    按 UTC 解释）、请求 ID、客户端 IP 过滤，返回分页结果。
    include_full=False（默认）时剥离 response_full 大字段，页面列表/内存
    占用最小；导出等需要完整响应体的场景传 include_full=True。
    """
    _ensure_loaded()
    needle_path = path.strip().lower() if path else None
    needle_file = file.strip().lower() if file else None
    needle_ip = ip.strip().lower() if ip else None
    needle_method = method.strip().upper() if method else None
    needle_code = code.strip() if code else None
    ts_from = normalize_ts_bound(ts_from)
    ts_to = normalize_ts_bound(ts_to)
    range_time = ts_from is not None or ts_to is not None
    items = list(_entries)  # 倒序展示
    items.reverse()
    if needle_path:
        items = [e for e in items if needle_path in str(e.get("path", "")).lower()]
    if needle_file:
        items = [e for e in items if needle_file in str(e.get("file", "")).lower()]
    if range_time:
        items = [e for e in items if entry_in_time_range(e.get("ts"), ts_from, ts_to)]
    if status is not None:
        items = [e for e in items if e.get("status") == status]
    if status_min is not None:
        items = [
            e
            for e in items
            if isinstance(e.get("status"), int) and e["status"] >= status_min
        ]
    if status_max is not None:
        items = [
            e
            for e in items
            if isinstance(e.get("status"), int) and e["status"] <= status_max
        ]
    if needle_code:
        items = [e for e in items if _response_code(e) == needle_code]
    if needle_method:
        items = [
            e
            for e in items
            if str(e.get("method", "")).upper() == needle_method
        ]
    if min_duration_ms is not None:
        items = [
            e
            for e in items
            if isinstance(e.get("duration_ms"), (int, float))
            and e["duration_ms"] >= min_duration_ms
        ]
    if request_id:
        items = [e for e in items if request_id == e.get("request_id")]
    if needle_ip:
        items = [e for e in items if needle_ip in str(e.get("ip", "")).lower()]
    total = len(items)
    page = items[offset : offset + limit]
    if not include_full:
        page = [
            {k: v for k, v in e.items() if k != "response_full"} for e in page
        ]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": page,
    }
