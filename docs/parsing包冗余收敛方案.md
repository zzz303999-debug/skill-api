# parsing 包冗余收敛方案（P1-P3 已实施；P4 S1/S2 已实施）

> 状态：P1/P2（0699199）、P3 A 案（2aec0bb）；P4-S1（66184ab）与 P4-S2 已实施
> 范围：`app/orders/bill/parsing/`（columns / template_store / legacy_template /
> ai_header / normalizers）+ `app/orders/bill/aggregation/aggregator.py`（仅正则/清洗收口）
> 性质：P1/P2 结构性收敛，**行为零变更**（输出/采纳判定/指纹逐字节不变）；
> P3 为行为修正候选（单列待拍板）；P4 大文件拆分另开不入本轮
> 背景：parsing 簇 2733 行 / 8 文件 AST + 逐字对比勘察，发现 3 处同语义重复
> 实现 + 1 处大文件负债

---

## §0 明确不动

- 双库并行（legacy_template sha1-16 / template_store md5-8）与回退链：线上有按
  旧指纹固化的模板，两套库键不可互相替代（docstring 既有警示，保留）
- `_parse_with_template` 338 行大函数：拆分属 P4（工程量大，P1-P3 稳定后再开）
- `normalizers` 各清洗函数的业务语义与 `NORMALIZER_REGISTRY` 注册面
- columns 的列匹配/业务列边界/费用列发现逻辑（2026-09 刚拆出，边界干净）

---

## §1 改动清单

### P1 · 表头去空白 4 文件单源收口（`_HEADER_WHITESPACE_RE` 五份 → 一份）

**现状**（实证）：`re.compile(r"[\s\u3000]+")` 逐字重复 4 份 +
每份配套一个"去全部空白"函数：

| 文件 | 正则 | 函数 | None 保护 |
|---|---|---|---|
| columns.py | `_HEADER_WHITESPACE_RE` | `normalize_header(text)`（公开） | 无 |
| template_store.py | `_HEADER_WHITESPACE_RE` | `_normalize(text)` | `or ""` |
| legacy_template.py | `_HEADER_WHITESPACE_RE` | `_normalize(text)` | `or ""` |
| ai_header.py | `_HEADER_WHITESPACE_RE` | `_strip_whitespace(text)` | `or ""` |

四者语义一致（表头文本去空白参与匹配/指纹）；消费面全部传非 None 的 str
（parser 8 处调用 normalize_header 均 `_header_text()`/`str(... or "序号")` 产出）。

**改法**：
1. columns.normalize_header 加 `or ""` 防御（对现有 str 调用面行为零差异，纯防御）；
2. template_store / legacy_template 的 `_normalize` 删除，调用点改 `normalize_header`
   （`from .columns import normalize_header`；同包相对导入，方向 columns 无 parsing
   内依赖 → 无环）；
3. ai_header `_strip_whitespace`（L468，仅 L415/420 两调用点）删除，改
   `normalize_header`（`from .columns import`；ai_header 现依赖 fees/schema，
   无 columns 反向引用 → 无环）。

**效果**：正则 4 份 → 1 份；函数 3 份私有重复 → 公开单源；净 -20 行上下。

**风险**：低（纯搬移 + 防御增强；指纹/匹配输出逐字节不变——同正则同输入）。

### P2 · 浮点尾巴正则/清洗双实现收口（normalizers 单源，aggregator 改引用）

**现状**（实证）：`_FLOAT_TAIL_RE = re.compile(r"^\d+\.0+$")` 双份 +
清洗函数双实现：

- normalizers.py：`strip_float_tail(value)`（公开，含 None/空白 → None、非纯数字
  形态原样返回、注册进 NORMALIZER_REGISTRY）
- aggregator.py：`_strip_float_tail(text)`（私有，同正则同语义，仅省 None 保护）+
  `clean_order_num` 内联 `if _FLOAT_TAIL_RE.match(cleaned): cleaned = cleaned.split(".")[0]`

**改法**（aggregation → parsing.normalizers 单向引用，normalizers 无 bill 内
依赖 → 无环；流水线方向解析→聚合，下游引用上游纯函数库）：
1. aggregator 删 `_FLOAT_TAIL_RE` 与 `_strip_float_tail`，改
   `from ..parsing.normalizers import strip_float_tail`；
2. `clean_plate_no` / `clean_group_key` 两函数体替换为单行委托
   `return strip_float_tail(value)`（原实现 = strip+判空+尾巴去除三件套，与
   strip_float_tail 对 None/空白/非数字形态的处理逐分支等价——见 §0 等价性）；
3. `clean_order_num` 内联两行改 `cleaned = strip_float_tail(cleaned)`（非纯数字
   形态原样返回 = 原 if 不命中路径，等价）。

**效果**：正则/清洗单源；aggregator 净 -15 行上下；两通道（模板 normalizers 引用
与归集清洗）同口径不再漂移。

**风险**：低（逐分支等价性已核对）；等价性论证：
- `strip_float_tail(None)=None` ↔ clean_plate_no None→None ✓
- 空白串 → None ↔ 原 return None ✓（clean_group_key 同构）
- 纯数字 `.0+` → 去尾 ↔ 原 _strip_float_tail 同分支 ✓
- 其余文本 strip 后原样 ↔ 原实现 strip 后原样 ✓

### P3 · ai_header `_to_money` 与 parser 口径统一（**A 案已实施 2026-09-09**）

**现状**（实证）：ai_header._to_money（L242，13 行）docstring 自称"与
parser._to_money 同口径"，实为弱版——只 `float(text.strip())`，不处理：
- 千分位/货币符号（to_number 支持）→ 简单版返回 None
- 多段文本 `'1300\r\n2200'` 拆分累加 → 简单版 float 整体失败返回 None

parser._to_money（L136，21 行复杂版）才是真解析口径（T9：合并单元格多值累加）。

影响：L3 校验闸门抽样核对（_sample_check）用弱版，对 AI 给出的金额做核对时，
千分位/多段形态会被误判为"值不符"→ 保守方向误拒（AI 映射被拒走 400 或回退），
不产生错数据但偶发误拒异构模板。

**候选 A（推荐）✅ 已实施**：ai_header._to_money 删除，调用点改 `parser._to_money`
（ai_header → parser 单向，无环）；校验口径与真解析一致 → 误拒面收敛。
**行为修正**（采纳判定边界变化：少拒合法映射——千分位/货币符号/多段文本费用列
不再误拒），新增锁定用例 test_fee_sample_accepts_thousands_text（1,234.50
文本抽样通过 + 收录金额 1234.5）；既有"全中文金额拒"用例回归不变。
**候选 B**：保持双版，仅改 ai_header docstring 去掉"同口径"误导（纯文档）。
**候选 C**：本轮不动，P4 拆 parser 时顺带收口。

### P4 · parser.py 880 行拆分（S1/S2 已实施 2026-09-09）

**现状（AST 实证）**：parser.py 879 行，构成：

| 段 | 行段 | 内容 |
|---|---|---|
| ParseOutput | 63-88 | 解析结果模型（公共面，bill/__init__ 与 service 引用） |
| 表头/行级设施 | 90-397 | 12 函数 ≈308 行（_to_money/find_header_row/read_data_rows/结算区间 4 函数/…） |
| `_parse_with_template` | 400-737 | **338 行全仓最大单体**（准备段 65 / 动态收录 57 / 主循环 154 / 收尾 25） |
| 编排 | 740-851 | _match_template_and_parse/_parse_with_ai_result/_parse_exact/_parse_sheet ≈111 行 |

**目标**：结算区间纵向拆出 + `_parse_with_template` 无 >100 行函数；公共面符号
（ParseOutput）留 parser.py 不动。

#### P4-S1 · 结算区间簇纵向拆出 → `parsing/period.py`（纯搬移）

迁 `extract_bill_period` / `_format_period_date` / `_settlement_text` /
`_resolve_year_hint` + `_PERIOD_DATE_RE`（4 函数 ≈48 行）；**零外部引用已实证**
（app/tests 均无直调）→ 仅 parser.py 内部 import 调整（`from .period import
...`），调用点 4 处不变名。风险：低。

#### P4-S2 · `_parse_with_template` 段落子函数化（同文件，行为零变更）

按实证段落边界抽 3 个私有函数，消除 338 行单体：

1. `_layout_from_template(template, match, view, filename) -> RowParseLayout`：
   准备段 L411-477 + 动态收录候选段 L498-554（~120 行）→ dataclass
   `RowParseLayout` 收口 15+ 展开变量（field_cols/fee_cols/new_fee_cols/
   anchor_cols/channels/ignored_cols/candidates/dynamic_fee_cols/unmatched_raw/
   seq_col/year_hint/is_billrow/business_end…，仿 master_data `_ArchiveSession`
   收口先例）；
2. `_collect_row_fees(view, row, layout) -> RowFees`：主循环内费用三抽 + 锚点段
   L606-673（~68 行）→ dataclass `RowFees`（fee_items/anchors/fee_failures/
   fee_skipped 四收集器收口）；
3. 主循环 L557-710 保留为单循环（行过滤/字段收集/落行双路），行体降到 ~80 行；
   收尾 L712-737 留 `_parse_with_template` 主体（~30 行 + 循环）。

净效果：`_parse_with_template` 338 → 102 行；新增 2 个 dataclass + 4 函数；
parser.py 行数持平微增（dataclass/函数头成本，纵向收益在 S1）。

#### P4-S3 · read_data_rows 旧链路簇纵向拆出（可选，本轮不做）

`read_data_rows`/`_dynamic_fee_cols`/`_header_columns`/`_column_lookup` 等与模板
链路 helper（_to_money/_header_text/find_header_row）高度交织，拆分需跨模块
双向引用梳理 + 测试 import 改指（test_fee_dynamic/test_fees 直调）——收益
（-160 行）低于梳理成本，建议 S1+S2 落地后视 parser 行数再开。

#### P4 明确不动

- 编排三函数（_match_template_and_parse/_parse_with_ai_result/_parse_exact）边界
- 各 helper 归属（共享面大，移动收益低）
- ParseOutput 公共面（bill/__init__ 与 service 引用，不迁）

#### P4 验证纪律

S1/S2 独立提交；每步：bill 域 + 全量 pytest（golden 家族样本守护解析输出逐
字节不变）+ ruff + dep_check 全绿。

#### P4 实施记录（2026-09-09）

- **S1（66184ab）**：结算区间簇 → period.py（extract_bill_period /
  resolve_year_hint 公开化 + 簇内私有 helper），parser 879→823；
- **S2**：`RowParseLayout`/`RowFees` dataclass 收口 + `_layout_from_template`
  （99 行）/`_discover_dynamic_fee_cols`（88 行，B1/B1' 动态收录候选原样随迁，
  含 mapped_cols_flat 判据逐字保持）/`_collect_row_fees`（85 行）/
  `_append_parsed_row`（35 行）——**338 行单体消除，最大函数 102 行**；
- 门禁：bill 域 554 / 全量 1188 passed、ruff、dep_check 全绿；
- 后续：S3（read_data_rows 簇，可选）视需要另开。

---

## §2 验证纪律（每阶段）

1. bill 域测试全绿（含 golden 家族样本——指纹/解析输出逐字节不变的最佳守护）
2. ruff + dep_check --assert-rules
3. P1/P2 合并或独立提交均可（同主题收敛）；P3 若落地必须独立提交（行为修正）
4. 全量 pytest 1187 基线不降

## §3 建议执行顺序

1. ✅ **P1 + P2**（0699199）
2. ✅ **P3 A 案**（2aec0bb）
3. **P4 待确认**：S1（period 拆出）→ S2（_parse_with_template 子函数化）→
   S3（read_data_rows 簇，可选后置）——确认后按 S1→S2 执行，每步独立提交
