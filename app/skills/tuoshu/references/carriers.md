# 船公司识别

船公司字段 `carrier` 有两种来源：**原文明确写船公司** 或 **通过提单号前缀推断**。

## 输出规范

- 原文明示船公司时逐字保留完整值，不截断附加部分，如 `EMC CPS`
- 仅通过提单号前缀/船名兜底推断时输出标准大写缩写，如 `ONE`/`EMC`

## 提单号前缀 → 船公司

主流船公司的提单号前缀（SCAC 或自定义 4 位前缀）：

| 前缀 | carrier | 全称 |
|------|---------|------|
| `ONEY` | `ONE` | Ocean Network Express（含前身 K Line/MOL/NYK） |
| `HLCU` | `HLC` | Hapag-Lloyd |
| `HDMU` | `HMM` | HMM（现代商船） |
| `MAEU` `MSKU` `MRKU` | `MSK` | Maersk（马士基） |
| `MSCU` `MEDU` | `MSC` | MSC 地中海航运 |
| `COSU` `CSNC` | `COSCO` | 中远海运 |
| `OOLU` | `OOCL` | 东方海外 |
| `CMDU` `APLU` | `CMA` | CMA CGM / APL |
| `EGLV` `EMCU` | `EMC` | Evergreen（长荣） |
| `YMLU` `YMJA` | `YML` | Yang Ming（阳明） |
| `KKLU` | `KLINE` | 已并入 ONE（现划归 ONE） |
| `SITC` `SITG` | `SITC` | SITC 海丰 |
| `WHLC` `WHSU` | `WHL` | Wan Hai（万海） |
| `ZIMU` | `ZIM` | ZIM |
| `PABV` `PILU` | `PIL` | Pacific International Lines |
| `TSLU` | `TSL` | TS Lines |
| `RCLU` `RCLB` | `RCL` | Regional Container Lines |
| `KMTU` `HEUN` | `HEUNG` | Heung-A / 兴亚 |
| `SNKO` `KMDU` | `KMTC` | Korea Marine Transport |
| `PCIU` | `PANCON` | Pan Continental |
| `MATS` | `MATSON` | Matson |
| `SUDU` | `HAMSUD` | Hamburg Süd（已并入 Maersk，仍单独统计） |
| `HJSC` | `HJS` | Hanjin（已破产，历史单据） |
| `KFLB` `KFLS` `KFLU` `KFLSE` `KFLSH` `KFLSE` | `KAWA` | Kawasaki-Kisen / K-Line 支线（各分公司前缀） |
| `PDLU` `PELS` `PELSHN` | `PDL` | PDL / PIL 支线 |
| `DNPO` `DNPOB` | `DONGJIN` | Dongjin |
| `TGSH` | `TGL` | Sinotrans / TG Line（部分文档） |
| `CNRX` `CNCT` `CNXW` | `CNSH` | 中国船代号，具体船公司需结合船名 |
| `WBHY` `WBHYC` | `WEIBAO` | 未知/私营；标注为原前缀 |
| `SHHQ` `SHJA` `SHJHY` `SHTC` `SHCK` | `SHCH` | 上海本地货代前缀，多为 NVOCC |
| `JSTG` `JSSH` | `JSTG` | 江苏拖车/货代 |
| `SECU` | `SECU` | 未知，保留 |
| `M236` `N236` `292` | 数字类 | 通常是货代或车队编号，非船公司提单号；`carrier` 输出 null |

## 处理规则

1. **优先原文**：若原文有 `船公司: ONE` 或明确船名（如 `MAERSK HORSBURGH`）→ 从船名/明文推断
2. **提单号推断**：提单号首 3-4 位字母查表
3. **船名推断**：无提单号时，从船名前缀推断（示例见下）
4. **数字型编号**：无字母前缀的编号（如 `292340329`、`257299206`）是货代内部业务号，**不是船公司提单号**，`carrier` 输出 null
5. **多船公司**：一票不可能对应多船公司；原文明示船公司优先，只有原文缺失时才用主提单号前缀兜底

## 船名 → 船公司（辅助）

| 船名前缀 | carrier |
|---------|---------|
| `MAERSK` / `MSC` / `MSK` | MSK 或 MSC 需看具体 |
| `COSCO` | COSCO |
| `EVER` | EMC |
| `HYUNDAI` | HMM |
| `HMM` | HMM |
| `ONE` / `NYK` / `MOL` | ONE |
| `CMA CGM` / `APL` | CMA |
| `OOCL` | OOCL |
| `YANG MING` | YML |
| `HAPAG` / `HAPAG-LLOYD` | HLC |
| `ZIM` | ZIM |
| `WAN HAI` | WHL |
| `SITC` | SITC |
| `MSC` | MSC |

## 未知处理

前缀不在表中：

- 若前 4 位是字母且格式类似提单号（4 字母 + 8-10 数字），保留原提单号，`carrier` 输出 `null` 并在 `remark` 附注 `carrier_unknown_prefix: XXXX`
- 若明显是内部编号（纯数字或含 `-`），`carrier` 输出 null
