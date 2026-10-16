"""命令行入口 —— 在线（真实 MCP）与离线（脱敏样例）双模式。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analytics import compute_metrics
from .audit import run_audit
from .mcp_client import (
    MCPAuthError,
    MCPClient,
    MCPError,
    fetch_bundle,
    parse_campaign_titles,
)
from .models import (
    AccountPoints,
    Coupon,
    Order,
    dedupe_orders,
    parse_account,
    parse_coupons,
    parse_orders,
)
from .personas import headline, select_personas
from .report import (
    render_html,
    render_markdown_summary,
    write_json_snapshot,
    write_report,
)

DEFAULT_OUT = "docs/demo.html"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _stderr_tracer(message: str) -> None:
    """把 MCP 调用日志写到 stderr。

    刻意不走 stdout —— 脚本的 stdout 要保持是"纯结果"，
    这样 `run.py | jq` 之类的管道用法才不会被日志污染。
    """
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------


def load_offline(
    fixture_dir: Path,
) -> tuple[list[Order], list[Coupon], list[str], AccountPoints | None, list[str]]:
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

    # 样例里也给一份积分快照与活动，保证离线模式能覆盖这两条分支
    account = AccountPoints(
        available=46.5, accumulative=61.5, expired=15.0, currency="麦享会积分"
    )
    promotions = ["1+1随心配", "周一会员日", "早餐满减"]

    warnings.append("当前为离线样例模式，数据为脱敏假数据")
    return orders, coupons, warnings, account, promotions


def load_online(
    args: argparse.Namespace,
) -> tuple[list[Order], list[Coupon], list[str], str, AccountPoints | None, list[str]]:
    """在线模式：真实调用麦当劳 MCP。"""
    client = MCPClient(trace=_stderr_tracer if args.verbose else None)
    bundle = fetch_bundle(
        client,
        order_pages=args.pages,
        page_size=args.page_size,
        store_code=args.store or None,
        include_campaigns=not args.no_campaigns,
        bind_coupons=args.bind_coupons,
        enrich=not args.no_enrich,
        enrich_limit=args.enrich_limit,
    )

    orders = dedupe_orders(parse_orders(bundle.orders_raw))

    coupons: list[Coupon] = []
    for entry in bundle.coupons_raw:
        coupons.extend(parse_coupons(entry.get("payload"), source=entry.get("source", "unknown")))

    account = parse_account(bundle.account_raw)
    promotions = parse_campaign_titles(bundle.campaigns_raw)

    warnings = list(bundle.warnings)
    if bundle.enriched:
        warnings.append(
            f"已通过 query-order 补全 {bundle.enriched} 单详情（原价 / 优惠金额 / 用券）"
        )
    if not orders:
        warnings.append("order-list 未返回任何订单，请确认账号下有历史订单记录")
    return orders, coupons, warnings, "麦当劳 MCP 实时数据", account, promotions


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
    parser.add_argument("--pages", type=int, default=3, help="历史订单拉取尝试次数（接口忽略分页参数）")
    parser.add_argument("--page-size", type=int, default=200, help="每页条数（默认 200）")
    parser.add_argument("--top", type=int, default=6, help="常点榜单展示条数（默认 6）")
    parser.add_argument("--personas", type=int, default=3, help="展示的人格标签数量（默认 3）")
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="跳过 query-order 逐单补全（会失去原价与优惠金额，审计将失效）",
    )
    parser.add_argument(
        "--enrich-limit", type=int, default=200, help="逐单补全详情的订单数上限（默认 200）"
    )
    parser.add_argument(
        "--bind-coupons",
        action="store_true",
        help="调用 auto-bind-coupons 一键领券（会真实写入账户，默认关闭）",
    )
    parser.add_argument("--no-campaigns", action="store_true", help="跳过活动日历查询")
    parser.add_argument("-v", "--verbose", action="store_true", help="把每次 MCP 调用打印到 stderr")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.offline:
            orders, coupons, warnings, account, promotions = load_offline(Path(args.fixtures))
            source = "本地脱敏样例"
        else:
            orders, coupons, warnings, source, account, promotions = load_online(args)
    except MCPAuthError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print("   想先看效果可以加 --offline 用内置样例跑一遍。", file=sys.stderr)
        return 2
    except (MCPError, FileNotFoundError) as exc:
        print(f"❌ 取数失败：{exc}", file=sys.stderr)
        return 3

    metrics = compute_metrics(orders, top_n=max(args.top, 12))
    personas = select_personas(metrics, limit=args.personas)
    audit = run_audit(orders, metrics, coupons, account)

    html_text = render_html(
        metrics,
        personas,
        audit,
        headline=headline(metrics),
        source=source,
        warnings=warnings,
        account=account,
        promotions=promotions,
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
                "account": {
                    "available": getattr(account, "available", 0.0),
                    "expired": getattr(account, "expired", 0.0),
                    "atRisk": getattr(account, "at_risk", 0.0),
                }
                if account
                else None,
            },
            args.json_out,
        )

    print(f"✅ 年报已生成：{out_path}")
    print(
        f"   订单 {metrics.total_orders} 单 · 实付 {metrics.total_paid:.2f} 元 · "
        f"省下 {metrics.total_saved:.2f} 元 · 优惠利用率 {audit.coupon_utilization * 100:.0f}%"
    )
    if getattr(account, "available", 0) or getattr(account, "expired", 0):
        print(
            f"   积分 {getattr(account, 'available', 0):g} 可用 · "
            f"{getattr(account, 'at_risk', 0):g} 即将过期 · "
            f"{getattr(account, 'expired', 0):g} 已过期作废"
        )

    if args.markdown:
        print()
        print(render_markdown_summary(metrics, personas, audit, headline=headline(metrics)))

    for warning in warnings:
        print(f"   ⚠️  {warning}")

    return 0
