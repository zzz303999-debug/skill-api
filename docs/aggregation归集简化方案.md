# aggregation 归集包代码简化方案

> 状态：**待确认**（2026-09-09 首版）
> 范围：`app/orders/bill/aggregation/`（aggregator.py / canonical_aggregator.py）
> 性质：结构性简化，**行为零变更**（输出 JSON 键序/键集/数值逐字节不变）
> 背景：包内 883 行读审后，约七成复杂度为业务拍板/踩坑的必然累积（保留），
> 本方案只动五处代码层冗余。落地方式：一次提交 + 全量门禁。

---

## 0. 明确不动（复杂度保留清单）

- `_build_reconciliation` 双锚点状态机（SUM 公式缓存旧值实测坑，note 语义不可删）
- `_fees_of` 费目治理细节（import:false→excluded / other 别名 / 动态码 note / 锚点只取合计小计列）
- `to_canonical` 16 字段映射（表驱动化需动序列化省略语义，违反行为零变更）
- 全部带日期/拍板依据的注释（踩坑记录，保留）

---

## 1. 改动清单（五处，均为纯结构搬移/去重）

### ① `_order_from_row` 样板瘦身（aggregator.py，净减约 20 行）

**现状**：L367-378 连续 13 次 `_nonempty(row.X)` 局部变量；随后 L426-437 与
L440-454 两段"if value: dict[k]=value"逐字段填充（order_data 8 键 + driver 7 键）。

**改法**：新增模块级私有 helper：

```python
def _text_values(row: BillRow, *attrs: str) -> dict[str, str]:
    """非空文本字段收集（None/空白省略；键序 = attrs 顺序）。"""
    return {
        a: v
        for a in attrs
        if (v := _nonempty(getattr(row, a)))
    }
```

`_order_from_row` 内 13 个局部变量删除，改为一次收集后按段挑选：

```python
texts = _text_values(
    row, "c_title", "c_name", "c_phone", "c_sn", "factory_name",
    "factory_bei", "b_wharf", "b_get_address", "b_back_address",
    "d_name", "d_phone",
)
c_title = texts.get("c_title")          # 必填判空（missing_fields/BillOrder 共用）
```

- `order_data` 顶层段：`c_sn/c_name/c_phone/factory_name/factory_bei/b_wharf`
  从 `texts` 按既有键序取（`if k in texts`），`month`/`c_note` 保持原条件赋值
- `driver` 段：`b_get_address/b_back_address/d_name/d_phone` 从 `texts` 取，
  `b_date/d_num/get_ys_zj` 保持原条件赋值
- `d_num` 特殊处理不变：`clean_plate_no(_nonempty(row.d_num))`
- **键序保证**：dict update 的键序 = 各段元组顺序 = 既有响应键序；driver 子
  dict 基础键 `pay_yf_zj` 在前，与现状一致

**等价性论证**：`_nonempty` 语义原样（strip 后非空）；取值顺序、省略规则、
必填恒带键（order_num1/type/c_title/data/box/driver）全部不变；注释（data
收敛 1 条、必填空值省略规则）原位保留。

### ② `_collect_anchors` 拼串去重（aggregator.py，净减约 8 行）

**现状**：L90-94 与 L100-105 两段 `model_dump().values()` 过滤拼串完全同体。

**改法**：新增模块级 helper 并两处替换：

```python
def _row_text(row: BillRow) -> str:
    """整行原文拼串（尾部锚点行探测用）。"""
    return " ".join(
        str(v) for v in row.model_dump().values()
        if v is not None and str(v).strip()
    )
```

总箱型箱量段 → `matches = _BOX_ANCHOR_RE.findall(_row_text(row))`；
合计大写段 → `row_text = _row_text(row)` 后 `if "合计大写" in row_text`。
两分支本就不会同时命中同一行，行为等价。

### ③ `clean_plate_no` / `clean_group_key` 实现去重（aggregator.py）

**现状**：两函数体逐字相同（去浮点尾巴），仅 docstring/公开语义不同。

**改法**：新增模块级 helper，两函数体改为单行委托（公开名/docstring/导出
不变——canonical_aggregator 按语义 import 不受影响）：

```python
def _strip_float_tail(text: str) -> str:
    """Excel 数字单元格浮点尾巴（9486.0 → 9486）。"""
    return text.split(".")[0] if _FLOAT_TAIL_RE.match(text) else text
```

### ④ `_canonical_from_row` bl_no 展开简化（canonical_aggregator.py）

**现状**：L202 `bl_no = clean_group_key(values.get("bl_no"))`，L225 构造时
`**{k: v for k, v in values.items() if k != "bl_no"}` 排除后再展开——绕。

**改法**：先 pop 再展开：

```python
bl_no_raw = values.pop("bl_no", None)
bl_no = clean_group_key(bl_no_raw)
...
order = CanonicalOrder(
    bl_no=bl_no or bl_no_raw,   # 原 `bl_no or values.get("bl_no")` 兜底语义不变
    ...
    **values,                   # values 已无 bl_no，无需排除式展开
)
```

**等价性论证**：pop 保留剩余键序；`bl_no or bl_no_raw` 与原 `bl_no or
values.get("bl_no")` 取值相同（values.get 即被 pop 的原值）；dict 展开键集
不变（仅少一次过滤推导）。

### ⑤ `_box_type_of` / `_box_groups_of` 同源双遍历合并（canonical_aggregator.py）

**现状**：两函数各自遍历 `box_type_qty`——一个取首项箱型原文、一个逐项累加。

**改法**：合并为单次遍历，首项语义**精确保持原样**（不引入 strip 差异）：

```python
def _boxes_of(row: dict) -> tuple[list[BoxGroup], Any]:
    """行内箱聚合：逐项按箱型累加（保出现序）+ 首项箱型原文（container 用）。"""
    counts: dict[str, int] = {}
    order: list[str] = []
    items = row.get("box_type_qty")
    first = None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        first = items[0].get("type")          # 原 _box_type_of 语义：首项原文不清洗
    if isinstance(items, list):
        for item in items:
            ...（原 _box_groups_of 累加逻辑原样）
    return [BoxGroup(b_type=t, box_num=counts[t]) for t in order], first
```

- `_container_of` 签名改为 `(row, box_type)`：`box_type=box_type` 直用；
- `_canonical_from_row`：`box_groups, first_box_type = _boxes_of(row)`，
  `containers = _container_of(row, first_box_type)`；
- 删除 `_box_type_of`/`_box_groups_of`（无包外引用，grep 确认后删）。

**注意**：原 `_box_type_of` 返回 `items[0].get("type")` **原文不做 strip/空判**
（type 为空白串时也原样返回）——`first` 必须同语义，否则 container.box_type
进 payload 可能变化，故实现上 first 取在累加循环之前、不经清洗。

---

## 2. 验证计划（一次提交后执行）

| 项 | 命令 | 预期 |
|---|---|---|
| 单测（bill 域，聚合/费用/payload 全覆盖） | `uv run pytest tests/orders/bill/ -q` | 552 passed（与基线一致） |
| 集成 | `uv run pytest tests/api/ tests/orders/ -q` | 887 passed, 3 skipped |
| 全量回归 | `uv run pytest tests/ -q` | 1186 passed, 11 skipped |
| 静态门禁 | `uv run ruff check app/ tests/` + `uv run python scripts/dep_check.py --assert-rules` | 全绿 |

重点回归面：test_categorize / test_payload / test_order_full_payload /
test_route / test_dedup_integration（键序、box、driver、c_note 形状锁定）。

---

## 3. 提交

一次性提交：方案文档 + aggregator.py + canonical_aggregator.py（无测试改动，
行为零变更由既有断言兜底）。建议提交信息：

```
refactor(bill): aggregation 归集包五处样板简化——行为零变更

- aggregator: _order_from_row 非空字段样板瘦身（_text_values helper）
- aggregator: _collect_anchors 整行拼串去重（_row_text helper）
- aggregator: clean_plate_no/clean_group_key 共享 _strip_float_tail
- canonical: _canonical_from_row bl_no pop 简化展开
- canonical: _box_type_of/_box_groups_of 合并单次遍历（_boxes_of）
- 门禁：全量 1186 passed / ruff / dep_check 全绿
```
