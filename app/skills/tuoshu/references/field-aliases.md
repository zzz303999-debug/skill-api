# 字段同义词表

不同货代/客户对同一字段使用的中文标签差异极大。LLM 抽取时按下表将原文标签映射到 schema 字段。

**匹配规则**：模糊匹配（去空格、去冒号、忽略"："/"："/"号"等后缀）。若同一文档中同时出现多个别名，按表格中列出的**优先级**取值。

## booking_no（我方业务编号）

优先级从高到低：

| 别名 | 出现文档举例 |
|------|-------------|
| 我司业务编号 | 03.19 天和托书.xlsx |
| 我司编号 | 1KT310158上海三人行做箱委托书.docx |
| 关单号 | 03-29 OOLU2693179200拖车托书.docx |
| 育海编号 | 12.docx / 13.docx |
| 倍联业务编号 | 特格威系列 |
| 编号 | 通用 |
| booking no / booking number | 英文托书 |
| BKG NO | 英文托书 |

**注意**：`关单号`在部分文档里是海关关单号，但在很多货代的模板中就是他们的业务编号。若同时出现`我司编号`和`关单号`，取`我司编号`。

## customer_ref（客户编号 / 客户订单号）

| 别名 |
|------|
| 客户编号 |
| 客户订单号 |
| 客户参考号 |
| PO / PO# / PO NO / P.O. |
| 工厂合同号 |
| CUSTOMER REF |
| CUSTOMER PO |

## mbl_no（主提单号 / 主单号）

| 别名 |
|------|
| 主提单号 |
| 主单号 |
| 提单号（默认视为主提单，除非同时出现"分单号"） |
| MBL NO / MB/L / M B/L |
| MASTER BL |
| 关单号（**特殊情况**：当同一文档没有"提单号"字段但"关单号"字段值符合提单号格式如 `ONEYSH...` 时） |

## hbl_no（分提单号 / 分单号）

| 别名 |
|------|
| 分提单号 |
| 分单号 |
| HBL NO / HB/L |
| HOUSE BL |

## vessel（船名）

| 别名 |
|------|
| 船名 |
| 船名航次（**含航次**，需拆分） |
| VESSEL |
| VSL |

### 船名航次拆分规则

原文往往是 `船名 + 航次` 合写：

- `MSC GAYANE   FI111A` → vessel=`MSC GAYANE`, voyage=`FI111A`
- `HMM DUBLIN / 006W` → vessel=`HMM DUBLIN`, voyage=`006W`
- `BALLENITA/0PPT4E` → vessel=`BALLENITA`, voyage=`0PPT4E`
- `MAERSK HAMBURG 202W` → vessel=`MAERSK HAMBURG`, voyage=`202W`
- `EVER SAFETY/0358-099E` → vessel=`EVER SAFETY`, voyage=`0358-099E`
- `MSC ARINA FJ207W` → vessel=`MSC ARINA`, voyage=`FJ207W`

分隔符：`/`、`V.`、`V`、多个空格。航次通常是 `字母+数字+字母` 或纯数字+字母 结尾。

## voyage（航次）

见上，通常与船名合并出现。

## pol（起运港）

| 别名 |
|------|
| 起运港 |
| 装货港 |
| POL / PORT OF LOADING |
| FROM |

若原文只写中文 `上海` `宁波` 等，输出对应英文大写：`SHANGHAI` / `NINGBO`。

## pod（目的港）

| 别名 |
|------|
| 目的港 |
| 目的地 |
| 卸货港 |
| POD / DESTINATION / PORT OF DISCHARGE |
| TO |

## transit_port（中转港）

| 别名 |
|------|
| 中转港 |
| 中转港代码 |
| 中转 |
| TS PORT / TRANSSHIPMENT |

值处理见 `schema.md`（"见设"规则）。

## terminal（港区/码头）

| 别名 |
|------|
| 港区 |
| 码头 |
| TERMINAL |

保留原文（`洋一`/`洋4`/`外二`/`梅山码头`），不归一。

## etd（预计开航日 / 船期）

| 别名 |
|------|
| 船期 |
| 开航日 |
| 预计开航 |
| ETD |
| SAILING DATE |

## si_cutoff（截 SI / 截单）

| 别名 |
|------|
| 截 SI |
| 截 SI&VGM |
| SI 截止 |
| 截单时间 |
| 截信息 / 截信息： |
| SI CUT-OFF / SI CUTOFF |

## customs_cutoff（截关）

| 别名 |
|------|
| 截关时间 |
| 截关 |
| 截报关 |
| CUSTOMS CUT-OFF |
| CY CUT-OFF |

## loading_time（做箱/装箱时间）

| 别名 |
|------|
| 做箱时间 |
| 做箱日期 |
| 装箱时间 |
| 装箱日期 |
| 拆装箱日期 |
| LOADING DATE / STUFFING DATE |

## containers[].type（箱型）

| 别名 |
|------|
| 箱型箱量（**含数量**，需拆分） |
| 箱量 |
| 箱型 |
| CONTAINER TYPE |
| CTNR |

### 箱型箱量拆分规则

- `1*40HC` → type=`40HC`, qty=`1`
- `2×40HQ` → type=`40HQ`, qty=`2`
- `40HQ*9` → type=`40HQ`, qty=`9`
- `40'GP *9` → type=`40GP`, qty=`9`
- `1*40HQ+2*40GP` → 拆两条：`{40HQ,1}` + `{40GP,2}`
- `1X40HQ` / `1x40HQ` → type=`40HQ`, qty=`1`
- `20GPX1` → type=`20GP`, qty=`1`

归一见 `container-types.md`。

## containers[].packages（件数）

| 别名 |
|------|
| 件数 |
| 件数为 |
| PACKAGES / PKGS / CTNS |
| QUANTITY / QTY |

## containers[].gross_weight_kg（毛重）

| 别名 |
|------|
| 毛重 |
| 毛重(KGS) |
| 毛重为 |
| GROSS WEIGHT / G.W. |
| KGS |

单位换算：吨/T → KG（× 1000）

## containers[].volume_cbm（体积）

| 别名 |
|------|
| 体积 |
| 体积(CBM) |
| 体积为 |
| VOLUME / MEASUREMENT |
| CBM / M3 |

## factory.*

工厂信息通常成组出现：

| schema 字段 | 常见别名 |
|------------|---------|
| `factory.name` | 做箱工厂 / 装箱工厂 / 客户名称 / 做箱地址（含工厂名） |
| `factory.address` | 地址 / 做箱地址 / 提货地址 / ADD |
| `factory.contact` | 联系人 |
| `factory.phone` | 电话 / 联系电话 / 手机 |

**注意**：
1. `联系人` 单元格常同时含姓名和电话（如 `成雅 13861235286`），需拆分。
2. **`factory.name` 优先取"客户名称/装箱工厂"字段**，其次是文档强调的品牌名（如 `喜临门家具` / `特格威`）。**不要把"提货地址"里的仓库/保税区名当作工厂名**。地址里若出现 `XX 保税仓库` `XX 工业园` 等属于地点，`factory.name` 应回到上下文里的客户/品牌名。

## remark（备注）

| 别名 |
|------|
| 备注 |
| 备注： |
| 注意事项 |
| REMARK / NOTE |

## shipper_agent（我方公司/发件方）

通常出现在文档顶部标题或"FM"字段：

| 别名 |
|------|
| FM / 发件人 |
| 文档抬头公司名（如"浙江经茂国际货运代理有限公司"） |

## 常见冲突处理

1. **"关单号"歧义**：优先当 booking_no；若同时有明确的 "我司编号"，"关单号" 就是 mbl_no
2. **"提单号"歧义**：默认 mbl_no；同时出现"主单号"和"提单号"则可能"提单号"是 hbl_no
3. **"目的港" vs "中转港代码"**：`中转港代码` 是三字/五字港口代码（如 `ITTRS`/`USTIW`），仍归 `transit_port`
4. **多列同名**：Excel 里可能"我司编号"列有 header 但值分布在下方多行 → 各柜独立记录
