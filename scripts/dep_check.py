"""dep_check.py — 包级依赖图 + 循环检测（架构重构各阶段复测用）。
用法：python dep_check.py [--assert-rules]
"""
import ast
import os
import sys
from collections import defaultdict

deps = defaultdict(set)

def mod_name(path: str) -> str:
    m = path[:-3].replace("/", ".").replace(".__init__", "")
    return m

for root, dirs, fs in os.walk("app"):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in fs:
        if not f.endswith(".py"):
            continue
        path = os.path.join(root, f)
        m = mod_name(path)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("app"):
                        deps[m].add(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    # 相对 import：module 为相对路径名（如 submission），
                    # 按 level 回溯包层级后拼出绝对模块名
                    base = m.split(".")
                    pkg = base if f == "__init__.py" else base[:-1]
                    for _ in range(node.level - 1):
                        pkg = pkg[:-1]
                    full = ".".join(pkg + ([node.module] if node.module else []))
                    deps[m].add(full)
                elif node.module and node.module.startswith("app"):
                    deps[m].add(node.module)

def pkg_of(m: str) -> str:
    parts = m.split(".")
    return ".".join(parts[:3]) if len(parts) >= 3 else m

pkg_deps = defaultdict(set)
for m, targets in deps.items():
    src = pkg_of(m)
    for t in targets:
        if pkg_of(t) != src:
            pkg_deps[src].add(pkg_of(t))

print("=== 包级依赖 ===")
for src in sorted(pkg_deps):
    print(f"{src} → {sorted(pkg_deps[src])}")

bidir = [
    (a, b)
    for a, ts in pkg_deps.items()
    for b in ts
    if b in pkg_deps and a in pkg_deps[b] and a < b
]
print(f"\n双向依赖: {bidir if bidir else '无'}")

# 函数级 lazy import 检查（app.api → app.main）
api_main = [
    m for m, ts in deps.items()
    if m.startswith("app.api") and any(t.startswith("app.main") for t in ts)
]
orders_skills = [
    m for m, ts in deps.items()
    if m.startswith("app.orders") and any(t.startswith("app.skills") for t in ts)
]
print(f"api → main 引用: {api_main or '无'}")
print(f"orders → skills 引用: {orders_skills or '无'}")

if "--assert-rules" in sys.argv:
    # 分阶段断言（渐进重构，docs/架构重构计划.md §6）：
    # --phase 2（P1 完成）：零双向依赖 + api 无 main；
    # --phase 4（P3 完成）：追加 orders 无 skills。
    args = sys.argv
    phase = int(args[args.index("--phase") + 1]) if "--phase" in args else 99
    failures = []
    if bidir:
        failures.append(f"存在双向依赖: {bidir}")
    if api_main:
        failures.append(f"api 层仍引用 app.main: {api_main}")
    if orders_skills:
        msg = f"orders 域仍引用 skills: {orders_skills}"
        if phase >= 4:
            failures.append(msg)
        else:
            print(f"[PENDING P3] {msg}")
    if failures:
        print("\n[FAIL] 架构规则违反：")
        for f_ in failures:
            print(f"  - {f_}")
        sys.exit(1)
    print("\n[PASS] 架构规则全部满足")
