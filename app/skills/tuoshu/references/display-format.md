# 运输委托书展示格式约束

## 规则

当向用户展示解析结果时，严格按照以下格式输出。

## 核心原则

1. **展示层用中文标签**，内容与JSON字段对应，不输出JSON
2. **中文标签必须与 schema.md 中字段的"说明"列严格一致**，不得自由发挥
3. **不分区块**，直接按顺序列出有值的字段
4. **字段缺失时不显示该行**
5. **不输出 doc_type（文档类型）等标题性/元信息字段**
6. 拆装箱地址中的联系人、电话、地址信息合并为一行显示
7. 箱型箱量从 containers 数组中取第一个元素展示
8. 所有数值字段保持原数值，为0时显示"0"
9. 多柜时展示第一个柜的箱型箱量，其余在备注中说明

## 字段标签映射（严格按 schema.md 说明）

| JSON字段 | schema说明 | 展示标签 |
|---------|-----------|---------|
| customs_declaration_no | 关单号 / 我方业务编号 / 我司编号 | 关单号 |
| customer_ref | 客户编号 / PO / 客户订单号 | 客户编号 |
| mbl_no | 主提单号（Master B/L） | 主提单号 |
| hbl_no | 分提单号（House B/L） | 分提单号 |
| vessel | 船名，全大写 | 船名 |
| voyage | 航次 | 航次 |
| carrier | 船公司缩写 | 承运人 |
| pol | 起运港，英文大写 | 起运港 |
| pod | 目的港，英文大写 | 目的港 |
| transit_port | 中转港 | 中转港 |
| terminal | 港区/码头 | 港区 |
| etd | 预计开航日 / 船期 | 船期 |
| si_cutoff | 截 SI / 截单时间 | 截单时间 |
| customs_cutoff | 截关 / 截报关 | 截关时间 |
| loading_time | 做箱/装箱时间 | 做箱/装箱时间 |
| shipper_agent | 我方公司/发件方 | 委托公司 |
| recipient | 收件方（TO 字段） | 收件方 |
| doc_date | 文档日期（DATE 字段） | 日期 |
| sender | 发货方（FROM 字段） | 发货方 |
| sender_contact | 发货联系人 | 发货联系人 |
| remark | 汇总备注 | 备注 |
| containers[].type | 归一后的箱型 | 箱型 |
| containers[].qty | 该类型/该行的箱数 | 箱量 |
| containers[].container_no | 集装箱号 | 箱号 |
| containers[].seal_no | 铅封号 | 铅封号 |
| containers[].packages | 件数 | 件数 |
| containers[].packages_unit | 件数单位 | 件数单位 |
| containers[].gross_weight_kg | 毛重（KG） | 毛重 |
| containers[].volume_cbm | 体积（CBM） | 体积 |
| containers[].po_no | 客户 PO 号 | PO号 |
| containers[].mbl_no | 该柜对应的提单号 | 提单号 |
| containers[].remark | 该柜专属备注 | 箱型备注 |
| factory.name | 工厂名称 | 发货单位 |
| factory.address | 详细地址 | 提货联系人及地址 |
| factory.contact | 联系人姓名 | 联系人 |
| factory.phone | 联系电话 | 电话 |

## 输出示例

```
委托公司：上海秉晟国际物流有限公司
客户编号：BSSE2108100016
日期：2021-08-10
收件方：上海运嘉货运代理有限公司
主提单号：1KT251889
船名：MAERSK HAMBURG
航次：131W
起运港：SHANGHAI
目的港：PIVDENNYI
箱型：20GP
箱量：1
船期：2021-08-17
提货联系人及地址：江苏省苏州市太仓市郑和中路4号
联系人：曹小姐
电话：0512-81602075
做箱/装箱时间：2021-08-13 07:00
件数：0
毛重：0
体积：0
```
