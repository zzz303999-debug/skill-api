# parsing 包冗余收敛方案（P1+P2 纯重构，P3 待拍板，P4 另开）

> 状态：**待确认**（2026-09-09 首版）
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

### P3 · ai_header `_to_money` 与 parser 口径统一（**待拍板：行为修正**）

**现状**（实证）：ai_header._to_money（L242，13 行）docstring 自称"与
parser._to_money 同口径"，实为弱版——只 `float(text.strip())`，不处理：
- 千分位/货币符号（to_number 支持）→ 简单版返回 None
- 多段文本 `'1300\r\n2200'` 拆分累加 → 简单版 float 整体失败返回 None

parser._to_money（L136，21 行复杂版）才是真解析口径（T9：合并单元格多值累加）。

影响：L3 校验闸门抽样核对（_sample_check）用弱版，对 AI 给出的金额做核对时，
千分位/多段形态会被误判为"值不符"→ 保守方向误拒（AI 映射被拒走 400 或回退），
不产生错数据但偶发误拒异构模板。

**候选 A（推荐）**：ai_header._to_money 删除，调用点改 `parser._to_money`（parser
无 ai_header 依赖 → 无环）；校验口径与真解析一致 → 误拒面收敛。**行为修正**
（采纳判定边界变化：少拒合法映射），需测试适配（若有针对弱版的用例）。
**候选 B**：保持双版，仅改 ai_header docstring 去掉"同口径"误导（纯文档）。
**候选 C**：本轮不动，P4 拆 parser 时顺带收口。

### P4 · parser.py 880 行拆分（另开，不入本轮）

`_parse_with_template` 338 行（L400-737，调用 25 个符号）为全仓最大单体函数；
`_match_template_and_parse` / `read_data_rows` 等周边也随行。拆法候选：
横向（模板行构造段抽 `_RowBuilder` 类）或纵向（按模板/AI/精确三分文件）——
工程量大、行为零变更验证成本高，建议 P1-P3 稳定后仿 service.py P1-P4 模式另出文档。

---

## §2 验证纪律（每阶段）

1. bill 域测试全绿（含 golden 家族样本——指纹/解析输出逐字节不变的最佳守护）
2. ruff + dep_check --assert-rules
3. P1/P2 合并或独立提交均可（同主题收敛）；P3 若落地必须独立提交（行为修正）
4. 全量 pytest 1187 基线不降

## §3 建议执行顺序

1. **P1 + P2**（纯重构，一次提交或分两次——文档审定后执行）
2. **P3 拍板**（A/B/C 三选一；A 案独立提交 + 测试适配）
3. P4 另开会话

确认后从 P1 开始；P3 请一并给 A/B/C 意向。
