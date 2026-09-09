# master_data 建档编排简化方案（M1+M3+M4）

> 状态：**待确认**（2026-09-09 首版；M1c 边界与 M5 归属已用户拍板）
> 范围：`app/orders/bill/master_data/`（orchestrator.py / store.py / keys.py）
> 性质：结构性简化，**行为零变更**（报告键集/键序、store 落盘结构、日志事件逐项不变）
> 背景：编排大脑 orchestrator 497 行，`_create_one_async` 160 行 + 12 参数为复杂度
> 热点。M2（建档 outcome 模型化）/ S1（JSON store 骨架横向收敛）另开，不入本轮；
> **M5（车辆建档 duplicate 漏登记终态修复）已拍板另开：单独提交、单独加测试，
> 不混入 M1c（M1c 保持纯重构；M5 已实施于 2026-09-09，见 §5）**。

---

## 0. 明确不动（业务拍板复杂度保留）

- 阈值语义（N 默认 5、未达阈值不阻塞 + 订单标注）与 owner 隔离（preview 无 sk → None）
- 依赖序串行（客户 → 工厂；车辆 → 司机）与工厂依赖委托止血（2026-09-03）
- T27b 三态（success / duplicate→exists_external / failed）+ no_id_created +
  skip_archive 终态语义（无车牌/无手机号司机实证、factory 无 client_id 实证）
- `_client_record/_client_finalized/_factory_dependency_candidate/_annotate_pending/
  _pending_top` 语义与注释

---

## 1. 改动清单

### M1a · `_ArchiveSession` 建档会话收集器（orchestrator.py 新增，模块私有）

**现状**：`attempted/archived/failed/exists_external` 四个收集器 + `store/owner/sk`
以 12 参数贯穿 `run_master_data_async` → `_create_one_async`（可变引用传进传出），
主循环与 driver 子流程**双处维护同一 attempted**（L456-458 与 L325-329）。

**改法**：新增会话类（放 orchestrator.py，仅建档编排用）：

```python
class _ArchiveSession:
    """单批建档会话：收集器（attempted/archived/failed/exists_external）与
    store/owner/sk 运行上下文收口（2026-09-09 M1a 抽取）。"""

    def __init__(self, store, owner: str, sk: str):
        self.store = store
        self.owner = owner
        self.sk = sk
        self.attempted: set[tuple[str, str]] = set()
        self.archived: dict[tuple[str, str], dict[str, Any]] = {}
        self.failed: list[dict[str, Any]] = []
        self.exists_external: list[dict[str, Any]] = []

    def mark_attempted(self, kind: str, key: str) -> None:
        """登记本批已尝试键（成功失败均不重试）。"""
        self.attempted.add((kind, key))

    def record_archive(self, kind: str, key: str, display: str, archive_id: str) -> None:
        self.archived[(kind, key)] = {"display": display, "archive_id": archive_id}

    def record_fail(self, kind: str, key: str, display: str, reason: str) -> None:
        self.failed.append({"kind": kind, "key": key, "display": display, "reason": reason})

    def record_exists_external(self, kind: str, key: str, display: str, message: str) -> None:
        self.exists_external.append(
            {"kind": kind, "key": key, "display": display, "message": message}
        )
```

`_create_one_async` 签名收敛：`(kind, candidate, session)`（12 参数 → 3）；
rec 在函数内 `session.store.get(...)` 重取（内存读，微秒级，主循环已判过终态/阈值）。

### M1b · 车辆前置建档子流程抽取

**现状**：driver 分支内嵌 35 行车辆编排（L319-352：查车 → 未建则建 → 登记）。

**改法**：抽 `_ensure_truck_archive_async(candidate, session) -> str`（返回
truck_archive_id，无则空串），driver 分支调用点两行替换；子流程内
`attempted/archived/failed` 全部走 session 方法（原 L325-329 手工同步消除）。

**等价性论证**：车辆建档仅发生在 driver 候选建档路径（run 主循环不单独建车），
抽取后调用点与顺序不变；truck 建档失败仅进 failed 不阻塞 driver（truck_id 可空）
语义保留。

### M1c · 终态判定收口（store.py + orchestrator.py）

**现状**：终态判定 `archive_id or exists_external or skip_archive` 在 run 主循环
L460-461 写一遍；`_client_finalized`（L162-169）内联同款（不含 skip_archive——
客户无 skip 场景，语义等价）。

**改法**：
1. store.py 新增 `def is_final(self, kind: str, key: str, owner: str) -> bool`：
   记录存在且含任一终态键（archive_id / exists_external / skip_archive）→ True；
2. run 主循环 L460 与 driver 分支 truck 判断改用：
   - 键终态（跳过建档）：`store.is_final(kind, key, owner)`；
   - 车辆"已建过车"（L323 `truck_rec.get("archive_id")`）语义是 **archive_id 专属**
     不回退 exists_external——**保持原判，不换 is_final**。车辆 exists_external 时
     跨批仍会重发建车请求（现状行为，每批一次注定被拒请求），该问题已拍板另开
     **M5 独立修复（单独提交+测试），不借 M1c 修行为**（2026-09-09 用户拍板）。

**注意**：`_client_finalized` 保持 `archive_id or exists_external`（客户终态
不含 skip_archive——客户从不 mark_skip_archive，语义等价）；工厂 no_client_id
被拒 → skip_archive 后主循环由 is_final 拦下（与现状 L460-461 一致）。

### M3 · keys.py 死变量删除

**现状**：L15 `_FULL_WIDTH_RE = re.compile(r"[\uFF01-\uFF5E]")` 定义后零引用
（`_to_half_width` 用 ord 手动判断）→ 删定义（1 行）。

### M4 · 编排内小重复 + docstring 流水精简

1. `run_master_data_async`：`dict(Counter(c.kind for c in candidates))` 在
   candidates/report 构造（L418）与 incremented（L443-445）两处同批计算 →
   构造时提一次局部量，两处复用（报告键集/键序不变）；
2. docstring 流水精简（保留 T27b/依赖委托/实证等因果）：
   - `_create_one_async` docstring 中"2026-09 异步化改造后为生产唯一入口：建档调用
     走 await create_archives_fn（async 版）；依赖前置/终态登记/报告语义逐行一致
     （司机前置建车同样 await）"→ 并入 M1 说明（同步改造已消除双实现），保留
     三态/终态语义描述；
   - `run_master_data_async` docstring"建档段走 create_archives_async，其余逻辑
     逐行一致"流水句删除；
   - 行内"# ---- 异步实现（2026-09 异步化改造后为生产唯一入口；网络段走
     create_archives_async）----"分节注释简化。

---

## 2. 行为零变更核对点（改后逐项对照）

| 项 | 保持依据 |
|---|---|
| 报告键集/键序（archived 元素含 display/archive_id；failed/exists_external 元素结构） | session 方法内结构 = 原 append 结构 |
| attempted 双处维护收敛为一处 | session.attempted 唯一事实（主循环 mark_attempted + driver 子流程同 session） |
| 车辆建档时机/次数 | _ensure_truck 内 mark_attempted 保留（原 L328 语义） |
| store 落盘结构/键序 | 未动 store 写路径 |
| 阈值/终态/依赖判定顺序 | 主循环判定序不变（attempted → 终态 → 阈值 → 端点 → factory 委托 → create_one） |
| 日志事件 | 不改日志（driver skip 等 warning/info 原位保留） |

## 3. 验证计划

| 项 | 命令 | 预期 |
|---|---|---|
| master_data 专测 | `uv run pytest tests/orders/bill/test_master_data.py tests/orders/bill/test_master_data_config.py tests/orders/bill/test_master_data_http.py -q` | 全绿（与基线一致） |
| bill 域 | `uv run pytest tests/orders/bill/ -q` | 552 passed |
| 全量回归 | `uv run pytest tests/ -q` | 1186 passed, 11 skipped |
| 静态门禁 | `uv run ruff check app/ tests/` + `uv run python scripts/dep_check.py --assert-rules` | 全绿 |

## 4. 提交（一次提交，含文档）

```
refactor(bill): master_data 建档编排简化——_ArchiveSession 收口 + 终态判定 + 清理

- M1a: _ArchiveSession 会话收集器（attempted/archived/failed/exists_external +
  store/owner/sk 收口），_create_one_async 参数 12 → 3，消除双处维护 attempted
- M1b: 车辆前置建档抽 _ensure_truck_archive_async（driver 分支 -35 行内嵌）
- M1c: store.is_final 终态判定收口（archive_id/exists_external/skip_archive）
- M3: keys.py 删 _FULL_WIDTH_RE 死变量
- M4: Counter 同批重复计算提一次；docstring 异步化改造流水精简
- 门禁：master_data 专测 + bill 552 + 全量 1186 / ruff / dep_check 全绿
```

## 5. 后续项

### M5 · 车辆建档 duplicate 漏登记终态——跨批重发建车请求修复（**已拍板另开**：单独提交 + 单独加测试，不混 M1c）

**问题（M1b 执行实证校准，2026-09-09）**：truck 建档分支只处理 success/error
两态——TMS"已存在"拒单（duplicate 标记，client 归一化同主路径）**只进 failed、
不登记 exists_external**（无 mark_exists_external 调用，原 driver 内嵌段亦如此）
→ 存量车牌每批建档被拒 → failed 跨批重发注定被拒的建车请求（attempted 仅单批
防重）。与客户侧 2026-09-03 修复同源，车辆→司机这对漏修——但机制为 duplicate
**未登记终态**（非"已登记但判定漏认"：truck 路径本无登记）；主建档路径
_create_one_async 三态一致处理，truck 分支漏登记。

**改法**（`_ensure_truck_archive_async` 两处，对齐主路径三态）：

1. 结果登记：响应带 duplicate/no_id_created → `mark_exists_external` 登记 +
   进报告 exists_external 段（与 failed 区分，message 取 error.message 兜底"已存在"）；
2. 预检判定：truck_rec 认 exists_external（无 id → 返回空串——truck_id 可空，
   司机照常建档；archive_id 仍优先复用）。

**效果**：存量车牌首批 duplicate → 登记终态 → 后续批次不再重发建车请求；司机
不阻塞；成功路径（archive_id 复用）零变化。

**测试（新增 1 例）**："truck duplicate 登记 exists_external 后第二批不重发"——
首批建车返回 duplicate → exists_external 单列（failed 不含 truck）+ store 标记；
司机照常建档成功（truck_id 空）；第二批建车请求计数仍为 1。

**提交**：单独 commit（不带 M1-M4 文件）；建议信息：

```
fix(bill): 车辆建档 duplicate 漏登记终态——跨批重发建车请求修复（M5）
```

### 待议项（不入本轮）

- **M2**：建档 outcome 模型化（ArchiveOutcome pydantic，client/orchestrator/
  fee_bootstrap 8 处 `.get()` 消费点收口，仿 R5 流程）
- **S1**：JSON store 骨架横向共享（fee_registry/master_data_store/imported_registry/
  template_store/access_log 同款持久化 → core 基类，独立大案）
