# 自由文本创建订单接口 Postman 测试文档

## 1. 接口信息

| 项目 | 值 |
|---|---|
| 方法 | `POST` |
| URL | `{{base_url}}/orders` |
| Content-Type | `application/json` |
| 鉴权 Header | `X-API-Key: {{api_key}}` |
| 本地 Swagger | `{{base_url}}/docs` |

> 注意：此接口会真实调用当前配置的订单创建接口。项目本地 `.env` 当前指向预发布环境，
> Postman 每点击一次 Send 都可能创建一张订单。接口不会自动重试；结果不明确时不要直接
> 重复发送，应先按提单号到 TMS 查询。

## 2. Postman 环境变量

在 Postman 新建 Environment，添加：

| 变量 | Initial value / Current value |
|---|---|
| `base_url` | `http://127.0.0.1:8080` |
| `api_key` | 项目 `.env` 中的 `API_KEY` |

本地启动服务：

```bash
make dev
```

## 3. 创建请求

在 Postman 中选择 `Body` -> `raw` -> `JSON`，输入：

```json
{
  "content": "门点地址：浙江省嘉兴市嘉善县姚庄镇利群路269号；做箱时间：7月20日；船名航次：ZHONG GU YING KOU V.2605N；提单号：KMTCSHAP950393；箱型箱量：1*20RF；托运人/公司名称：海丰；目的港：BUSAN；中转港代码：KRPUS；开港时间/开航时间：2026-07-26",
  "roomId": "ewewdsdw121"
}
```

- `content`：需要解析并下单的自由文本。
- `roomId`：上游链路标识，接口原样回传，并原样传入订单创建接口顶层 `roomId`。

请求 Headers：

| Key | Value |
|---|---|
| `Content-Type` | `application/json` |
| `X-API-Key` | `{{api_key}}` |

## 4. 样例字段映射

接口只解析明确出现的标签，不调用 LLM，不补全、不推断、不归一化字段值。

| 原文标签和值 | 下单字段 | 实际值 |
|---|---|---|
| `门点地址：浙江省...269号` | `data.factory_bei` | `浙江省嘉兴市嘉善县姚庄镇利群路269号` |
| `做箱时间：7月20日` | `data.driver[0].b_date` | `7月20日` |
| `船名航次：ZHONG GU YING KOU V.2605N` | `data.b_ship_name` | `ZHONG GU YING KOU` |
| 同上 | `data.b_ship_num` | `2605N` |
| `提单号：KMTCSHAP950393` | `data.order_num1` | `KMTCSHAP950393` |
| 同上 | `data.data[0].b_order_num` | `KMTCSHAP950393` |
| `箱型箱量：1*20RF` | `data.box[0]` | `{"b_type":"20RF","box_num":1}` |
| `托运人/公司名称：海丰` | `data.c_title` | `海丰` |
| `目的港：BUSAN` | `data.b_end_dock` | `BUSAN` |
| `中转港代码：KRPUS` | `data.b_end_port` | `KRPUS` |
| `开港时间/开航时间：2026-07-26` | `data.b_open_ship_time` | `2026-07-26` |

`data.type` 固定为 `1`，`data.c_id` 来自服务端 `ORDER_API_JXT_OPEN_ID`。

> 日期格式注意：按“原样传值”的要求，`做箱时间：7月20日` 会直接传为
> `driver[0].b_date="7月20日"`。订单接口文档声明该字段格式为 `YYYY-MM-DD`；如果下游
> 严格校验，它可能返回业务失败。本接口不会擅自补成 `2026-07-20`。

## 5. 实际下游请求结构

服务端最终向订单接口发送：

```json
{
  "apiKeyInfo": {
    "ext_app_id": "<ORDER_API_EXT_APP_ID>",
    "ext_user_id": "<ORDER_API_EXT_USER_ID>",
    "jxt_open_id": "<ORDER_API_JXT_OPEN_ID>",
    "order_info": [
      {"test": 1}
    ]
  },
  "data": {
    "order_num1": "KMTCSHAP950393",
    "type": 1,
    "c_id": "<ORDER_API_JXT_OPEN_ID>",
    "c_title": "海丰",
    "b_ship_name": "ZHONG GU YING KOU",
    "b_ship_num": "2605N",
    "factory_bei": "浙江省嘉兴市嘉善县姚庄镇利群路269号",
    "b_end_port": "KRPUS",
    "b_end_dock": "BUSAN",
    "b_open_ship_time": "2026-07-26",
    "data": [
      {"b_order_num": "KMTCSHAP950393"}
    ],
    "box": [
      {"b_type": "20RF", "box_num": 1}
    ],
    "driver": [
      {"b_date": "7月20日"}
    ]
  },
  "userId": "<ORDER_API_USER_ID>",
  "roomId": "ewewdsdw121"
}
```

## 6. 成功响应

HTTP `200`：

```json
{
  "roomId": "ewewdsdw121",
  "source_fields": {
    "做箱时间": "7月20日",
    "提单号": "KMTCSHAP950393"
  },
  "extracted": {},
  "order_data": {},
  "upstream": {
    "code": "200",
    "msg": "添加成功",
    "data": [
      {
        "sn": "EX26040001",
        "sns": "EX26040001-1"
      }
    ]
  },
  "meta": {
    "extractor": "explicit_labels",
    "value_mode": "verbatim"
  }
}
```

- `source_fields`：原文标签和值，用于核对输入是否原样解析。
- `extracted`：映射后的独立订单抽取结构。
- `order_data`：实际发送给下游的 `data` 部分。
- `upstream`：订单创建接口的原始业务响应。

## 7. Postman Tests 脚本

在 Postman 的 `Scripts` -> `Post-response` 中粘贴：

```javascript
pm.test("HTTP status is 200", function () {
  pm.response.to.have.status(200);
});

const body = pm.response.json();

pm.test("roomId is returned unchanged", function () {
  pm.expect(body.roomId).to.eql("ewewdsdw121");
});

pm.test("order API accepted the order", function () {
  pm.expect(body.upstream.code).to.eql("200");
});

pm.test("source values are preserved", function () {
  pm.expect(body.source_fields["做箱时间"]).to.eql("7月20日");
  pm.expect(body.source_fields["船名航次"]).to.eql("ZHONG GU YING KOU V.2605N");
});

pm.test("required order fields are mapped", function () {
  pm.expect(body.order_data.order_num1).to.eql("KMTCSHAP950393");
  pm.expect(body.order_data.c_title).to.eql("海丰");
  pm.expect(body.order_data.data[0].b_order_num).to.eql("KMTCSHAP950393");
  pm.expect(body.order_data.box[0]).to.deep.eql({b_type: "20RF", box_num: 1});
});
```

## 8. 错误响应

### 缺少必填字段

缺少 `提单号` 或 `托运人/公司名称` 时返回 HTTP `422`，且不会调用下单接口：

```json
{
  "error": {
    "code": "order_not_ready",
    "message": "text does not contain all required order fields",
    "details": {
      "missing_fields": ["order_num1"]
    }
  }
}
```

### 其他状态

| HTTP 状态 | error.code | 说明 |
|---|---|---|
| `401` | `unauthorized` | `X-API-Key` 缺失或错误 |
| `422` | `order_not_ready` | 文本缺少下单必填字段 |
| `502` | `order_upstream_error` | 订单接口网络异常、非 2xx 或业务 code 非 `200` |
| `503` | `order_api_not_configured` | 服务端下单账号配置不完整 |

订单接口返回 `code="204"` 时，本接口会返回 `502 order_upstream_error`，并在
`details.upstream_code/upstream_message` 中保留业务错误信息。
