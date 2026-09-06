"""orders 域注册表公共工具（P6 抽取；不合并两侧锁模型）。

bill（asyncio 锁，per-bl_no 分段）与 manifest（threading.Lock）的临界区
语义不同，保持各自实现；本模块只收敛两侧逐行一致的纯工具：
- normalize：提单号 strip + 大写规范化（注册表业务唯一键口径）
- atomic_write_json：临时文件 + os.replace 原子落盘（进程崩溃不损坏主文件）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import settings


def normalize(bl_no: str | None) -> str | None:
    """提单号规范化（与两侧原实现逐行一致）：空值原样返回，否则 strip + 大写。"""
    if not bl_no:
        return bl_no
    return str(bl_no).strip().upper()


def registry_storage_path(filename: str) -> Path:
    """注册表文件路径：{storage_dir}/{filename}（测试可 monkeypatch storage_dir）。"""
    return settings.storage_dir / filename


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON：同目录临时文件 + os.replace（进程崩溃不损坏主文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
