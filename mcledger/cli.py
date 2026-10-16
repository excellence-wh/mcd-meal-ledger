"""命令行入口 —— 在线（真实 MCP）与离线（脱敏样例）双模式。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analytics import compute_metrics
from .audit import run_audit
from .mcp_client import MCPAuthError, MCPClient, MCPError, fetch_bundle
from .models import Coupon, dedupe_orders, parse_coupons, parse_orders
from .personas import headline, select_personas
from .report import (
    render_html,
    render_markdown_summary,
    write_json_snapshot,
    write_report,
)

DEFAULT_OUT = "docs/demo.html"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


# --------------------------------------------------------------------------


def load_offline(fixture_dir: Path) -> tuple[list, list[Coupon], list[str]]:
    """离线模式：读本地脱敏样例，保证无 Token 也能完整跑通。"""
    warnings: list[str] = []
    orders_file = fixture_dir / "orders.sample.json"
    coupons_file = fixture_dir / "coupons.sample.json"

    if not orders_file.exists():
        raise FileNotFoundError(f"样例订单缺失：{orders_file}")

    raw_orders = json.loads(orders_file.read_text(encoding="utf-8"))
    orders = dedupe_orders(parse_orders(raw_orders))

    coupons: list[Coupon] = []
    if coupons_file.exists():
        raw_coupons = json.loads(coupons_file.read_text(encoding="utf-8"))
        if isinstance(raw_coupons, list):
            coupons = parse_coupons(raw_coupons, source="available")
        elif isinstance(raw_coupons, dict):
            for source, payload in raw_coupons.items():
                coupons.extend(parse_coupons(payload, source=source))
    else:
        warnings.append("未找到 coupons.sample.json，跳过券审计")

    warnings.append("当前为离线样例模式，数据为脱敏假数据")
    return orders, coupons, warnings


def load_online(args: argparse.Namespace) -> tuple[list, list[Coupon], list[str], str]:
    """在线模式：真实调用麦当劳 MCP。"""
    client = MCPClient(verbose=args.verbose)
    bundle = fetch_bundle(
        client,
        order_pages=args.pages,
        page_size=args.page_size,
        store_code=args.store,
        include_campaigns=not args.no_campaigns,
        bind_coupons=args.bind_coupons,
    )

    orders = dedupe_orders(parse_orders(bundle.orders_raw))

    coupons: list[Coupon] = []
    for entry in bundle.coupons_raw:
        coupons.extend(parse_coupons(entry.get("payload"), source=entry.get("source", "unknown")))

    warnings = list(bundle.warnings)
    if not orders:
        warnings.append("order-list 未返回任何订单，请确认账号下有历史订单记录")
    return orders, coupons, warnings, "麦当劳 MCP 实时数据"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcd-meal-ledger",
        description="麦门账本 —— 把你的麦当劳消费变成一份带审计结论的个人年报",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/run.py --offline\n"
            "  python scripts/run.py --out docs/demo.html\n"
            "  MCD_MCP_TOKEN=xxx python scripts/run.py --store 门店编码 --pages 5\n"
        ),
    )
    parser.add_argument("--offline", action="store_true", help="使用内置脱敏样例数据，无需 MCP Token")
    parser.add_argument("--fixtures", default=str(FIXTURE_DIR), help="样例数据目录")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"HTML 输出路径（默认 {DEFAULT_OUT}）")
    parser.add_argument("--json-out", default="", help="同时导出中间结果 JSON 的路径")
    parser.add_argument("--markdown", action="store_true", help="在终端打印 Markdown 简报")
    parser.add_argument("--store", default="", help="门店编码（影响门店券与菜单查询）")
    parser.add_argument("--pages", type=int, default=3, help="历史订单拉取页数（默认 3）")
    parser.add_argument("--page-size", type=int, default=50, help="每页条数（默认 50）")
    parser.add_argument("--top", type=int, default=6, help="常点榜单展示条数（默认 6）")
    parser.add_argument("--personas", type=int, default=3, help="展示的人格标签数量（默认 3）")
    parser.add_argument(
        "--bind-coupons",
        action="store_true",
        help="调用 auto-bind-coupons 一键领券（会真实写入账户，默认关闭）",
    )
    parser.add_argument("--no-campaigns", action="store_true", help="跳过活动日历查询")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每次 MCP 调用")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.offline:
            orders, coupons, warnings = load_offline(Path(args.fixtures))
            source = "本地脱敏样例"
        else:
            orders, coupons, warnings, source = load_online(args)
    except MCPAuthError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print("   想先看效果可以加 --offline 用内置样例跑一遍。", file=sys.stderr)
        return 2
    except (MCPError, FileNotFoundError) as exc:
        print(f"❌ 取数失败：{exc}", file=sys.stderr)
        return 3

    metrics = compute_metrics(orders, top_n=max(args.top, 12))
    personas = select_personas(metrics, limit=args.personas)
    audit = run_audit(orders, metrics, coupons)

    html_text = render_html(
        metrics,
        personas,
        audit,
        headline=headline(metrics),
        source=source,
        warnings=warnings,
    )
    out_path = write_report(html_text, args.out)

    if args.json_out:
        write_json_snapshot(
            {
                "source": source,
                "orders": [
                    {
                        "orderId": o.order_id,
                        "createdAt": o.created_at.isoformat() if o.created_at else None,
                        "channel": o.channel,
                        "store": o.store,
                        "gross": o.gross,
                        "paid": o.paid,
                        "saved": o.saved,
                        "items": [{"name": i.name, "qty": i.qty, "paid": i.paid_amount} for i in o.items],
                    }
                    for o in orders
                ],
                "metrics": {
                    "totalOrders": metrics.total_orders,
                    "totalPaid": metrics.total_paid,
                    "totalSaved": metrics.total_saved,
                    "saveRate": metrics.save_rate,
                    "avgOrderValue": metrics.avg_order_value,
                },
                "personas": [
                    {"title": p.title, "evidence": p.evidence, "score": p.score} for p in personas
                ],
                "audit": {
                    "grade": audit.grade,
                    "couponUtilization": audit.coupon_utilization,
                    "estimatedMissed": audit.estimated_missed,
                    "findings": [
                        {"level": f.level, "title": f.title, "amount": f.amount}
                        for f in audit.findings
                    ],
                },
            },
            args.json_out,
        )

    print(f"✅ 年报已生成：{out_path}")
    print(f"   订单 {metrics.total_orders} 单 · 实付 {metrics.total_paid:.2f} 元 · "
          f"省下 {metrics.total_saved:.2f} 元 · 优惠利用率 {audit.coupon_utilization * 100:.0f}%")

    if args.markdown:
        print()
        print(render_markdown_summary(metrics, personas, audit, headline=headline(metrics)))

    for warning in warnings:
        print(f"   ⚠️  {warning}")

    return 0
