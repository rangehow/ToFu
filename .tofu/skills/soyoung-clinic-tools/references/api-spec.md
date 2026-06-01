# 新氧青春诊所 — Skill API 规范（本地副本）

> 来源：ClawHub `east5ringroad-kyle/soyoung-clinic-tools` v2.2.2
> 适用：直接 HTTP POST，无 SDK，不依赖 OpenClaw runtime。

## 基础信息

| 项 | 值 |
|---|---|
| Base URL | `https://skill.soyoung.com` |
| Content-Type | `application/json; charset=utf-8` |
| 字符编码 | UTF-8 |
| 鉴权 | 请求体里的 `api_key` 字段（用户登录令牌） |

## 接口总览

| # | 功能 | 方法 | 路径 | 说明 |
|---|---|---|---|---|
| 1 | 门店查询 | POST | `/booking/skill/query/store` | 按城市查询门店列表 |
| 2 | 预约切片查询 | POST | `/booking/skill/query/booking_slice` | 按门店 + 日期查询可约切片 |
| 3 | 我的预约（按日期聚合） | POST | `/booking/skill/query/booking` | 当前用户的预约列表 |
| 4 | 提交预约 | POST | `/booking/skill/submit/booking` | 创建预约 |
| 5 | 修改预约 | POST | `/booking/skill/modify/booking` | 修改预约时间 |
| 6 | 取消预约 | POST | `/booking/skill/cancel/booking` | 取消预约 |
| 7 | 品项查询 | POST | `/project/skill/clinic_solution/search` | 项目知识库关键词搜索 |
| 8 | 商品查询 | POST | `/project/skill/clinic_product/search` | C 端商品关键词搜索 |
| 9 | 医生查询 | POST | `/project/skill/clinic_doctor/search` | 医生 / 门店医生 / 排班 |

## 公共字段

所有请求体都必须带：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `api_key` | string | ✓ | 用户登录令牌（`https://www.soyoung.com/loginOpenClaw`） |
| `request_id` | string | ✓ | 每次请求的唯一 ID，建议用 uuid4 |

`/booking/skill/*` 业务字段（按需带）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `city_name` | string | 城市名称 |
| `hospital_id` | long | 机构/门店 ID |
| `date` | string | 日期 `YYYY-MM-DD` |
| `start_time` | string | 预约开始 `YYYY-MM-DD HH:MM:SS` |
| `end_time` | string | 预约结束 `YYYY-MM-DD HH:MM:SS` |
| `booking_id` | long | 预约 ID（修改/取消用） |

`/project/skill/*` 业务字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `city_name` | string | 城市名称（商品/医生可选） |
| `content` | string | 关键词（实体词，如「肉毒」「保利店」） |

---

## 1) 门店查询

`POST /booking/skill/query/store`

```json
{
  "api_key": "...",
  "city_name": "北京",
  "request_id": "<uuid4>"
}
```

响应（`data` 是数组）：

```json
[
  {
    "机构ID": 11642747,
    "机构名称": "北京保利总部店",
    "营业时间": "09:00-15:30",
    "门店面积（平米）": 123,
    "累计服务人次": 0,
    "机构地址": "..."
  }
]
```

## 2) 预约切片查询

`POST /booking/skill/query/booking_slice`

```json
{
  "api_key": "...",
  "hospital_id": 11642747,
  "city_name": "北京",
  "date": "2026-03-17",
  "request_id": "<uuid4>"
}
```

响应（每天聚合，含 `切片明细` 数组）：

```json
[
  {
    "切片日期": "2026-03-20 00:00:00",
    "切片明细": [
      {
        "切片开始时间": "08:00:00",
        "切片结束时间": "09:00:00",
        "切片剩余库存": 19,
        "切片总库存": 19
      }
    ],
    "总库存": 19,
    "剩余库存": 19,
    "项目名称": "预约面诊"
  }
]
```

> 切片项目固定为「预约面诊」，**不要**传任何 `project` 参数。

## 3) 我的预约（按日期聚合）

`POST /booking/skill/query/booking`

```json
{
  "api_key": "...",
  "request_id": "<uuid4>"
}
```

响应：

```json
[
  {
    "日期": "2026-03-17",
    "日期名称": "今日",
    "当天已预约总数": 2,
    "当天预约明细": [
      {
        "预约ID": 13409,
        "基础品ID": 11116508,
        "基础品名称": "...",
        "机构ID": 15731,
        "机构名称": "...",
        "预约开始时间": "08:00",
        "预约结束时间": "09:00",
        "可预约子单（订单号）": "...",
        "顶级订单号": "...",
        "业务类型": 1,
        "是否无单预约（0/1）": 0
      }
    ]
  }
]
```

## 4) 提交预约

`POST /booking/skill/submit/booking`

```json
{
  "api_key": "...",
  "hospital_id": 11642747,
  "start_time": "2026-03-17 15:00:00",
  "end_time": "2026-03-17 16:00:00",
  "request_id": "<uuid4>"
}
```

响应：

```json
{ "失败原因": null, "预约ID": 13433 }
```

`失败原因 = null` 表示成功；非 null 时把原因展示给用户。

## 5) 修改预约

`POST /booking/skill/modify/booking`

```json
{
  "api_key": "...",
  "hospital_id": 11642747,
  "start_time": "2026-03-17 17:00:00",
  "end_time": "2026-03-17 18:00:00",
  "booking_id": 13433,
  "request_id": "<uuid4>"
}
```

## 6) 取消预约

`POST /booking/skill/cancel/booking`

```json
{
  "api_key": "...",
  "booking_id": 13434,
  "request_id": "<uuid4>"
}
```

## 7) 品项查询（项目知识库）

`POST /project/skill/clinic_solution/search`

```json
{
  "api_key": "...",
  "content": "肉毒",
  "request_id": "<uuid4>"
}
```

响应（数组，无数据返回 `[]`）：

```json
[
  {
    "品项id": 192,
    "品项名称": "国产肉毒",
    "核心原理": "...",
    "适应症": "...",
    "功效": "...",
    "术后护理": "..."
  }
]
```

> 接口可能返回更多字段（功效、适应症、术后护理、常见问题），按字面意思
> 转述给用户即可。

## 8) 商品查询（价格）

`POST /project/skill/clinic_product/search`

```json
{
  "api_key": "...",
  "city_name": "北京",
  "content": "肉毒",
  "request_id": "<uuid4>"
}
```

响应：

```json
[
  {
    "商品id": 11599670,
    "商品名称": "韩国品牌-50U",
    "商品数量": "1个",
    "使用产品信息": ["主使用产品:韩国进口, 规格:50单位"],
    "售卖价格": "999.0元",
    "到手价格": "999.0元"
  }
]
```

> 仅返回 C 端商品，不含好物商品类型。

## 9) 医生查询

`POST /project/skill/clinic_doctor/search`

带 `city_name`：

```json
{
  "api_key": "...",
  "city_name": "北京",
  "content": "保利店",
  "request_id": "<uuid4>"
}
```

不带 `city_name`（全国搜）：

```json
{
  "api_key": "...",
  "content": "唐碧莹",
  "request_id": "<uuid4>"
}
```

响应：

```json
[
  {
    "医生id": 4,
    "医生名称": "唐碧莹",
    "医生职称": "主治医师",
    "医生常驻门店": "新氧青春诊所(北京保利总部店) No.001",
    "医生所在城市": "北京市",
    "医生认证信息": "...",
    "医生排班信息": "日期 - 20260330（星期一）- 休息，日期 - 20260331（星期二）- 北京保利店11:00-20:00, ..."
  }
]
```

`医生排班信息` 是单字符串，里面用「，」分隔每天，建议解析后按日期渲染表格。
