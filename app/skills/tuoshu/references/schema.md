# 托书统一 JSON Schema

所有托书文档统一抽取为以下 JSON 结构。字段命名英文小写下划线，便于对接业务系统。

## 顶层结构

```json
{
  "doc_type": "PACKING_NOTICE",
  "internal_ref": "ESFF21030474",
  "customs_declaration_no": null,
  "customer_ref": "8650135793",
  "mbl_no": "ONEYSH1AC9779502",
  "hbl_no": null,

  "vessel": "MSC GAYANE",
  "voyage": "FI111A",
  "carrier": "ONE",

  "pol": "SHANGHAI",
  "pod": "SANTOS",
  "transit_port": null,
  "terminal": "洋一",

  "etd": "2021-03-23",
  "si_cutoff": null,
  "customs_cutoff": null,
  "loading_time": "2021-03-19",

  "containers": [
    {
      "type": "40HC",
      "qty": 1,
      "container_no": null,
      "seal_no": null,
      "packages": 1468,
      "packages_unit": "CTNS",
      "gross_weight_kg": 19784,
      "volume_cbm": 62.14,
      "po_no": null,
      "mbl_no": "ONEYSH1AC9779502",
      "remark": null
    }
  ],

  "factory": {
    "name": "常州市天和电动工具有限公司",
    "address": "常州市遥观镇建农工业园",
    "contact": "成雅",
    "phone": "13861235286"
  },

  "shipper_company": null,
  "shipper_agent": null,

  "recipient": "俊泰",
  "doc_date": "2021-12-30",
  "sender": "江苏倍联",
  "sender_contact": "陈俐玲",

  "remark": "10点截单,提供箱封号",

  "order_mapping": {
    "c_sn": "ESFF21030474",
    "mbl_no": "ONEYSH1AC9779502",
    "hbl_no": null,
    "c_title": null,
    "factory_name": "常州市天和电动工具有限公司",
    "c_note": "10点截单,提供箱封号"
  },
  "review_issues": [
    {
      "code": "missing_shipper_company",
      "field": "shipper_company",
      "message": "缺少托运人公司，订单必填字段 c_title 需人工确认",
      "source_values": [],
      "blocking": true
    }
  ],
  "ready_for_order": false,

  "source": {
    "file": "03.19 ESFF21030474 天和托书.xlsx",
    "doc_format": "xlsx",
    "template_hint": "zuoxiang_std",
    "extracted_at": "2026-07-14T11:30:00"
  },

  "raw_text_snippet": "做箱通知书 | 做箱时间：2021/3/19 ..."
}
```

## 字段详解

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `doc_type` | enum | ✓ | 见下方枚举 |
| `internal_ref` | string \| null | | 我司业务编号 / 我司编号；对应订单接口 `c_sn` |
| `customs_declaration_no` | string \| null | | 海关报关单号 / 明确标注的关单号；不得填我司业务编号 |
| `customer_ref` | string \| null | | 客户编号 / PO / 客户订单号 |
| `mbl_no` | string \| null | | 主提单号（Master B/L） |
| `hbl_no` | string \| null | | 子提单号 / 分提单号（House B/L）；不得回退到 `mbl_no` |
| `vessel` | string \| null | | 船名，全大写 |
| `voyage` | string \| null | | 航次，如 `FI111A` / `006W` / `2210E` |
| `carrier` | string \| null | | 船公司缩写（见 `carriers.md`），如 `ONE`/`MSK`/`COSCO` |
| `pol` | string \| null | | 起运港，英文大写；若原文只有"上海"则输出 `SHANGHAI` |
| `pod` | string \| null | | 目的港，英文大写，保留州/国家修饰信息（见 `ports.md`） |
| `transit_port` | string \| null | | 中转港；待查描述如 `见设备交接单` 逐字保留，只有原文真正缺失时才为 null |
| `terminal` | string \| null | | 港区/码头，保留原文（如 `洋一`/`外五`/`梅山码头`） |
| `etd` | date \| null | | 预计开航日 / 船期，`YYYY-MM-DD` |
| `si_cutoff` | datetime \| null | | 截 SI / 截单时间 |
| `customs_cutoff` | datetime \| null | | 截关 / 截报关 |
| `loading_time` | date \| datetime \| null | | 做箱/装箱时间 |
| `containers` | array | ✓ | 见下方 |
| `factory` | object \| null | | 做箱工厂信息 |
| `shipper_company` | string \| null | | 托运人公司；对应订单 `c_title`，不得用收件货代或个人姓名代填 |
| `shipper_agent` | string \| null | | 我方公司/发件方（如"倍联"/"欣一捷"） |
| `recipient` | string \| null | | 收件方（TO 字段） |
| `doc_date` | string \| null | | 文档日期（DATE 字段），格式 `YYYY-MM-DD` |
| `sender` | string \| null | | 发货方（FROM 字段） |
| `sender_contact` | string \| null | | 发货联系人（FROM 后面的联系人姓名） |
| `remark` | string \| null | | 汇总备注 |
| `order_mapping` | object | ✓ | 确定性生成的订单接口映射（`c_sn/c_title/factory_name/c_note`） |
| `review_issues` | array | ✓ | 人工复核问题；同柜数据冲突、关键箱数据缺失、订单必填缺失、人名不保真均列入 |
| `ready_for_order` | boolean | ✓ | 无 blocking 复核问题时才为 true；下单前必须检查 |
| `source` | object | ✓ | 元数据 |
| `raw_text_snippet` | string \| null | | 原文首 200 字符（溯源） |

### doc_type 枚举

| 值 | 中文触发词 |
|----|-----------|
| `PACKING_NOTICE` | 做箱通知书 / 做箱通知 / 装箱通知 |
| `TRANSPORT_ORDER` | 运输委托书 / 货物配舱通知书 |
| `TRUCKING_ORDER` | 派车托书 / 拖车托书 / 车队托书 |
| `BOOKING_NOTE` | 做箱委托书 / 做箱委托单 / 装柜委托 |
| `UNKNOWN` | 无法归类 |

### containers[] 项

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 原文箱型代码，见 `container-types.md`；禁止把 `40HQ` 改成 `40HC` |
| `qty` | int | 该类型/该行的箱数 |
| `container_no` | string \| null | 集装箱号（如 `SEGU1234567`），托书阶段通常为空 |
| `seal_no` | string \| null | 铅封号，托书阶段通常为空 |
| `packages` | int \| null | 件数 |
| `packages_unit` | string \| null | 件数单位，如 `CTNS`/`PACKAGES`/`PKGS` |
| `gross_weight_kg` | number \| null | 毛重（KG） |
| `volume_cbm` | number \| null | 体积（CBM） |
| `po_no` | string \| null | 客户 PO 号 |
| `mbl_no` | string \| null | 该柜对应的提单号（多柜可能各有单号） |
| `remark` | string \| null | 该柜专属备注 |

### factory 项

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string \| null | 工厂名称 |
| `address` | string \| null | 详细地址 |
| `contact` | string \| null | 联系人姓名 |
| `phone` | string \| null | 联系电话 |

### source 项

| 字段 | 类型 | 说明 |
|------|------|------|
| `file` | string | 原文件名 |
| `doc_format` | string | `xlsx`/`xls`/`docx`/`doc`/`pdf`/`image` |
| `template_hint` | string \| null | 若识别出已知模板，填模板 ID；否则 null |
| `extracted_at` | datetime | 抽取时间 |

## 硬性规则

### 精确复制

以下字段必须**从原文逐字符复制**，不得改写字符/大小写：

- `internal_ref`, `customs_declaration_no`, `customer_ref`, `mbl_no`, `hbl_no`, `po_no`, `container_no`, `seal_no`
- `sender`, `sender_contact`, `factory.contact` 等人名字段

原因：一个字符错误（O vs 0、I vs 1、B vs 8）就是业务事故。

### 数字不计算

`packages`、`gross_weight_kg`、`volume_cbm` 只做**格式清洗**（去空格、单位符号），不做算术：

- ✓ `"19,784 KGS"` → `19784`
- ✓ `"62.14 CBM"` → `62.14`
- ✗ 不做多柜求和
- ✗ 不做单位换算（除非原文明确单位为吨/T，此时换算为 KG 并在 `remark` 说明）

输出 JSON 中必须是纯数字，不带单位或千分位：`"2,150 CTNS"` → `packages: 2150`，`"5,375.0 KGS"` → `gross_weight_kg: 5375.0`。

### 港口/箱型/船公司处理

见对应 references 文件。港口和船公司按各自规则处理；箱型仅识别和拆分数量，
`HQ/HC/DV/GP` 等原文代码不得互换。

### 中转港待查描述保真

原文出现以下任一，`transit_port` 必须逐字保留，不能输出 `null`：

- `见设`
- `见设备单`
- `见设备交接单`
- `进港代码请参照设备交接单`
- `见设备`

这些内容表示“存在中转港信息，但需要查询其他文件”，不是“没有中转港”。如果同一文档还出现另一个具体港口值，保留明确值，并通过 blocking 的 `review_issues` 记录冲突和值列表。

### 订单映射与人工复核

- `internal_ref` → `order_mapping.c_sn`
- `mbl_no` / `hbl_no` → `order_mapping.mbl_no` / `order_mapping.hbl_no`，主子提单不得互相回退
- `shipper_company` → `order_mapping.c_title`
- `factory.name` → `order_mapping.factory_name`
- `containers[].po_no` 全量汇总到 `order_mapping.c_note`，并保留原 `remark`
- `c_title` 或 `factory_name` 缺失时必须生成 blocking 复核项
- 同一柜出现两组件数/毛重/体积时，主值和所有候选值都要留痕，并生成 `conflicting_container_data` 复核项；不得自动裁决通过
- `carrier` 与主提单号前缀冲突时，以前缀表修正 JSON，并生成 `carrier_prefix_mismatch` blocking 复核项
- 同一柜 `packages` 与 `volume_cbm` 均为空时，生成 `missing_container_measurements` blocking 复核项
- `柜N：...`/`柜N备注：...` 的柜级信息必须进入对应 `containers[N-1].remark`，不能只放顶层 `remark`

### 展示单一事实源

- API `data` 是唯一事实源，固定使用本 schema 的英文 key
- API 不返回独立摘要字符串；中文审核界面只能逐字段渲染最终校验后的 `data`，禁止调用模型二次生成
- `raw_text_snippet` 对文本型文档由转换原文直接截取，禁止使用模型改写的摘要作为核对证据

### 多柜展开

- 原文 `3*40HC` → `containers: [{type: "40HC", qty: 3, ...}]`（一条记录）
- 原文 `1*40HQ + 2*40GP` → `containers: [{type: "40HQ", qty: 1}, {type: "40GP", qty: 2}]`（两条）
- 原文一票多柜、每柜独立提单号/明细 → 每柜一条记录，`mbl_no` 分别填

### 空值

- 缺失字段 → `null`
- 空字符串 → `null`
- 原文 `/` `无` `见附件` 等占位符 → `null`
- 数字缺失 → `null`（不用 0，除非原文就是 0）

### 日期格式

- Date：`YYYY-MM-DD`
- Datetime：`YYYY-MM-DDTHH:MM:SS`
- 原文如 `10点截单` 且无具体日期 → `si_cutoff: null`，`remark` 保留 `"10点截单"`
- 原文 `2月28日` 无年份 → 依次参考文档日期、文件名或业务编号中的年份；前两项均无线索时才使用当前年份，仍有歧义则 null 并进入人工复核
- `开港时间`不是 `etd`，原样并入 `remark`；只有开航/开船/船期进入 `etd`
