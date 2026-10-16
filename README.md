<div align="center">

# 麦门账本 · McDonald's Meal Ledger

**把你的麦当劳消费记录，变成一份带审计结论的个人年报。**

不只是告诉你花了多少 —— 而是告诉你，钱漏在哪。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#为什么零依赖)
[![Tests: 169](https://img.shields.io/badge/tests-169%20passed-success.svg)](./scripts/selftest.py)
[![MCP: McDonald's](https://img.shields.io/badge/MCP-mcd.cn-DA291C.svg)](https://open.mcd.cn/mcp)

</div>

---

## 这是什么

**麦门账本**基于麦当劳 MCP 能力，读你的历史订单，用确定性算法算出三件事：

| | 普通统计会告诉你的 | 麦门账本多告诉你的 |
|---|---|---|
| **1. 事实** | 你在麦当劳花了 ¥4,177 | 123 单 / 覆盖 98 天 / 平均客单价 ¥33.96 / 整体折扣率 17.3% |
| **2. 画像** | 无 | ⏳ 资深麦门老友、🔥 连击王、🍦 甜品治愈系 —— 每个标签都附数据依据 |
| **3. 审计** | 无 | **26 单按原价结的，按你自己的折扣水平本可省约 ¥241.38（估算）** |

第 3 条是重点。**普通的年度报告只统计过去，麦门账本审计漏洞。**

> 示例输出（离线脱敏样例，可完全复现）：
>
> ```
> 这 123 单里有 26 单是按原价结的，合计实付 ¥1,095.20。
> 而你其余订单的平均折扣率是 22.0% ——
> 按你自己的这个水平，这 26 单本可省约 ¥241.38（估算）。
> ```

---

## 效果预览

打开 [`docs/demo.html`](./docs/demo.html) 即可看到完整年报（离线样例数据，无需 Token）。

年报包含：核心数字看板 → 麦门人格（带证据）→ 消费节奏图表（内联 SVG）→
常点榜单 → 账单审计（含优惠利用率评级）。

产物是**自包含 HTML**：内联样式 + 内联 SVG，零外部资源，
可以直接双击打开、直接截图分享，不会在别人手机上碎掉。

---

## 快速开始

### 环境要求

**Python 3.10+，不需要安装任何第三方依赖。**

### 第一步：先看效果（无需 Token）

```bash
git clone https://github.com/<你的用户名>/mcd-meal-ledger.git
cd mcd-meal-ledger
python scripts/run.py --offline --markdown
```

打开 `docs/demo.html`，你会看到一份基于 123 笔脱敏样例订单生成的完整年报。

### 第二步：生成你自己的年报

1. **领取 MCP Token** → 到 [open.mcd.cn/mcp](https://open.mcd.cn/mcp) 手机号登录，
   点右上角「控制台」→「激活」，一键复制 Token（免费）。

2. **设置环境变量并运行**：

```bash
# macOS / Linux
export MCD_MCP_TOKEN="你复制的Token"
python scripts/run.py --out docs/mine.html --markdown

# Windows PowerShell
$env:MCD_MCP_TOKEN="你复制的Token"
python scripts/run.py --out docs/mine.html --markdown
```

3. 打开 `docs/mine.html` —— 这是你自己的麦门账本。

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--offline` | 使用内置脱敏样例，不需要 Token |
| `--out PATH` | HTML 输出路径（默认 `docs/demo.html`） |
| `--markdown` | 在终端打印文字简报 |
| `--json-out PATH` | 同时导出中间结果 JSON |
| `--pages N` / `--page-size N` | 历史订单拉取页数与每页条数（默认 3 × 50） |
| `--store CODE` | 指定门店编码（影响门店券与菜单查询） |
| `--personas N` / `--top N` | 展示的人格标签数 / 常点榜单条数 |
| `--bind-coupons` | 调用一键领券（⚠️ 会真实写入账户，默认关闭） |
| `-v` | 打印每次 MCP 调用 |

---

## 在 WorkBuddy 中使用（Skill 形式）

本项目同时是一个标准 Skill，定义在 [`skills/mcd-meal-ledger/SKILL.md`](./skills/mcd-meal-ledger/SKILL.md)。

1. 打开 WorkBuddy → 左侧边栏【专家·技能·连接器】→【连接器】→ 右上角【自定义连接器】→【配置 MCP】
2. 粘贴 [`mcp-config.example.json`](./mcp-config.example.json) 的内容，
   把 `${MCD_MCP_TOKEN}` 换成你的真实 Token，保存并启用 `mcd-mcp`
3. 直接把 `skills/mcd-meal-ledger/` 目录作为 Skill 加载
4. 在对话框说：「**帮我看看我今年在麦当劳花了多少钱，亏在哪**」

Skill 会自动走完整链路：拉取订单与券 → 本地确定性计算 → 生成年报 → 讲成人话。

---

## 为什么零依赖

```
取数  ──▶  麦当劳 MCP        实时的订单、券、菜单、积分、门店、活动
算术  ──▶  mcledger（纯标准库） 确定性统计 / 人格判定 / 优惠审计 —— 可测试、可复现
表达  ──▶  WorkBuddy Agent    理解需求、组织语言、确认后下单
```

**把「算钱」从大模型手里拿走，是这个项目最重要的一个决定。**

优惠组合和聚合统计都属于「大模型算不准」的问题。让模型口算「我平均每单花了多少」，
它常常会给你一个看起来很自信但完全错误的数字 —— 而在点餐场景里，**价格错一位数就是事故**。

所以：

- MCP 客户端（`mcp_client.py`）用标准库 `urllib` 手写，没有引入任何 SDK；
- 统计与审计（`analytics.py` / `audit.py`）是纯函数，同一份输入永远得到同一份输出；
- 报告生成（`report.py`）只做字符串拼接与内联 SVG，不依赖任何图表库或模板引擎。

**结果是：任何环境 `python scripts/run.py` 都能直接跑，不需要 `pip install` 任何东西。**

---

## 项目结构

```
mcd-meal-ledger/
├── README.md                     项目介绍、安装方法、使用示例、目标用户
├── CONTEST_DECLARATION.md        参赛声明（官方原文，逐字节一致）
├── MCP_INTEGRATION.md            MCP 接入说明：用了哪些 Tool、调用流程、业务价值
├── mcp-config.example.json       脱敏 MCP 配置示例（仅环境变量占位符）
├── workbuddy.md                  WorkBuddy 开发对话记录
├── LICENSE                       MIT
│
├── mcledger/                     核心包（纯标准库，零依赖）
│   ├── models.py                 宽容取值的归一化：脏数据 / 异形外壳 / 时间戳
│   ├── mcp_client.py             Streamable HTTP + JSON-RPC 的极简 MCP 客户端
│   ├── analytics.py              确定性统计引擎（含内联 SVG 图表）
│   ├── personas.py               规则驱动、证据可追溯的消费人格引擎
│   ├── audit.py                  优惠审计引擎 —— 找出钱漏在哪
│   ├── report.py                 自包含 HTML + Markdown 年报生成
│   └── cli.py                    命令行入口
│
├── scripts/
│   ├── run.py                    一键生成年报（推荐入口）
│   ├── selftest.py               102 项自测断言，纯标准库
│   ├── fetch_snapshot.py         从真实 MCP 拉数据快照（落盘到 .gitignore 目录）
│   └── make_fixtures.py          生成脱敏样例数据（固定种子，可复现）
│
├── fixtures/                     脱敏样例数据（123 笔订单 + 8 张券）
├── skills/mcd-meal-ledger/       Skill 定义（SKILL.md）
└── docs/
    ├── demo.html                 示例年报（离线样例生成）
    └── sample-metrics.json       中间结果 JSON
```

---

## 自测

```bash
python scripts/selftest.py
# ✅ 169 passed, 0 failed  (共 169 项)
```

覆盖五层：

1. **解析层** —— 脏数据（`"¥25.5元"`、毫秒时间戳、缺字段）、三种响应外壳、渠道文案归一化
2. **统计层** —— 聚合结果与手工核算逐项对齐，空输入不崩溃
3. **业务层** —— 人格规则判定、审计结论（含「全原价时不虚构金额」这条诚实性断言）
4. **端到端** —— 离线样例跑通、HTML 产物**同一输入产出完全一致**（可复现性断言）
5. **真实 MCP 契约** —— 用真实联调报文锁死
   `orderProductList` / `realTotalAmount` / `beType` 编码映射 / 套餐拆解 / 积分字段，
   防止未来"优化"别名表时把这些关键字段弄丢

---

## 目标用户

| 用户 | 场景 | 麦门账本给什么 |
|---|---|---|
| **麦门忠实用户** | 想知道自己一年在麦当劳花了多少 | 一份可分享的消费年鉴 |
| **想省钱的人** | 感觉自己在麦当劳花多了，但不知道花在哪 | 优惠审计：哪几单买亏了、有多少券可以领没领 |
| **有分享欲的年轻人** | 想发一条有数据感、有梗的内容 | 「甜品治愈系」「资深麦门老友」这类人格标签 + 精美卡片 |
| **想复盘消费的人** | 想看自己的消费节奏与结构 | 月度趋势、时段热力、渠道分布、常点榜单 |
| **开发者** | 想看看 MCP 能怎么用 | 一个零依赖、可测试、可复现的完整参考实现 |

---

## MCP 接入

本项目调用 **8 个**麦当劳 MCP Tool：

| Tool | 用途 |
|---|---|
| `order-list` | 历史订单（数据地基，但只给实付金额） |
| `query-order` | 订单详情（**优惠审计的唯一数据源**：原价 / 优惠金额 / 用券明细） |
| `available-coupons` | 麦麦省可领取券 |
| `query-my-coupons` | 账户已有券 |
| `query-store-coupons` | 门店专属券 |
| `query-meals` | 门店菜单 |
| `query-my-account` | 积分账户 |
| `campaign-calendar` | 活动日历 |

> **一个关键经验：券必须从三路查全。** 麦麦省可领券、账户已有券、门店专属券
> 是三路并集，**少查一路，用户就少省钱**。
>
> **另一个关键经验：`order-list` 只返回实付金额。** 不逐单调 `query-order` 补全原价与
> 优惠金额，「省了多少」会恒等于 0 —— 审计引擎会**静默失效且不报错**。
> 本项目为此把逐单补全做成了核心步骤。

完整说明（含**真实联调实测的字段结构**、调用时序图、5 个真实联调问题的排查与修复、
业务价值分析）见 **[MCP_INTEGRATION.md](./MCP_INTEGRATION.md)**。

---

## 已通过真实联调验证

本项目不是"写完就交"，而是**用真实 Token 跑通了全链路**，并因此发现并修复了
5 个只在真实数据上才会暴露的问题（脱敏样例永远测不出来）：

| # | 问题 | 后果 |
|:-:|---|---|
| ① | 明细键名实为 `orderProductList` | 订单明细整体解析为空，榜单与画像全部失效 |
| ② | 实付字段实为 `realTotalAmount`，且订单列表不给原价 | **优惠审计静默失效，省下金额恒为 0** |
| ③ | 服务端不返回 `Mcp-Session-Id` | 每次调用都重复握手，请求量翻倍 |
| ④ | `query-meals` / `query-store-coupons` 有 3 个必填参数 | 两次调用直接返回 400 |
| ⑤ | 活动标题实为 `articleDto.title`（`activityTitle` 恒为空） | 活动板块静默变成空白卡片 |

这些问题现已固化为 `scripts/selftest.py` 中的**「真实 MCP 契约测试」**区块，
用真实报文当夹具，防止回归。

真实账号实测产出：

```
订单 2 单 · 实付 46.50 元 · 优惠利用率 0%
积分 46.5 可用 · 0 即将过期 · 15 已过期作废
```

---

## 常见问题

**Q：没有 MCP Token 能跑吗？**
能。`--offline` 会用内置的 123 笔脱敏样例数据完整跑通全流程，方便先看效果。

**Q：我的消费数据会被上传到哪吗？**
不会。数据从麦当劳 MCP 直接拉到你的本机，所有计算都在本地完成，
本项目没有任何服务端。从 MCP 拉取的真实快照默认写入 `fixtures/live/`，
该目录已在 `.gitignore` 中排除。

**Q：为什么报告里的「可省金额」是估算？**
因为它本来就是估算。审计引擎只以**你自己已经实现过的平均折扣率**为基准推算，
不虚构任何「理论最优价」—— 宁可少报，也不给你一个到店付不出来的假数字。
报告里已明确标注「估算」字样。

**Q：会替我用掉券或者下单吗？**
不会。`create-order` / `cancel-order` / `draw-lottery` 等写操作**一律不调用**；
`auto-bind-coupons`（一键领券）仅在显式传入 `--bind-coupons` 时执行，且会提前提示。

**Q：为什么我的报告显示"省下 ¥0.00"？**
很可能你的订单确实全部是原价结算的。麦当劳 MCP 的订单列表只返回实付金额，
本项目会逐单调用详情接口补全原价与优惠金额 —— 只有两边对得上，
才能算出真实的优惠。如果确实是全原价，报告不会编造一个"本可省"的数字，
而是直接指出「全部订单均为原价」并告诉你券池里有多少张可领的券。

**Q：报告里的热量数据是营养建议吗？**
不是。热量只是订单数据的客观陈述，本项目不提供任何医疗或营养建议。

---

## 免责声明

本项目为**麦当劳程序员创意开发大赛参赛作品，由参赛者独立开发，非麦当劳官方产品**。

项目输出仅供参考，不构成医疗、营养或其他专业建议；餐品信息、价格及供应状态
以麦当劳官方渠道的实时结果为准。「估算可省」为基于用户自身历史折扣水平的推算。

「McDonald's」「麦当劳」等商标归其权利人所有，本项目不主张任何商标权利，
亦不构成任何形式的官方背书。

---

## License

[MIT](./LICENSE)

---

<div align="center">

**如果这份「账单审计」的思路对你有启发，欢迎点一个 ⭐**

</div>
