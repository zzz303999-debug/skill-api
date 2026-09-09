#!/usr/bin/env python
"""fee_registry 存量费目 → 目标账号 owner 槽迁移工具（2026-09-08 费目 owner 化）。

背景：费目隔离上线后，旧版全局 registry 条目（含模板外动态码，如 test1 →
xb444ac06）迁入 _LEGACY_OWNER 槽、不再参与判定。若某账号在 TMS 已有同名档案
（旧版全局语义下由该账号建档）却未在系统侧登记归属，其首传自举会撞自己名下
档案 → 204 → exists_external → 费用永久降级（静默丢费，响应仍 200）。

本工具把存量码「认领」到指定账号的 owner 槽（price_id 原样复制，非新建档），
该账号首传即直接命中既有档案。仅涉及有存量档案的账号（新账号/新费目全程
动态自举，无需任何预置）。

用法（registry 直写，推荐——storage 为挂载卷，改文件即生效，无二次发版）：
    python scripts/migrate_fee_registry_owners.py <owner_key> [registry.json]
        --to-owner [--apply]      # 存量码复制进该 owner 槽（缺省 dry-run）
        [--force]                 # 覆盖该 owner 槽的无 id 终态记录（tombstone）

用法（YAML 预置，可选双保险——config 随镜像分发，需提交发版）：
    python scripts/migrate_fee_registry_owners.py <owner_key> [registry.json]
        --yaml config/fee_price_map.prod.yaml [--apply]

- owner_key：python scripts/fee_owner_key.py <sk> 的输出（16 位 hex）
- registry 路径缺省 ./storage/fee_registry.json；兼容新旧两种文件形态：
  旧扁平 {code: rec}（升级前）与嵌套 {code: {owner: rec}}（升级后，取 _legacy 槽）
- **--to-owner --apply 必须在目标代码（owner 化版本）已运行后执行**：旧代码
  读不懂嵌套格式（price_id 全 null → 费用静默降级），先升级后认领
- **tombstone 冲突**：目标 owner 槽已有「无 price_id 终态记录」（撞名 204 的
  exists_external）时缺省跳过认领并明示清单——说明该账号已先于迁移触发过自举
  且撞名，需人工在 TMS 确认价格后加 `--force` 覆盖认领（正常已登记的有效
  price_id 记录永不覆盖，`--force` 亦不影响）；上线窗口内务必先认领后放流量
- 纯标准库实现（json 原子写），服务器宿主机/容器内任意 python3 可跑；
  **兼容 python 3.6**（服务器宿主可能无 3.8+：禁用 walrus/内置泛型注解/| 联合）
"""

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 与 app.orders.bill.fee_registry 同款常量（脚本纯标准库，不 import app）
_LEGACY_OWNER = "_legacy"
_RECORD_KEYS = {"price_id", "tms_name", "created_at", "status"}
_DEFAULT_REGISTRY = Path("./storage/fee_registry.json")
_CODE_LINE_RE = re.compile(r"^ {4}([A-Za-z0-9_]+):\s*(\d+)")


def _usage() -> str:
    return (
        "用法: python scripts/migrate_fee_registry_owners.py <owner_key> [registry.json] "
        "[--to-owner | --yaml 目标.yaml] [--apply]（详见脚本 docstring）"
    )


def _parse_args(argv: List[str]) -> Tuple[str, Path, str, Optional[Path], bool, bool]:
    """解析参数：位置 owner_key [registry]；模式 --to-owner / --yaml；--apply；--force。"""
    positional = []
    mode = "yaml"  # 缺省兼容旧用法（--yaml 片段输出）
    yaml_path = None
    apply = False
    force = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--yaml":
            i += 1
            if i >= len(argv):
                raise SystemExit("--yaml 缺少路径参数")
            yaml_path = Path(argv[i])
            mode = "yaml"
        elif arg == "--to-owner":
            mode = "registry"
        elif arg == "--apply":
            apply = True
        elif arg == "--force":
            force = True
        elif arg.startswith("-"):
            raise SystemExit("未知参数: " + arg)
        else:
            positional.append(arg)
        i += 1
    if len(positional) not in (1, 2):
        raise SystemExit(_usage())
    owner_key = str(positional[0]).strip()
    if len(owner_key) != 16 or not all(c in "0123456789abcdef" for c in owner_key):
        raise SystemExit(
            "owner_key 应为 16 位 hex（python scripts/fee_owner_key.py <sk>）: " + owner_key
        )
    registry_path = Path(positional[1]) if len(positional) == 2 else _DEFAULT_REGISTRY
    if mode == "yaml" and yaml_path is None:
        if apply:
            raise SystemExit("--apply 需要 --yaml 指定目标文件")
        # 无 --yaml 且非 --to-owner：打印可粘贴 YAML 片段（旧用法 dry-run）
        mode = "print"
    return owner_key, registry_path, mode, yaml_path, apply, force


def _load_raw(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("registry 读取失败: %s (%s)" % (path, exc)) from None
    if not isinstance(raw, dict):
        raise SystemExit("registry 非 dict: %s" % path)
    return raw


def _flat_codes(raw: dict) -> Dict[str, dict]:
    """存量码归一：扁平全体 / 嵌套 _legacy 槽 → {code: rec}。"""
    codes = {}
    for code, val in raw.items():
        if not isinstance(val, dict):
            continue
        if any(k in _RECORD_KEYS for k in val):
            codes[str(code)] = val  # 旧扁平全局条目 → 存量
        else:
            legacy = val.get(_LEGACY_OWNER)
            if isinstance(legacy, dict):
                codes[str(code)] = legacy
    return codes


def _record_for_copy(rec: dict) -> Optional[dict]:
    """可认领记录（有可用 price_id）；无 id（撞名终态等）→ None。"""
    price_id = rec.get("price_id")
    if isinstance(price_id, int) and not isinstance(price_id, bool):
        num = price_id
    elif isinstance(price_id, str) and price_id.isdigit():
        num = int(price_id)
    else:
        return None
    return {
        "price_id": num,
        "tms_name": str(rec.get("tms_name") or "") or None,
        "created_at": str(rec.get("created_at") or "") or None,
        "status": rec.get("status"),
    }


def _is_flat(raw: dict) -> bool:
    """顶层 value 直接含记录键 → 旧扁平形态。"""
    for val in raw.values():
        if isinstance(val, dict) and any(k in _RECORD_KEYS for k in val):
            return True
    return False


def _atomic_write(path: Path, data: dict) -> None:
    """原子写：临时文件 + replace（与 app 侧持久化同款）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _migrate_to_owner(path: Path, owner_key: str, apply: bool, force: bool) -> int:
    """存量码 → 指定 owner 槽（registry 直写）。返回复制条数；dry-run 不落盘。

    tombstone（owner 槽已有无 price_id 记录，如撞名 204 终态）缺省跳过并列出，
    需 --force 才覆盖认领；有效 price_id 的正常登记永不覆盖。
    """
    raw = _load_raw(path)
    flat = _is_flat(raw)
    codes = _flat_codes(raw)
    if not codes:
        raise SystemExit("registry 无存量码: %s" % path)

    claimed = []
    skipped = []
    existing = []
    conflicts = []
    for code in sorted(codes):
        rec = codes[code]
        if not flat:
            owners = raw.get(code)
            slot = owners.get(owner_key) if isinstance(owners, dict) else None
            if isinstance(slot, dict):
                if slot.get("price_id") is not None:
                    existing.append(code)
                    continue  # 该 owner 槽已登记有效 price_id → 跳过，永不覆盖
                if not force:
                    conflicts.append((code, str(rec.get("tms_name") or "")))
                    continue  # tombstone（撞名 204 终态无 id）→ 需 --force 覆盖认领
        copy = _record_for_copy(rec)
        if copy is None:
            skipped.append((code, str(rec.get("tms_name") or "")))
        else:
            claimed.append((code, copy))

    verb = "将复制" if not apply else "已复制"
    print('%s %d 码到 owner 槽 "%s"（registry: %s）:' % (verb, len(claimed), owner_key, path))
    for code, copy in claimed:
        name = copy["tms_name"] or ""
        print("  + %s: %s%s" % (code, copy["price_id"], "   # " + name if name else ""))
    if existing:
        print("\n%d 码该 owner 槽已登记（price_id 有效，跳过不覆盖）：" % len(existing))
        print("  ~ %s" % ", ".join(existing))
    if conflicts:
        print(
            "\n%d 码该 owner 槽已有无 price_id 终态记录（撞名 204 tombstone，"
            "跳过认领）：" % len(conflicts)
        )
        for code, name in conflicts:
            print("  ! %s  %s" % (code, name))
        print("    该账号已先于迁移触发自举且撞名 → 费用持续降级；人工在 TMS 查价后")
        print("    加 --force 覆盖认领")
    if skipped:
        print("\n%d 码无可用 price_id 未认领（需人工在 TMS 查询补值）：" % len(skipped))
        for code, name in skipped:
            print("#   %s  %s" % (code, name))
    if not apply:
        print("\n（dry-run：加 --apply 落盘）")
        return 0

    if flat:
        # 扁平源：整表转为嵌套（存量进 _legacy 留档 + 目标 owner 认领）
        nested = {}
        for code in sorted(codes):
            nested[code] = {_LEGACY_OWNER: codes[code]}
        raw = nested
    for code, copy in claimed:
        owners = raw.setdefault(code, {})
        exist = owners.get(owner_key)
        if not (isinstance(exist, dict) and exist.get("price_id") is not None):
            owners[owner_key] = copy
    _atomic_write(path, raw)
    print("已写回 %s（%s，原 owner 记录保留）" % (path, "扁平→嵌套迁移" if flat else "嵌套增量"))
    return len(claimed)


def _owner_section_bounds(lines: List[str], owner_key: str) -> Tuple[int, int]:
    start = None
    for idx, line in enumerate(lines):
        if line.rstrip("\n") == '  "%s":' % owner_key:
            start = idx
            break
    if start is None:
        raise SystemExit(
            'YAML 中不存在 owner_price_ids 段键 "%s"——先在目标文件建段' % owner_key
        )
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(lines[idx]) - len(lines[idx].lstrip()) < 4:
            end = idx
            break
    return start, end


def _merge_into_yaml(path: Path, owner_key: str, insertions: List[str]) -> List[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    _, end = _owner_section_bounds(lines, owner_key)
    existing = set()
    for line in lines:
        match = _CODE_LINE_RE.match(line)
        if match:
            existing.add(match.group(1))
    added = []
    for line in insertions:
        match = _CODE_LINE_RE.match(line)
        if match and match.group(1) not in existing:
            added.append(line)
    if added:
        newline = "\n" if not text.endswith("\n") else ""
        block = "".join(line + "\n" for line in added)
        lines.insert(end, block)
        if newline:
            lines.insert(end, newline)
        path.write_text("".join(lines), encoding="utf-8")
    return added


def _build_insertions(codes: Dict[str, dict]) -> Tuple[List[str], List[Tuple[str, str]]]:
    lines = []
    skipped = []
    for code in sorted(codes):
        rec = codes[code]
        price_id = rec.get("price_id")
        tms_name = str(rec.get("tms_name") or "") or ""
        comment = "   # " + tms_name if tms_name else ""
        if isinstance(price_id, int) and not isinstance(price_id, bool):
            lines.append("    %s: %d%s" % (code, price_id, comment))
        elif isinstance(price_id, str) and price_id.isdigit():
            lines.append("    %s: %d%s" % (code, int(price_id), comment))
        else:
            skipped.append((code, tms_name))
    return lines, skipped


def main() -> int:
    try:
        owner_key, registry_path, mode, yaml_path, apply, force = _parse_args(sys.argv[1:])
    except SystemExit:
        print(_usage(), file=sys.stderr)
        return 2
    if not registry_path.exists():
        raise SystemExit(
            "registry 文件不存在: %s（缺省 ./storage/fee_registry.json，可显式传生产拷贝路径）"
            % registry_path
        )

    if mode == "registry":
        _migrate_to_owner(registry_path, owner_key, apply, force)
        return 0

    # yaml / print 模式（保留：YAML 预置作为可选双保险）
    codes = _flat_codes(_load_raw(registry_path))
    if not codes:
        raise SystemExit("registry 无存量码: %s" % registry_path)
    insertions, skipped = _build_insertions(codes)
    if mode == "print":
        print('\n'.join(['  "%s":' % owner_key] + insertions))
        print(
            "\n# 共 %d 码；%d 码无可用 price_id 未输出（需人工在 TMS 查询补值）："
            % (len(codes), len(skipped))
        )
        for code, name in skipped:
            print("#   %s  %s" % (code, name))
        return 0

    if not yaml_path.exists():
        raise SystemExit("YAML 文件不存在: %s" % yaml_path)
    if apply:
        added = _merge_into_yaml(yaml_path, owner_key, insertions)
        print('已写入 %s：段 "%s" 新增 %d 码' % (yaml_path, owner_key, len(added)))
        for line in added:
            print("  + " + line.strip())
        print(
            "\n共 %d 存量码；%d 码无可用 price_id 未预置（需人工在 TMS 查询补值）："
            % (len(codes), len(skipped))
        )
        for code, name in skipped:
            print("#   %s  %s" % (code, name))
        if not added:
            print("（段内已全覆盖，无新增）")
    else:
        text = yaml_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        try:
            _owner_section_bounds(lines, owner_key)
        except SystemExit as exc:
            raise SystemExit(str(exc)) from None
        existing = set()
        for line in lines:
            m = _CODE_LINE_RE.match(line)
            if m:
                existing.add(m.group(1))
        missing = []
        for line in insertions:
            m = _CODE_LINE_RE.match(line)
            if m and m.group(1) not in existing:
                missing.append(line)
        print(
            'dry-run：%s 段 "%s" 将新增 %d 码（--apply 生效）：'
            % (yaml_path, owner_key, len(missing))
        )
        for line in missing:
            print("  + " + line.strip())
        print(
            "\n共 %d 存量码；%d 码无可用 price_id（需人工在 TMS 查询补值）："
            % (len(codes), len(skipped))
        )
        for code, name in skipped:
            print("#   %s  %s" % (code, name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
