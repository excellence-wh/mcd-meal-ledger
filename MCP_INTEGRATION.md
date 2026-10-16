# MCP_INTEGRATION.md

本文说明「麦门账本」实际接入的麦当劳 MCP 能力：用了哪个 Server、哪些 Tool、
调用流程是怎样的、以及每一路调用在业务上解决了什么问题。

**本文档的所有字段结构、参数要求与编码映射均来自 2026-10-16 的真实联调结果
（使用真实 Token 调用 `mcp.mcd.cn`），不是推测。** 联调期间发现并修复了 5 个
会导致项目静默失效的问题，见第五节。

---

## 一、接入的 MCP Server

| 项目 | 值 |
|---|---|
| 名称 | 麦当劳中国 MCP 服务（mcd-mcp） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| Token 领取 | [open.mcd.cn/mcp](https://open.mcd.cn/mcp) 手机号登录 → 控制台 → 激活 |
| 限流 | 每 Token 600 次/分钟，超限返回 `429` |
| 官方文档 | [M-China/mcd-mcp-server](https://github.com/M-China/mcd-mcp-server) |

真实联调补充确认：

- **握手成功**，`serverInfo` 返回 `{"name": "mcd-mcp", "version": "1.0.0"}`；
- **服务端当前实际开放 35 个 Tool**（官方文档列的是 33 个，多出
  `query-promotions`、`query-survey-coupon`，且 `query-party-store-session`
  的实际名称与文档中 `query-partystore-session` 不一致）；
- ⚠️ **服务端不返回 `Mcp-Session-Id` 响应头**。这一点直接影响了客户端实现，见第五节问题 ③。

本项目**没有引入任何 MCP SDK**，而是在 `mcledger/mcp_client.py` 中用 Python 标准库
手写了一个极简但完整的 Streamable HTTP 客户端。原因有三：

1. **零依赖承诺** —— 任何人 clone 下来就能跑，不需要 `npm install` / `pip install`；
2. **协议面很窄** —— Streamable HTTP 的本质就是「POST 一个 JSON-RPC，
   可能回 JSON 也可能回 SSE 流」，手写反而比引入依赖更可控；
3. **可审计** —— 只有把调用链写明白，这份文档才讲得清楚。

客户端已实现的协议细节：

- `initialize` 握手，用独立的 `_initialized` 标记（而不是 session id）判断是否已握手；
- 响应体同时兼容 `application/json` 与 `text/event-stream`（逐行解析 `data:` 帧）；
- 结果解析优先级：`structuredContent` → `content[].text`（再尝试 JSON 反序列化）→ 原始 `result`；
- **识别业务层错误**：麦当劳 MCP 的业务错误是 `HTTP 200` + `{success: false, code, message}`。
  如果不显式识别，会被当成「调用成功但没数据」而静默丢掉；
- `401` 抛出明确的鉴权错误并提示重新领取 Token，`429` 自动指数退避重试；
- 客户端侧主动限速（最小请求间隔 120ms），从源头避免把自己撞上限流。

---

## 二、实际使用的 Tool

本项目共调用 **9 个**麦当劳 MCP Tool，覆盖「订单 → 补全 → 券 → 菜单 → 积分 → 活动」六条数据线。

| # | Tool | 用途 | 在本项目中的业务价值 |
|:-:|---|---|---|
| 1 | `order-list` | 查询历史订单 | **整个 Skill 的数据地基**。但只返回实付金额，必须配合 #2 |
| 2 | `query-order` | 查询订单详情（逐单） | **优惠审计的唯一数据源**。只有它返回原价、优惠金额与用券明细 |
| 3 | `available-coupons` | 麦麦省可领取的券 | 找出「可领未领」的券，这是最容易补救的一类漏洞 |
| 4 | `query-my-coupons` | 账户已有券 | 判断「有券未用」 |
| 5 | `query-store-coupons` | 门店专属券 | 门店券最容易被忽略，**少查这一路，用户就少省钱** |
| 6 | `query-meals` | 门店可售餐品菜单 | 提供餐品编码与标签（注意：它**不返回品类与名称**，见问题 ②） |
| 7 | `query-my-account` | 积分账户 | 展示可用 / 临期 / **已过期作废**积分 |
| 8 | `campaign-calendar` | 当月活动日历 | 说明当下有哪些活动可以和券搭配着用 |
| 9 | `list-nutrition-foods` | 官方营养表 | 营养画像与「按热量筛搭配」的唯一数据源 |

### 选型说明：为什么是这 8 个

麦当劳 MCP 实际开放 35 个 Tool。本项目**刻意只用了 8 个**，理由：

- **写操作的克制**：`create-order` / `cancel-order` / `auto-bind-coupons` / `draw-lottery`
  都会真实改变用户账户状态。一个「读历史、做审计」的工具没有理由在用户没明确要求时
  替他下单、领券或抽奖。因此这些 Tool **默认一律不调用**。
- **`auto-bind-coupons` 是唯一的例外**，且由 `--bind-coupons` 参数显式开启，
  调用前会在文档与 Skill 中双重提示「会产生实际影响」。
- **用不到的 Tool 不接**：`list-nutrition-foods` 的营养数据本项目只用到订单行里
  自带的热量字段；`mall-*` 系列商城能力与「消费审计」主题无关，故不接入。

### 一个关键经验：券必须从三路查全

```
可用的券 = 麦麦省可领取券（available-coupons）
         ∪ 我账户已有的券（query-my-coupons）
         ∪ 当前门店专属券（query-store-coupons，需 storeCode + orderType + beType）
```

**三路是并集，不是替代关系。少查任何一路，审计结论就会漏报，用户就少省钱。**

---

## 三、真实调用流程

### 3.1 时序

```
                       麦门账本 (mcledger)
                              │
  ┌───────────────────────────┼───────────────────────────┐
  │  ① 握手                                                 │
  │  initialize ──────────────▶│ mcp.mcd.cn                │
  │  （服务端不返回 session id，客户端用本地标记避免重复握手）  │
  │                                                        │
  │  ② 取订单                                               │
  │  order-list ──────────────▶ 历史订单（仅实付金额）        │
  │                                                        │
  │  ③ 逐单补全 ★关键★                                       │
  │  query-order × N ─────────▶ 原价 / 优惠金额 / 用券 / 单价  │
  │                                                        │
  │  ④ 取券（三路并集）                                      │
  │  available-coupons        ──▶ 可领券                    │
  │  query-my-coupons         ──▶ 账户券                    │
  │  query-store-coupons      ──▶ 门店券（需 3 个必填参数）    │
  │                                                        │
  │  ⑤ 菜单 / 积分 / 活动                                    │
  │  query-meals / query-my-account / campaign-calendar     │
  └───────────────────────────┬───────────────────────────┘
                              ▼
  ┌───────────────────────────────────────────────────────┐
  │  ⑥ 归一化  models.parse_orders / parse_coupons         │
  │     宽容取值 + 渠道编码映射 + 套餐拆解 + 关键词品类识别    │
  └───────────────────────────┬───────────────────────────┘
                              ▼
  ┌───────────────────────────────────────────────────────┐
  │  ⑦ 确定性计算（全部在本地，不经过模型）                  │
  │     analytics.compute_metrics  ──▶ 统计画像             │
  │     personas.evaluate_personas ──▶ 消费人格             │
  │     audit.run_audit            ──▶ 优惠 + 积分审计       │
  └───────────────────────────┬───────────────────────────┘
                              ▼
  ┌───────────────────────────────────────────────────────┐
  │  ⑧ 表达  report.render_html ──▶ 自包含 HTML 年报        │
  └───────────────────────────────────────────────────────┘
```

### 3.2 真实联调的实际请求序列

```
[mcp] order-list({"pageNo": 1, "pageSize": 200})           ← 一次拿到全部
[mcp] query-order({"orderId": "1030766560000567244…"})     ← 逐单补全
[mcp] query-order({"orderId": "1030711850000567244…"})
[mcp] available-coupons({})                                ← 7 张可领
[mcp] query-my-coupons({"page": "1", "pageSize": "200"})
[mcp] query-store-coupons({"storeCode": "1960183", "orderType": 1, "beType": 1})
[mcp] query-meals({"storeCode": "1960183", "orderType": 1, "beType": 1})
[mcp] query-my-account({})
[mcp] campaign-calendar({})
```

设计上的几个取舍：

- **每一路调用都独立 try/except 降级**。任意一个 Tool 失败（例如某门店查不到券），
  只会往 `bundle.warnings` 里追加一条提示，**不会中断整份年报的生成**。
  宁可少一个板块，也不能让用户什么都拿不到。
- **订单补全有上限**（默认 200 单），避免历史极多的账号把请求数放大到不可控。
- **客户端限速**（最小间隔 120ms）内置在 `_throttle()` 中，调用方不需要关心限流。

---

## 四、真实字段结构（联调实测）

### 4.1 统一响应外壳

所有 Tool 都返回同一层包装。这一层必须在取数时剥掉：

```json
{
  "success": true,
  "code": 200,
  "message": "请求成功",
  "datetime": "2026-10-16 14:39:16",
  "traceId": "90e2c792be3d6c30588d514d7b994589",
  "data": { }
}
```

失败时 `success` 为 `false`，`message` 里带官方说明 —— 这一点在联调时帮了大忙，
因为**渠道编码映射表就是从错误信息里得到的**（见 4.4）。

### 4.2 `order-list` —— 只有实付，没有原价

```json
{
  "orderId": "1030766560000567244392805855",
  "orderType": "1",
  "beType": "1",
  "storeCode": "1960183",
  "storeName": "麦当劳天门万达广场餐厅",
  "createTime": "2026-01-01 15:47:26",
  "orderStatus": "订单已完成",
  "orderProductList": [
    { "productCode": "520689", "productName": "小杯鲜萃咖啡", "quantity": 1, "comboItemList": [] }
  ],
  "realTotalAmount": "10"
}
```

**注意三件事**：

1. 明细的键名是 `orderProductList`（不是 `items` / `productList`）；
2. 金额只有 `realTotalAmount`（实付），**没有原价、没有优惠金额**；
3. 明细里**没有单价**。

这三点合起来意味着：**只靠 `order-list`，每笔订单的「省了多少」恒等于 0，
整个优惠审计引擎会静默失效。** 必须用 `query-order` 补全。

### 4.3 `query-order` —— 全量定价信息

```json
{
  "orderId": "1030711850000567244682228887",
  "storeName": "麦当劳天门万达广场餐厅",
  "orderProductList": [
    {
      "productName": "精选单人餐三件套",
      "quantity": 1,
      "price": "36.5",
      "comboItemList": [
        { "itemName": "新升级板烧鸡腿堡", "itemQuantity": 1 },
        { "itemName": "中薯条", "itemQuantity": 1 },
        { "itemName": "鲜萃冰咖", "itemQuantity": 1 }
      ]
    }
  ],
  "totalAmount": "36.5",          // 原价
  "realTotalAmount": "36.5",      // 实付
  "totalDiscountAmount": "0",     // 优惠金额
  "couponList": [],               // 用券明细
  "realDeliveryPrice": "0",
  "takeWay": "外带"
}
```

关键点：

- `totalAmount` 才是原价，`realTotalAmount` 是实付，两者相减才是「省下的钱」；
- 套餐行通过 `comboItemList` 暴露子项（`itemName` + `itemQuantity`），**子项没有独立价格**；
- ⚠️ **`query-order` 不返回 `orderType` / `beType` / `storeCode`** ——
  渠道信息只在 `order-list` 里。因此补全时必须做**字段合并**，不能直接用详情覆盖列表项。

### 4.4 渠道编码表（来自官方 inputSchema，权威）

真实接口返回的是**数字编码**，不是中文文案。映射关系直接取自官方
Tool `description`（也出现在错误响应里）：

| 字段 | 取值 |
|---|---|
| `beType` | `1` 到店取餐 ・ `2` 麦乐送到家 ・ `5` 得来速(DT) ・ `6` 企业团餐 |
| `orderType` | `1` 到店（含到店自取 + 得来速车道取餐） ・ `2` 外送（含麦乐送 + 企业团餐） |

`beType` 更精确，因此**优先使用 `beType`，`orderType` 作为兜底**。

### 4.5 其他 Tool 的真实结构

**`available-coupons`** —— `data` 直接是数组，字段非常少：

```json
[
  { "couponName": "中薯条买一送一", "couponImage": "https://…", "couponStatus": "CAN_GET", "label": "可领取" }
]
```

没有 `couponId`、没有面额、没有有效期。因此本项目对券只做「数量与名称」层面的审计，
不虚构面额。

**`query-my-coupons`** —— 列表在 `data.coupons[]`（不是 `data.list`）。

**`query-my-account`** —— 积分字段（注意官方拼写就是 `Mouth`）：

```json
{
  "availablePoint": "46.5",
  "accumulativePoint": "61.5",
  "usedPoint": "0",
  "expiredPoint": "15",              // 已过期作废 —— 已实现的净损失
  "frozenPoint": "0",
  "currentMouthExpirePoint": "0",    // 本月即将过期 —— 待实现的损失
  "nextMouthExpirePoint": "0",
  "currency": "麦享会积分"
}
```

**`campaign-calendar`** —— `data.dailyList[].events[]`，每个日期节点带 `today` 标记。
⚠️ 标题**不在** `activityTitle`（恒为空），而在 `articleDto.title`：

```json
{
  "date": "2026-10-16",
  "today": true,
  "dateText": "2026年10月16日 今日",
  "events": [
    {
      "activityCode": "45f3", "activityStage": 3, "eventType": 4, "price": "0",
      "activityTitle": "",                        // ← 恒为空，不能只用这个
      "articleDto": {
        "title": "超值𝟗.𝟗元早餐两件套陪你开工啦😋",
        "highlights": "9.9元超值早餐正式上新!",
        "buttonText": "立即领券"
      }
    }
  ]
}
```

**`query-meals`** —— ⚠️ 只返回 `data.categories[].meals[].code` 与 `tags`，
**既没有餐品名称，也没有品类名**。

---

## 五、联调发现并修复的 5 个问题

这一节是本项目最有价值的部分 —— 它们都是**只在真实数据上才会暴露**的问题，
脱敏样例永远测不出来。修复后已固化为 `scripts/selftest.py` 中的
「真实 MCP 契约测试」区块，防止回归。

### 问题 ① · 订单明细键名不匹配（严重）

| | |
|---|---|
| **现象** | 真实数据的明细键是 `orderProductList`，而别名表里只有 `items` / `products` / `productList` 等 |
| **后果** | 订单明细整体解析为空 → 常点榜单、品类画像、套餐拆解**全部失效**，而且不报错 |
| **修复** | 将 `orderProductList` 加入明细键别名表 |

### 问题 ② · 实付字段不匹配 + 原价缺失（严重）

| | |
|---|---|
| **现象** | 真实实付字段是 `realTotalAmount`（不在别名表里）；`order-list` 也不返回原价与优惠 |
| **后果** | 实付被算成 0；即使不补全，`saved` 也恒为 0 → **整个优惠审计引擎静默失效** |
| **修复** | 加入 `realTotalAmount` / `totalDiscountAmount` 别名；并新增 `query-order` 逐单补全步骤 |

这是本项目最危险的一个坑：**它不会报错，只会让你拿到一份「看起来很完整、
但其实什么都没算出来」的报告。**

### 问题 ③ · 服务端不返回 session id，导致重复握手

| | |
|---|---|
| **现象** | 麦当劳 MCP 不在响应头里返回 `Mcp-Session-Id` |
| **后果** | 客户端原逻辑是「没有 session id 就重新握手」，导致**每次 `call_tool` 都多发一次 `initialize`**，请求量翻倍 |
| **修复** | 改用独立的 `_initialized` 布尔标记，而不是拿 session id 当哨兵 |

### 问题 ④ · 两个 Tool 的参数必填项被漏掉

| | |
|---|---|
| **现象** | `query-meals` 与 `query-store-coupons` 的官方 schema 要求 `storeCode` + `orderType` + `beType` **三个全部必填**，原实现只传了 `storeCode` |
| **后果** | 两次调用都返回 `400`，门店券与菜单完全拿不到 |
| **修复** | 补齐三个参数；`storeCode` 从订单的 `storeCode` 推导，`orderType` / `beType` 取历史订单里的众数，缺省为 `1` / `1` |

另外顺带发现：`query-my-coupons` 的分页参数是 `page` / `pageSize`（字符串），
不是 `pageNo`；`order-list` 的 `inputSchema` **没有任何参数**，
传 `pageNo` / `pageSize` 会被忽略并重复返回同一批数据。

### 问题 ⑤ · 活动标题字段恒为空

| | |
|---|---|
| **现象** | `campaign-calendar` 里 `activityTitle` 在真实数据中**恒为空字符串**，真正的标题在 `articleDto.title`（其次是 `articleDto.highlights`） |
| **后果** | 活动板块静默变成空白 —— 一个"看起来接口调成功了"的空卡片 |
| **修复** | 标题按 `activityTitle` → `articleDto.title` → `articleDto.highlights` → `activitySubTitle` 依次回退；同时只在 `today = true` 的日期节点里取，避免把「往期回顾」当成当前活动 |

真实数据实测：修复后能从 15 个当日活动中正确抽出标题，例如
「麦当劳联动G-DRAGON」「超值𝟗.𝟗元早餐两件套陪你开工啦😋」
「🫐一口爆汁！蓝莓爆爆珠麦旋风上新」。

### 由问题 ② 派生的两个设计决定

**A. 逐单补全成为核心步骤。** `fetch_bundle()` 会在拿到订单列表后，
对每笔订单调用一次 `query-order`，把详情合并进列表项。
合并策略是「详情为主体，从列表项补齐详情缺失的渠道字段」——
因为 `query-order` 不带 `orderType` / `beType` / `storeCode`。

**B. 品类识别改用确定性关键词规则。** 由于 `query-meals` 不返回品类名、
`query-meal-detail` 只给单个餐品的特制选项，订单行里根本拿不到 category。
因此 `models.classify_item()` 用一组可测试的关键词规则判定品类
（咖啡 / 甜品 / 早餐 / 汉堡 / 小食 / 饮料），**完全不依赖模型判断**。

### 顺带增强：套餐拆解

真实数据里 `精选单人餐三件套` 这类套餐很常见，只统计套餐名对用户毫无意义。
因此新增套餐拆解：`comboItemList` 的子项会进入「套餐拆解后的单品榜」，
**金额仍归套餐行，不对子项分摊**（子项没有独立价格，硬分摊就是造假数据）。

---

## 六、数据归一化策略

| 处理点 | 做法 |
|---|---|
| 响应外壳 | 自动剥离 `{success, code, message, data}` |
| 字段别名 | 每个语义字段维护别名列表，按序尝试 |
| 嵌套路径 | 支持 `store.name` 这类浅层路径，兼容门店字段是对象还是字符串 |
| 响应形态 | 自动识别裸数组 / `{data:[…]}` / `{data:{list:[…]}}` / `{data:{coupons:[…]}}` |
| 金额脏数据 | `"¥25.5元"`、`"1,234.50"` 等一律可解析为 float |
| 时间脏数据 | 兼容 ISO8601、`Y-m-d H:i:s`、`Y/m/d`、秒级 / 毫秒级时间戳 |
| 渠道 | 数字编码（`beType` > `orderType`）优先，中文 / 英文文案兜底 |
| 套餐 | `comboItemList` 拆解为子项，只计数量不计金额 |
| 品类 | 关键词规则确定性判定，不依赖接口返回 category |

**金额归属规则（容易踩坑，单独说明）**：

原价（`gross`）取「订单明细求和」与「服务端原价字段」中的**较大者**。

- 情况 A：明细是原价，服务端的 `totalAmount` 其实是应付价（较低）→ 取明细求和，正确；
- 情况 B：明细被截断，服务端的 `totalAmount` 才是完整原价（较高）→ 取服务端字段，避免漏报优惠。

两种常见数据形态都能得到正确结果，且偏向「宁可高估原价」——因为高估原价最多让
「省了多少」偏乐观，而低估原价会让用户误以为自己没省到钱。

实付（`paid`）**只认明确的应付字段**，绝不把订单总额误当实付。

---

## 七、业务价值

### 真实联调产出（本人账号实测）

```
订单 2 单 · 实付 46.50 元 · 优惠利用率 0%
积分 46.5 可用 · 0 即将过期 · 15 已过期作废
```

审计结论：

> **全部订单均为原价** —— 2 单全部按原价结算，合计 ¥46.50，
> 历史记录中查不到任何优惠使用痕迹。因为还没有可参照的折扣水平，
> 这里不估算金额 —— 但可领的券是现成的。
>
> **有 7 张券可领但未领取** —— 券躺在券池里不会自己跳到你的账户，
> 需要在点餐前领取才会生效。
>
> **已有 15 积分过期作废** —— 积分属于会自然损耗的资产，
> 放着不用就是净损失，且过期后无法追回。

这三条结论全部由确定性代码算出，用户能核对、能复现。
其中「已过期作废积分」这条尤其有意思：**它是唯一一类「已经发生、且不可挽回」的损失**，
而以往没有任何工具会提醒用户。注意本项目在缺少可参照折扣水平时
**明确拒绝给出金额估算** —— 宁可少说，也不编一个数字。

### 为什么这件事必须调 MCP

| 能力 | 没有 MCP 会怎样 | 有了 MCP 之后 |
|---|---|---|
| 历史订单 | 用户只能靠记忆和零散截图，无法统计 | 真实、完整、可审计的消费账本 |
| 订单详情 | 只有实付金额，算不出省了多少 | 逐单补全原价与优惠，审计才成立 |
| 三路券池 | 券散落在麦麦省、账户、门店三处，用户根本不知道有多少 | 并集拉全，才能算出「有几张券可以领但没领」 |
| 门店菜单 | 无法把订单里的餐品归到品类 | 支撑「甜品治愈系」「咖啡常客」这类品类画像 |
| 积分账户 | 不知道有多少积分已过期、多少即将过期 | 把积分当资产审计：**已损失 / 待损失** 分开呈现 |
| 活动日历 | 报告只有过去，没有当下 | 补上「当前正在进行的活动」上下文 |

### 架构分工

| 角色 | 承担者 | 职责 |
|---|---|---|
| 取数 | 麦当劳 MCP | 实时的订单、券、菜单、积分、门店、活动 |
| 算术 | `mcledger` 包（纯标准库） | 确定性统计、人格判定、优惠与积分审计 |
| 表达 | WorkBuddy Agent | 理解需求、组织语言、确认后下单 |

**把「算钱」从大模型手里拿走，是这个项目最重要的一个决定。**

---

## 八、可验证性

- `scripts/selftest.py`：**261 项断言**，含专门的「真实 MCP 契约测试」区块 ——
  用真实联调抓回来的原始报文作为夹具，锁死 `orderProductList` / `realTotalAmount` /
  `beType` 等关键字段名，防止回归。
- `fixtures/` 内置脱敏样例数据（123 笔订单 + 8 张券），**没有 Token 也能完整跑通全流程**。
- 同一份输入产出**完全一致**的 HTML（已断言），不存在随机性。
- HTML 产物为自包含内联样式与内联 SVG，**不引用任何外部资源**（已断言）。

```bash
python scripts/selftest.py              # 261 passed, 0 failed
ruff check .                            # All checks passed!
mypy mcledger scripts                   # Success: no issues found in 12 source files
python scripts/run.py --offline         # 离线跑通完整年报
python scripts/fetch_snapshot.py        # 用真实 Token 抓数据快照
```

---

## 九、安全与合规

- 代码中**不包含任何真实 Token**；客户端只从环境变量 `MCD_MCP_TOKEN` 读取。
- `mcp-config.example.json` 仅使用 `${MCD_MCP_TOKEN}` 环境变量占位符。
- 从 MCP 拉取的真实数据快照写入 `fixtures/live/`，真实年报写入 `docs/mine.html`，
  两者均已在 `.gitignore` 中排除，**不会进入仓库**（已用 `git status --ignored` 验证）。
- 所有「估算可省」金额均标注「估算」字样，并说明以用户自身历史折扣水平为基准；
  当没有可参照的折扣水平时，**明确不估算金额**，只陈述事实。
- 不调用任何会改变用户账户状态的写操作 Tool（除用户显式开启的 `--bind-coupons`）。

---

*本项目为麦当劳程序员创意开发大赛参赛作品，由参赛者独立开发，非麦当劳官方产品。
餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准。*
