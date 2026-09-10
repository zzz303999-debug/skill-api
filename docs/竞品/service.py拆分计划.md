# bill/service.py 拆分计划

> 状态：**已确认**（用户 2026-09-09 审定版；P1 已完成，P2/P3 待执行，P4 视需要另开）
> 基线：service.py 665 行（15 个顶层函数）
> 纪律：行为零变更；每阶段独立提交、独立验证；单文件尽量 ≤500 行

---
可让 先按这份计划做一次「对照源码复核」，确认函数边界和引用关系无遗漏，再动手拆。
## 1. 现状

`service.py` 是 orders/bill 编排中枢（facade），职责清晰但体量超标：

| 行段 | 函数 | 职责 | 建议归属 |
|------|------|------|----------|
| 48-52 | `_sha256` | 文件指纹 | 可留 service 或 utils |
| 53-66 | `_order_dedup_parts` | 去重键段 | submission / response |
| 67-228 | 对账三件套 | 费用回填、报告 | fees/ |
| 229-335 | 两个文件级校验 | 箱型 / 提单号连坐 | validation |
| 336-381 | `_parse_stage_async` | 解析阶段编排 | parsing/ |
| 382-436 | `_aggregate_stage` | 归集阶段编排 | aggregation/ |
| 437-494 | 汇总组装 | summary / upstream | response |
| 495-665 | `build_result_async` | 总编排门面 | 保留在 service |

目标：`service.py` 回到约 200 行纯编排。

---

## 2. 分阶段计划

### P1：文件级校验下沉（最低风险）✅ 已完成（commit 见 git log）

**改动**
- 新增 `app/orders/bill/validation.py`
- 迁入：
  - `reject_unknown_box_types`（原 `_reject_unknown_box_types`）
  - `reject_missing_bl_no`（原 `_reject_missing_bl_no`）
  - `MISSING_BL_NO_MSG`（原 `_MISSING_BL_NO_MSG`）
- `service.py` 改为从 validation 引用；测试 import 同步指向（test_box_whitelist / test_dedup_integration）

**验证**
- 全量 bill 域测试：552 passed（与基线一致）
- ruff + dep_check：通过

**风险**：低（纯搬移）

---

### P2：费用对账下沉

**改动**
- 新增 `app/orders/bill/fees/reconcile.py`
- 迁入：
  - `_reconcile_order_fees`
  - `_fee_parse_failures`
  - `_build_fee_reports`
- `service.py` 改为从 fees.reconcile 引用

**验证**
- 费用相关单测 + 全量回归
- ruff + dep_check

**风险**：低-中（符号引用需同步：test_fee_bootstrap.py 直测 `_reconcile_order_fees` 的 import 需改指）

---

### P3：响应汇总下沉

**改动**
- 新增 `app/orders/bill/response_build.py`
- 迁入：
  - `_order_dedup_parts`
  - `_build_failed_details`
  - `_build_summary`
  - `_build_upstream`
- 与 `import_response.py` 形成「判定 / 组装」分工

**验证**
- 路由 / summary / 409 相关测试
- ruff + dep_check

**风险**：低（`_order_dedup_parts` 同时被 `_aggregate_stage` 引用，P4 迁移时注意依赖方向）

---

### P4：阶段编排归位（可选，收益中等）

**改动**
- `_parse_stage_async` → `parsing/` 合适入口（或 `parsing/stage.py`）
- `_aggregate_stage` → `aggregation/` 合适入口（或 `aggregation/stage.py`）
- `service.py` 只保留总编排调用

**验证**
- 全量测试
- 依赖方向复测

**风险**：中（跨包 import 较多）
**建议**：P1–P3 完成且稳定后再做

---

## 3. 明确不动

- `build_result_async` 保留在 `service.py`（facade 入口）
- 不新增架构层
- 不改对外行为 / OpenAPI
- 不在本轮处理 master_data / fee_bootstrap 内部结构

---

## 4. 验收纪律（每阶段）

1. 全量 pytest 全绿
2. ruff 通过
3. dep_check --assert-rules 通过
4. OpenAPI diff 为零
5. 阶段独立提交，完成即汇报

---

## 5. 建议执行顺序

1. 先提交当前未提交改动（owner 隔离、路由瘦身等）✅
2. 再按 **P1 → P2 → P3** 执行（P1 ✅）
3. P4 视需要另开

确认后从 P1 开始。

---

## 6. 对照源码复核记录（2026-09-09，P1 执行前）

- **AST 边界核对**：15 个顶层函数起止行号与上表一致，无遗漏函数（逐行扫描确认）。
- **函数体内 lazy import**（随迁保留原位语义）：
  - `_build_fee_reports`：`settings`、`fees.fee_bootstrap._owner_for`
  - `_reject_unknown_box_types`：`core.box_whitelist.check_unknown_box_types`（已随迁）
  - `_reject_missing_bl_no`：`submission.imported_registry.normalize`（已随迁）
  - `_parse_stage_async`：`asyncio`；`_aggregate_stage`：`payload.collect_unmapped_note`、`imported_registry`（P4 随迁）
- **外部符号引用**（P1 前共 4 处，均已在 P1 同步）：
  - test_box_whitelist.py:17（模块级）、test_dedup_integration.py:172/218、test_fee_bootstrap.py:916（P2 处理）
  - app 侧仅 bill_import.py 引用 `build_result_async`（门面不动）
- **P1 结果**：service.py 665 → 564 行；validation.py 115 行；bill 域 552 passed、ruff/dep_check 全绿。
