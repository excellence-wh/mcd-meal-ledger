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

=================  ==========================================================
``models``         数据模型与宽容取值解析（脏数据、异形响应外壳、编码映射）
``mcp_client``     Streamable HTTP + JSON-RPC 的极简 MCP 客户端与取数流程
``analytics``      确定性统计引擎（含内联 SVG 图表生成）
``personas``       规则驱动、证据可追溯的消费人格引擎
``audit``          优惠与积分审计引擎 —— 找出钱漏在哪
``report``         自包含 HTML + Markdown 年报生成
``cli``            命令行入口
=================  ==========================================================
"""

from .analytics import Metrics, compute_metrics, money
from .audit import AuditFinding, AuditResult, run_audit
from .models import (
    AccountPoints,
    Coupon,
    Order,
    OrderItem,
    classify_item,
    dedupe_orders,
    parse_account,
    parse_coupons,
    parse_orders,
)
from .personas import Persona, evaluate_personas, headline, select_personas
from .report import render_html, render_markdown_summary, write_report

__version__ = "1.0.0"

# 刻意按「模型 / 统计 / 人格 / 审计 / 报表」分组，而不是字母序 ——
# 分组能直接反映模块结构，对读代码的人比排序更有用。
__all__ = [  # noqa: RUF022
    "__version__",
    # 模型
    "AccountPoints",
    "Coupon",
    "Order",
    "OrderItem",
    "classify_item",
    "dedupe_orders",
    "parse_account",
    "parse_coupons",
    "parse_orders",
    # 统计
    "Metrics",
    "compute_metrics",
    "money",
    # 人格
    "Persona",
    "evaluate_personas",
    "headline",
    "select_personas",
    # 审计
    "AuditFinding",
    "AuditResult",
    "run_audit",
    # 报表
    "render_html",
    "render_markdown_summary",
    "write_report",
]
