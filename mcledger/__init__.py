"""麦门账本 (McDonald's Meal Ledger)
=====================================

把你的麦当劳消费记录，变成一份**带审计结论**的个人年报。

设计分工（整个项目最重要的一条原则）::

    取数  ->  麦当劳 MCP        实时的订单、菜单、优惠券、积分、门店、活动
    算术  ->  mcledger.*        纯标准库的确定性统计与审计，可测试、可复现
    表达  ->  Agent            理解需求、组织语言、确认后下单

同一份输入永远得到同一份输出 —— 这是把「算钱」这件事从大模型手里拿走
的唯一理由。

模块职责一览：

===================  ==========================================================
``models``           数据模型与宽容取值解析（脏数据、异形外壳、紧凑表格文本）
``mcp_client``       Streamable HTTP + JSON-RPC 的极简 MCP 客户端与取数流程
``analytics``        确定性统计引擎（含内联 SVG 图表生成）
``personas``         规则驱动、证据可追溯的消费人格引擎
``audit``            优惠与积分审计引擎 —— 找出钱漏在哪
``coupons``          从券名反推券规则的确定性解析器
``saver``            省钱助手：比价、凑单值不值、官方核价
``habits``           从历史订单认习惯，按真实跨度把账年化
``nutrition``        营养合计、约束筛选与个人饮食画像
``wallet``           积分的账：单位积分价值、抽奖成本、商城订单
``ics``              活动导出成可导入手机日历的 .ics
``report``           自包含 HTML + Markdown 年报生成
``cli``              命令行入口
===================  ==========================================================
"""

from .analytics import Metrics, compute_metrics, money, stores_from_orders
from .audit import AuditFinding, AuditResult, run_audit
from .coupons import CouponPortfolio, CouponRule, parse_coupon_rule
from .habits import HabitReport, build_habit_report, extract_habits
from .ics import CalendarEvent, build_ics, write_ics
from .models import (
    AccountPoints,
    Coupon,
    Order,
    OrderItem,
    classify_item,
    decode_rows,
    dedupe_orders,
    parse_account,
    parse_compact_table,
    parse_coupons,
    parse_orders,
)
from .nutrition import (
    NutritionFact,
    NutritionProfile,
    build_nutrition_profile,
    fit_meals,
    lookup_fact,
    parse_nutrition_foods,
    total_nutrition,
)
from .personas import Persona, evaluate_personas, headline, select_personas
from .report import render_html, render_markdown_summary, write_report
from .saver import Menu, MenuItem, Wish, WishItem, advise_topup, build_plans, menu_from_mcp
from .wallet import PointsProduct, PointsWallet, parse_wallet

__version__ = "1.0.0"

# 刻意按「模型 / 统计 / 人格 / 审计 / 前瞻能力 / 报表」分组，而不是字母序 ——
# 分组能直接反映模块结构，对读代码的人比排序更有用。
__all__ = [  # noqa: RUF022
    "__version__",
    # 模型
    "AccountPoints",
    "Coupon",
    "Order",
    "OrderItem",
    "classify_item",
    "decode_rows",
    "dedupe_orders",
    "parse_account",
    "parse_compact_table",
    "parse_coupons",
    "parse_orders",
    # 统计
    "Metrics",
    "compute_metrics",
    "money",
    "stores_from_orders",
    # 人格
    "Persona",
    "evaluate_personas",
    "headline",
    "select_personas",
    # 审计
    "AuditFinding",
    "AuditResult",
    "run_audit",
    # 券
    "CouponPortfolio",
    "CouponRule",
    "parse_coupon_rule",
    # 省钱
    "Menu",
    "MenuItem",
    "Wish",
    "WishItem",
    "advise_topup",
    "build_plans",
    "menu_from_mcp",
    # 习惯
    "HabitReport",
    "build_habit_report",
    "extract_habits",
    # 营养
    "NutritionFact",
    "NutritionProfile",
    "build_nutrition_profile",
    "fit_meals",
    "lookup_fact",
    "parse_nutrition_foods",
    "total_nutrition",
    # 积分的账
    "PointsProduct",
    "PointsWallet",
    "parse_wallet",
    # 日历
    "CalendarEvent",
    "build_ics",
    "write_ics",
    # 报表
    "render_html",
    "render_markdown_summary",
    "write_report",
]
