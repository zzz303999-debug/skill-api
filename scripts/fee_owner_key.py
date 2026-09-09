#!/usr/bin/env python
"""sk → owner_key：费目/档案 owner 槽键计算工具（纯标准库，无项目依赖）。

费目 per-owner 隔离（2026-09-08 拍板）后，owner_key = sha256(sk) 前 16 hex
（与 imported_registry/master_data_store/fee_registry 同款口径）。预置或核对
某账号槽位前需先算其 owner_key。用法：

    python scripts/fee_owner_key.py <sk>

sk 不落盘、不进日志（仅本进程内计算输出）。
"""

from __future__ import annotations

import hashlib
import sys


def owner_key(sk: str) -> str:
    """sk → 去重/建档维度键：sha256 前 16 hex（注册表不落盘 token 原文）。"""
    return hashlib.sha256((sk or "").encode("utf-8")).hexdigest()[:16]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    print(owner_key(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
