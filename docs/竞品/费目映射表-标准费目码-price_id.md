# 标准费目码 → TMS 费目/price_id 映射表

> 阶段二唯一外部依赖的落地件。**配置先行**：已实证的 5 个 price_id 直接填入，其余 `null` 占位——补值只改 YAML，不改代码。
> price_id 全局唯一、跨通道通用（EX26081252/EX26080031 双重实证），故一张表服务应收/应付/成本三通道。
> ⚠️ **按环境隔离**：测试/生产 price_id 大概率不同，必须分文件维护（`config/fee_price_map.{env}.yaml`）。

## 1. 配置文件（仓库路径 `config/fee_price_map.{env}.yaml`）

```yaml
# config/fee_price_map.test.yaml —— 测试环境（2026-08-13 起建）
# price_id: null = 待补/待建档（自举兜底见 §3）
freight:     { tms_name: 运费,     price_id: 820 }      # ✅ 实证（下单抓包+EX26081252）
pre_pick:    { tms_name: 预提费,   price_id: 121744 }   # ✅ 实证（跨通道同 id 复用实证）
fuel:        { tms_name: 油费,     price_id: 1134 }     # ✅ 实证（get/pay/supplier 三通道同现）
trip:        { tms_name: 出车费,   price_id: 821 }      # ✅ 实证
hijack:      { tms_name: 打劫费,   price_id: 1135 }     # ✅ 实证；竞品样本中未出现，先登记防长尾撞上
waiting:     { tms_name: 待时费,   price_id: null }
pre_inport:  { tms_name: 预进港,   price_id: null }
drop_box:    { tms_name: 落箱费,   price_id: null }     # 落/还箱费同目，名称以价格表为准
amend:       { tms_name: 打单费,   price_id: null }     # TMS 侧是「打单费」还是「打/改单费」待确认
yangshan:    { tms_name: 洋山费,   price_id: null }     # 洋山提/还若价格表分两项则拆码
damage_box:  { tms_name: 坏污箱费, price_id: null }
overdue:     { tms_name: 超期费,   price_id: null }
overweight:  { tms_name: 超重费,   price_id: null }
inspect:     { tms_name: 动检费,   price_id: null }
weigh:       { tms_name: 过磅费,   price_id: null }
tally:       { tms_name: 理货费,   price_id: null }
move:        { tms_name: 搬移费,   price_id: null }
lift:        { tms_name: 上下车费, price_id: null }
port_misc:   { tms_name: 港杂费,   price_id: null }
tax:         { tms_name: 税金,     price_id: null, import: false }  # 不录入仅对账，price_id 非必需
other:       { tms_name: 其它费,   price_id: null }     # ⚠️ to_other 归并目标；存在性+id 待确认/自举

# 费目自举（§3）：缺失费目码导入命中时懒创建（test=true / prod=false）
fee_bootstrap:
  enabled: true
  endpoint_key: price_create          # 引用 master_data.endpoints 键名（不重复配 URL）
  create_defaults:                    # 逆推规范 §8 最小字段集 + 抓包默认值
    sn_prefix: AUTO
    inout: "3"
    is_get: on
    is_pay: on
    price_type: "1"
    is_profit: "1"
    status: "1"
    dai_dian: "2"
    bao_zhang: "1"
    od: "0"
    use_imprest: "0"
    expense_rate: "5"
    class_id: "4612"                  # 费类=运费（测试环境实证）；其它类待费类表到位后细分
    classification_name: 运费
```

```yaml
# config/fee_price_map.prod.yaml —— 生产环境：整表 price_id 待生产价格表到位后填写
# 结构同上，tms_name 以生产价格表为准（名称也可能不同）；fee_bootstrap.enabled=false（默认）
```

## 2. 已确认 vs 待补

| 状态 | 费目 |
|---|---|
| ✅ price_id 已实证（5） | 运费=820、预提费=121744、油费=1134、打劫费=1135、出车费=821 |
| ⬜ 名称已推断、id 待补（15） | 待时/预进港/落箱/打单/洋山/坏污箱/超期/超重/动检/过磅/理货/搬移/上下车/港杂/税金 |
| ⚠️ 存在性待确认（1） | **其它费**——长尾 `to_other` 策略的归并目标，若 TMS 价格表无此目需先建档（自举 `is_other=1` 特判） |

**【2026-08-14 定论】测试环境价格表全量=7 费目**（费用区 DOM 实证，div class `price{id}` 直读）：运费820/预提费121744/油费1134/打劫费1135/出车费821 + 脏数据 1121=124827、测试=124823。**15 个标准费目与「其它费」在测试环境均不存在**——缺失性质从「待补 id」改为「待建档」。

## 3. 补值方法（任选其一，都不需要写代码）

1. **自举（已实施，2026-08-14 合入）**：`fee_bootstrap` 编排（fee_bootstrap.py + fee_registry.py），导入中命中且解析为 null 的费目码**懒创建**（AddCarPrice  建档）→ registry 登记 → 当批回填 price_id；幂等（registry 命中即复用）、失败降级下批重试、生产默认关闭。**preview 零副作用**：预览只输出 planned 计划清单不发建 档请求，真实导入（create_order=true）才建档。**golden 实证：秋怡 2019 全量 create 模式自举后 dropped 1901 → 0**；
2. **canonical other 原名建档（B2，2026-09-07 合入）**：标准字段链 to_other 归并费目的 note 原名（如「高速费」）→ create 时并入建档（动态码 `x+sha1[:8]`，name=原名）→ 费用管理出现同名档案；**订单 payload 零变更**（照常 other+note 提交，档案与订单费用无关联，财务按新档案统计为 0——如需迁移需 B3 写别名字典闭环）；幂等 registry/204 已存在则不再建；
3. **jinxin（BillRow 直传名）模板外建档（2026-09-07 合入）**：账单费用名中无已建档案者（别名命中且有 id 的跳过）create 时自动建档（`run_billrow_fee_bootstrap`）；preview 只出 planned；
4. **outerHTML 法（一次全量）**：TMS 费用弹窗 → 右键「费目」下拉框 → 检查 → `<select>` 的 `<option value="820">运费</option>` value 即 price_id（⚠️ 2026-08-14 实证费用区无下拉框，此法作废，改走价格表/客服导出）；
5. 价格表/费目管理界面导出或问金科信客服要价格表导出。

## 4. 运行期降级规则（fee_price_map.apply_price_map 实现）

- **解析顺序四级**：YAML 显式 id → registry（自举产物）→ 自举创建 → 降级 skip_report；YAML 与 registry 冲突时 **YAML 优先**（人工修正永远压过自动产物）；
- 某费目 `price_id: null`（自举未启用/建档失败）→ 该费目本条**不录入**，进对账报告（skip_report 语义），不阻塞整单；
- `other.price_id: null` 且出现未匹配费目 → 未匹配费目全部 skip_report + 显著 warning（提示先补其它费 id 或等自举）；
- 映射表加载失败/缺环境文件 → 启动即报错（fail fast），不允许裸跑。

## 5. 环境开关与运维

- `fee_bootstrap.enabled`：test=true / **prod=false**（生产价格表由运维管控，自举属越权行为，需显式开启后才会建档）；
- 自举产生的费目如命名/归类要调整 → TMS 界面直接改，registry 已记 id 不受影响（YAML 补 id 后 YAML 优先）；
- 注册表文件 `{storage_dir}/fee_registry.json` 为自动产物，勿手改；人工修正走 YAML。
