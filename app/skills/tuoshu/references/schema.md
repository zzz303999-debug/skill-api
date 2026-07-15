# 托书统一 JSON Schema

所有托书文档统一抽取为以下 JSON 结构。字段命名英文小写下划线，便于对接业务系统。

## 顶层结构

```json
{
  "doc_type": "PACKING_NOTICE",
  "booking_no": "ESFF21030474",
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

  "shipper_agent": null,

  "remark": "10点截单,提供箱封号",

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
| `booking_no` | string \| null | | 我方业务编号 / 我司编号 / 关单号（首选） |
| `customer_ref` | string \| null | | 客户编号 / PO / 客户订单号 |
| `mbl_no` | string \| null | | 主提单号（Master B/L） |
| `hbl_no` | string \| null | | 分提单号（House B/L） |
| `vessel` | string \| null | | 船名，全大写 |
| `voyage` | string \| null | | 航次，如 `FI111A` / `006W` / `2210E` |
| `carrier` | string \| null | | 船公司缩写（见 `carriers.md`），如 `ONE`/`MSK`/`COSCO` |
| `pol` | string \| null | | 起运港，英文大写；若原文只有"上海"则输出 `SHANGHAI` |
| `pod` | string \| null | | 目的港，英文大写，去中文/州省后缀（见 `ports.md`） |
| `transit_port` | string \| null | | 中转港；原文 `见设`/`见设备单`/`见设备交接单` → null |
| `terminal` | string \| null | | 港区/码头，保留原文（如 `洋一`/`外五`/`梅山码头`） |
| `etd` | date \| null | | 预计开航日 / 船期，`YYYY-MM-DD` |
| `si_cutoff` | datetime \| null | | 截 SI / 截单时间 |
| `customs_cutoff` | datetime \| null | | 截关 / 截报关 |
| `loading_time` | date \| datetime \| null | | 做箱/装箱时间 |
| `containers` | array | ✓ | 见下方 |
| `factory` | object \| null | | 做箱工厂信息 |
| `shipper_agent` | string \| null | | 我方公司/发件方（如"倍联"/"欣一捷"） |
| `remark` | string \| null | | 汇总备注 |
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
| `type` | string | 归一后的箱型，见 `container-types.md`，如 `20GP` / `40GP` / `40HC` / `45HC` |
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

- `booking_no`, `customer_ref`, `mbl_no`, `hbl_no`, `po_no`, `container_no`, `seal_no`

原因：一个字符错误（O vs 0、I vs 1、B vs 8）就是业务事故。

### 数字不计算

`packages`、`gross_weight_kg`、`volume_cbm` 只做**格式清洗**（去空格、单位符号），不做算术：

- ✓ `"19,784 KGS"` → `19784`
- ✓ `"62.14 CBM"` → `62.14`
- ✗ 不做多柜求和
- ✗ 不做单位换算（除非原文明确单位为吨/T，此时换算为 KG 并在 `remark` 说明）

### 港口/箱型/船公司归一

见对应 references 文件。**归一必须查表**，不得自由发挥。

### 中转港的"见设"规则

原文出现以下任一，`transit_port` 输出 `null`：

- `见设`
- `见设备单`
- `见设备交接单`
- `进港代码请参照设备交接单`
- `见设备`

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
- 原文 `2月28日` 无年份 → 参考文件名/上下文推断，无法推断则 null
