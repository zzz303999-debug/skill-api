# 港口名清洗

托书中的港口名格式极其随意，抽取时按以下规则归一。

## 输出规范

- 港口名统一**英文大写**
- 去掉中文注释、州省缩写后缀（除非港口需要区分）、路由标记、换行、特殊符号
- 未知港口保留原文大写

## 常见清洗场景

### 1. 混合中英文

| 原文 | 输出 |
|------|------|
| `SANTOS` | `SANTOS` |
| `HAMBURG` | `HAMBURG` |
| `Hamburg` | `HAMBURG` |
| `上海` | `SHANGHAI` |
| `宁波` | `NINGBO` |

### 2. 州省/国家后缀

美国港口常带州缩写，欧洲/南美常带国家：

| 原文 | 输出 | 说明 |
|------|------|------|
| `SEATTLE, WA` | `SEATTLE` | 去州缩写 |
| `TACOMA,WA` | `TACOMA` | 去州缩写 |
| `KANSAS CITY, MO` | `KANSAS CITY` | 保留城市名，去州 |
| `MOBILE` | `MOBILE` | 已是纯城市名 |
| `LONGVIEW` | `LONGVIEW` | |
| `LAZARO CARDENAS` | `LAZARO CARDENAS` | 墨西哥港，无后缀 |
| `HAIPHONG(DINH VU),VIET NAM` | `HAIPHONG` | 去括号内小港、去国家 |
| `SANTOS,BR` | `SANTOS` | 去国家缩写 |

**特例**：若不去州省会引起歧义（比如"PORTLAND, OR"和"PORTLAND, ME"是两个不同港口），保留州以区分：

| 港口 | 输出 |
|------|------|
| `PORTLAND, OR` | `PORTLAND, OR` |
| `PORTLAND, ME` | `PORTLAND, ME` |
| `NEWARK, NJ` | `NEWARK` | 只有一个 NEWARK 主流港，可去 |

**默认策略**：无歧义时去后缀，有歧义时保留。

### 3. 港口代码（5 字母）

有些托书写的是 UN/LOCODE 五字母代码而不是城市名：

| 原文 | 输出 | 说明 |
|------|------|------|
| `USTIW` | `USTIW` | 保留代码 |
| `ITTRS` | `ITTRS` | Trieste，保留代码 |
| `SKINC` | `SKINC` | Incheon，保留代码 |
| `AUBNE` | `AUBNE` | Brisbane，保留代码 |
| `USMOB` | `USMOB` | Mobile，保留代码 |
| `JPYOK` | `JPYOK` | Yokohama，保留代码 |
| `VNHPH` | `VNHPH` | Haiphong，保留代码 |
| `FRFOS` | `FRFOS` | Fos-sur-Mer，保留代码 |
| `ESBCN` | `ESBCN` | Barcelona，保留代码 |

**规则**：五字母 UN/LOCODE 是国际标准，直接保留大写，**不要**转换为城市全名（不同系统的映射不一致，交给下游）。

### 4. 换行、备注

原文可能带换行/备注（如 `TRIESTE\n2/12 截关`），只取港口名：

| 原文 | 输出 |
|------|------|
| `TRIESTE\n2/12 截关` | `TRIESTE` |
| `SANTOS**` | `SANTOS` |
| `HAMBURG（周三开）` | `HAMBURG` |
| `TACOMA（周1、5）` | `TACOMA` |

### 5. 路由标记

有些港口带 `-S` `-N` 后缀表示航线方向或副港：

| 原文 | 输出 | 说明 |
|------|------|------|
| `VALENCIA-S` | `VALENCIA-S` | 保留，可能表示某航线 |
| `BUSAN(D. CUSTOMER)` | `BUSAN` | 去括号 |

**规则**：明显是航线/客户类型的括号内容去掉；`-S`/`-N`/`-E`/`-W` 保留（可能是港口分区）。

### 6. 起运港中文简写

托书起运港常为中文：

| 中文 | 输出 |
|------|------|
| 上海 | `SHANGHAI` |
| 宁波 | `NINGBO` |
| 青岛 | `QINGDAO` |
| 深圳 / 盐田 | `SHENZHEN` 或 `YANTIAN`（保留原文对应） |
| 蛇口 | `SHEKOU` |
| 天津 / 新港 | `TIANJIN` |
| 大连 | `DALIAN` |
| 厦门 | `XIAMEN` |
| 广州 / 南沙 | `NANSHA` 或 `GUANGZHOU` |
| 香港 | `HONG KONG` |

## 中转港"见设"规则

原文出现以下任一，`transit_port` 输出 `null`：

- `见设`
- `见设备`
- `见设备单`
- `见设备交接单`
- `见 EIR`
- `进港代码请参照设备交接单`
- `设备交接单为准`
- 空 / `-` / `/`

## 常见混淆

| 相似港口 | 处理 |
|---------|------|
| `SANTOS`（巴西）vs `SANTOS DE GUADIANA`（西班牙） | 一般语境下 SANTOS = 巴西桑托斯 |
| `NEW YORK` vs `NEWARK` | 分开，不合并 |
| `LOS ANGELES` vs `LONG BEACH` | 分开 |
| `RIO DE JANEIRO` vs `RIO GRANDE` | 分开 |
| `LAZARO CARDENAS` | 完整保留，不缩写 |

## 未知港口

原文完全无法匹配已知模式：

- 保留原文，去除明显噪声（换行/备注符号/中文）
- 全部大写
- 在 `raw_text_snippet` 保留原始写法
- 不要臆造对应代码
