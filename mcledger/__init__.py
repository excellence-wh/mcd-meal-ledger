"""麦门账本 (McDonald's Meal Ledger)
=====================================

把你的麦当劳消费记录，变成一份**带审计结论**的个人年报。

设计分工（整个项目最重要的一条原则）::

    取数  ->  麦当劳 MCP        实时的菜单、订单、优惠券、营养、门店
    算术  ->  mcledger.*        纯标准库的确定性统计与审计，可测试、可复现
    表达  ->  Agent            理解需求、组织语言、确认后下单

同一份输入永远得到同一份输出 —— 这是把「算钱」这件事从大模型手里拿走
的唯一理由。
"""

from .analytics import Metrics, compute_metrics, money
from .audit import AuditFinding, AuditResult, run_audit
from .models import Coupon, Order, OrderItem, dedupe_orders, parse_coupons, parse_orders
from .personas import Persona, evaluate_personas, headline, select_personas
from .report import render_html, render_markdown_summary, write_report

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # 模型
    "Order",
    "OrderItem",
    "Coupon",
    "parse_orders",
    "parse_coupons",
    "dedupe_orders",
    # 统计
    "Metrics",
    "compute_metrics",
    "money",
    # 人格
    "Persona",
    "evaluate_personas",
    "select_personas",
    "headline",
    # 审计
    "AuditResult",
    "AuditFinding",
    "run_audit",
    # 报表
    "render_html",
    "render_markdown_summary",
    "write_report",
]
