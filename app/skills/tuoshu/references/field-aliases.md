# 字段同义词表

不同货代/客户对同一字段使用的中文标签差异极大。LLM 抽取时按下表将原文标签映射到 schema 字段。

**匹配规则**：模糊匹配（去空格、去冒号、忽略"："/"："/"号"等后缀）。若同一文档中同时出现多个别名，按表格中列出的**优先级**取值。

## internal_ref（我司业务编号）

优先级从高到低：

| 别名 | 出现文档举例 |
|------|-------------|
| 我司业务编号 | 03.19 天和托书.xlsx |
| 我司编号 | 1KT310158上海三人行做箱委托书.docx |
| 育海编号 | 12.docx / 13.docx |
| 倍联业务编号 | 特格威系列 |
| 编号 | 通用 |
| booking no / booking number | 英文托书 |
| BKG NO | 英文托书 |

上述字段是货代内部业务标识，对应订单接口 `c_sn`，不得放入 `customs_declaration_no`。

## customs_declaration_no（报关单号 / 关单号）

| 别名 |
|------|
| 报关单号 |
| 海关报关单号 |
| 关单号 |

只有原文明示这些标签时才填写。若同时出现`我司编号`和`关单号`，分别填入 `internal_ref` 和 `customs_declaration_no`，不得覆盖。

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
| VESSEL |
| VSL |

## voyage（航次）

| 别名 |
|------|
| 航次 |
| VOYAGE |
| VOY |
| VOY |

`船名航次`/`船名/航次` 为组合字段时，同时填写 `vessel` 和 `voyage`。优先按 `/` 或 `V.` 拆分，例如 `BALLENITA/0PPT4E`、`RESURGENCE V.1728S`；无法可靠拆分时不得猜测。

## carrier（承运人 / 船公司）

| 别名 |
|------|
| 承运人 |
| 船公司 |
| CARRIER |
| 船东 |

## pol（起运港 / 装货港）

| 别名 |
|------|
| 起运港 |
| 装货港 |
| 装港 |
| PORT OF LOADING |
| POL |

## pod（目的港 / 卸货港）

| 别名 |
|------|
| 目的港 |
| 卸货港 |
| 卸港 |
| PORT OF DISCHARGE |
| POD |
| DESTINATION |

## transit_port（中转港）

| 别名 |
|------|
| 中转港 |
| 中转 |
| 中转地 |
| TRANSSHIPMENT PORT |
| 转运港 |
| 目的港代码（当中转港代码出现时） |

## terminal（港区 / 码头）

| 别名 |
|------|
| 港区 |
| 码头 |
| 停靠港区 |
| 进港代码（当值为中文港区名时） |

## etd（船期 / 开航日）

| 别名 |
|------|
| 船期 |
| 开航日 |
| 开航时间 |
| 开船时间 |
| 预计开航 |
| 预计开船 |
| ETD |
| SAILING DATE |

`开港时间`/`开港日期`不是开航时间，不得写入 `etd`，应原样并入 `remark`。

## si_cutoff（截单时间 / 截 SI）

| 别名 |
|------|
| 截单时间 |
| 截单 |
| 截SI |
| SI CUTOFF |
| SI CUT OFF |
| CUTOFF |

## customs_cutoff（截关 / 截报关）

| 别名 |
|------|
| 截关 |
| 截关时间 |
| 截报关 |
| CUSTOMS CUTOFF |

## loading_time（做箱时间 / 装箱时间）

| 别名 |
|------|
| 做箱时间 |
| 做箱日期 |
| 装箱时间 |
| 装箱日期 |
| 拆装箱日期 |
| 装柜时间 |
| 装柜日期 |
| LOADING DATE |
| STUFFING DATE |

## containers（集装箱信息）

| 别名 |
|------|
| 箱信息 |
| 集装箱 |
| 柜信息 |
| CONTAINER LIST |
| CARGO INFO |

## factory（工厂信息）

| 别名 |
|------|
| 工厂 |
| 工厂信息 |
| 做箱工厂 |
| FACTORY INFO |

`门点地址` → `factory.address`；`工厂联系人` → `factory.contact`；`工厂电话` → `factory.phone`。

## shipper_company（托运人公司）

| 别名 |
|------|
| 托运人公司 |
| 托运人 |
| 发货人公司 |
| 发货公司 |
| SHIPPER |

收件方（TO）为货代或 `FROM/FM` 只有个人姓名时，不能据此推断托运人公司。缺失时输出 null，由人工补齐订单 `c_title`。

## shipper_agent（发件方 / 我方公司）

| 别名 |
|------|
| 发件方 |
| 我方公司 |
| 货代 |
| SHIPPER AGENT |

`FROM/FM` 单独作为 `sender` 原样保留；只有明确的公司名和托运人标签才能进入 `shipper_company`。

## remark（备注）

| 别名 |
|------|
| 备注 |
| 注意事项 |
| 特别说明 |
| REMARKS |
| NOTE |

## 歧义处理

1. **"关单号"歧义**：优先当 customs_declaration_no。即使值符合提单号格式（如 ONEYSH...），也不覆盖 `internal_ref`。仅当同一值还被明确标注为"提单号"时，才同时填入 mbl_no。
2. **"提单号"歧义**：默认当 mbl_no；若同时出现"分单号"字段，则"提单号"仍为 mbl_no，"分单号"为 hbl_no。
3. **"目的港代码" vs "中转港代码"**：原文"中转港代码" → transit_port；"目的港代码" → pod。
