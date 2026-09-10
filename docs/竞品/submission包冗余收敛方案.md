# submission 包冗余收敛方案（S1-S4 已实施）

> 状态：已实施（2026-09-09）
> 范围：`app/orders/bill/submission/`（client / payload / imported_registry）
> 性质：结构性收敛，**行为零变更**（错误文案/日志键/表单键值逐字保持）
> 背景：勘察发现 4 项包内冗余 + 2 项跨文件候选（另开），勘察报告经用户审定后执行

---

## §1 包内冗余（已实施）

### S1 · payload 双名一实现合并（payload.py 270 → 262 行）

**现状**：`build_order_payload` 纯透传 `build_order_form`（一行 return，
docstring 称"完整表单"名不副实）；消费者 18 处走 payload 名、仅
test_payload 10 处直测 form 名。

**改法**：实现合并保留公开名 `build_order_payload`（引用面大），删
`build_order_form`，test_payload import/调用改指；模块 docstring 同步。

**风险**：低（纯改名合并，测试全量覆盖同实现）。

### S2 · 双响应判定合并（client.py `_parse_create_response` / `_parse_canonical_response` → `_parse_downstream_response`）

**现状**：两函数四错误分支（HTTP≥400 / 非 JSON / 非对象 / code≠200）逐段
同构 ~45 行，差异仅错误文案前缀（step vs "order API"）、日志键
（jxt_order_* vs jxt_canonical_order_*）、成功取 o_id；两者外部 0 引用。

**改法**：合并为 `_parse_downstream_response(response, *, step, log_tag,
want_o_id=False)`——step 化文案逐字等价（canonical 传 step="order API"）；
日志键 f"{log_tag}_rejected/_ok"；want_o_id 分支保真 o_id 提取与日志
extra（AddWork 原带 step 键、canonical 不带，分叉保持逐字一致）。

**风险**：低-中（文案/日志键有测试与监控断言，改后全量回归锁定）。

### S3 · 双批量编排骨架抽（`_run_create_batch`）

**现状**：`create_orders_async` 与 `create_canonical_orders_async` 仅
submit 回调不同，owner/批闸 acquire+503/gather/finally 释放 ~20 行×2
同构（核心 `_create_one_async` 已共享）。

**改法**：抽 `_run_create_batch(orders, *, sk, submit, source_sha256)`，
两入口薄封装（submit lambda 各自注入）。

**风险**：低（逐字搬移 + 语义等价）。

### S4 · client.py 超线拆分（575 → 375 行）——AddWork 表单段独立 `addwork_form.py`

**现状**：S1-S3 后 client.py 548 行仍超纪律线 500；AddWork 键集常量+展平
（_FIXED_FIELDS/_TOP_FIXED_KEYS/_DATA_KEYS/_DRIVER_KEYS/_SHOU_KEYS +
_put_value/flatten_order/build_add_work_form ≈190 行）与 payload.py
（canonical 表单）职责对仗但混在 client。

**改法**：新建 `submission/addwork_form.py`（186 行，与 payload.py 对仗）：
表单键集常量与三函数原样搬移 + 模块 docstring（PHP 硬读键名/note 下沉等
实证口径随迁）；client.py 删段改引用（`json` import 随迁清理）；测试
import 改指（test_client 多行 import 块）。外部 app 零引用已实证。

**风险**：低（纯搬移；form 输出逐字不变——test_client 8 处 flatten 断言
全量锁定）。

---

## §2 门禁（每步全绿）

1. bill 域 554 passed（与基线一致）
2. 全量 1188 passed + 11 skipped
3. ruff + dep_check --assert-rules

## §3 跨文件候选（另开，本轮不做）

- **C1**：三类注册表骨架重复（ImportedOrderRegistry 256 / FeeRegistry 204 /
  MasterDataStore 209）——registry_common（38 行）只收口工具函数，未抽类
  骨架；建议抽 `SingleFileJsonRegistry` 基类、imported_registry 先行试水
- **C2**：manifest/submission（441 行）与 bill/submission 跨域同构——Q5
  拍板另开；bill 本番收敛后两域趋同（manifest 已单判定单入口形态）
- **不动**：费用发射双实现（client flatten shou 段 vs payload._emit_fees）——
  双通道各自 live 实证键位怪癖，合并中风险低收益
