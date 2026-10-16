"""命令行入口 —— 在线（真实 MCP）与离线（脱敏样例）双模式。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .analytics import compute_metrics, stores_from_orders
from .audit import run_audit
from .coupons import CouponPortfolio
from .habits import HabitReport, build_habit_report, project_annual_waste
from .ics import CalendarEvent, build_ics, write_ics
from .mcp_client import (
    CampaignEvent,
    MCPAuthError,
    MCPClient,
    MCPError,
    OfficialQuote,
    fetch_bundle,
    parse_campaign_events,
    parse_campaign_titles,
    parse_stores,
    quote_official_price,
)
from .models import (
    AccountPoints,
    Coupon,
    Order,
    dedupe_orders,
    parse_account,
    parse_coupons,
    parse_orders,
    to_int,
)
from .nutrition import (
    FitRequest,
    build_nutrition_profile,
    fit_meals,
    parse_nutrition_foods,
)
from .personas import headline, select_personas
from .report import (
    ReportExtras,
    render_html,
    render_markdown_summary,
    write_json_snapshot,
    write_report,
)
from .saver import (
    Menu,
    MenuItem,
    Wish,
    WishItem,
    advise_topup,
    build_plans,
    menu_from_mcp,
)
from .wallet import PointsWallet, parse_wallet

NUTRITION_FIXTURE = "nutrition.sample.json"


@dataclass
class LoadedData:
    """一次取数的结果。

    用结构体而不是越来越长的元组 —— 六个以上返回值靠位置记忆，
    迟早会有人把 account 和 promotions 传反，而且不会报错。
    """

    orders: list[Order] = field(default_factory=list)
    coupons: list[Coupon] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = ""
    account: AccountPoints | None = None
    promotions: list[str] = field(default_factory=list)
    events: list[CampaignEvent] = field(default_factory=list)
    nutrition: dict = field(default_factory=dict)
    # 真实门店菜单。拿不到就为 None，上层会退回到「用历史订单均价」并如实标注。
    menu: Menu | None = None
    wallet: PointsWallet = field(default_factory=PointsWallet)
    stores: list[dict[str, str]] = field(default_factory=list)
    store_code: str = ""
    # 官方核价器：给一批商品编码，让官方算价。离线模式下为 None。
    quoter: Callable[[list[dict]], OfficialQuote | None] | None = None


def build_quoter(
    client: MCPClient, store_code: str, order_type: int, be_type: int
) -> Callable[[list[dict]], OfficialQuote | None]:
    """把官方核价包成一个可调用的函数，供省钱助手做闭环核价。"""

    def _quote(items: list[dict]) -> OfficialQuote | None:
        return quote_official_price(
            client,
            store_code=store_code,
            order_type=order_type,
            be_type=be_type,
            items=items,
        )

    return _quote


def menu_from_orders(orders: list[Order]) -> Menu:
    """从历史订单里推出一份"价格参考表"。

    真实门店菜单只返回餐品编码、不返回名字；要拿到"名字 + 当前价格"
    得对每件餐品单独查一次详情，成本很高。所以默认用历史订单里的
    实际成交单价做参考 —— 好处是零额外请求，代价是价格是过去的。

    这个来源会被如实标注出来，不会假装成实时价。
    """
    buckets: dict[str, list[float]] = {}
    for order in orders:
        for item in order.items:
            if item.unit_price > 0:
                buckets.setdefault(item.name, []).append(item.unit_price)
    items = [MenuItem(name, sum(v) / len(v)) for name, v in buckets.items()]
    return Menu(items=sorted(items, key=lambda i: i.name), source="history")


def wish_from_text(text: str, menu: Menu) -> Wish:
    """把「巨无霸:2,中薯条」这样的文本解析成想点清单。

    价格优先取菜单里的；菜单里没有就填 0，上层会如实说明金额不完整。
    """
    items: list[WishItem] = []
    for chunk in re.split(r"[,，;；]", text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, qty_text = chunk.rpartition(":")
        if not sep:
            name, qty_text = chunk, "1"
        qty = to_int(qty_text, default=1) or 1
        hit = menu.find(name.strip())
        items.append(
            WishItem(name=name.strip(), qty=qty, unit_price=hit.price if hit else 0.0)
        )
    return Wish(items=items)


def load_nutrition_table(fixture_dir: Path) -> dict:
    """加载营养表。找不到就返回空表，让上层如实说"没有营养数据"。"""
    path = fixture_dir / NUTRITION_FIXTURE
    if not path.exists():
        return {}
    return parse_nutrition_foods(json.loads(path.read_text(encoding="utf-8")))


# 钱包三本账的样例文件。缺哪个就跳过哪个，不编数据。
WALLET_FIXTURES: dict[str, str] = {
    "products_raw": "points-products.sample.json",
    "lottery_raw": "lottery.sample.json",
    "prizes_raw": "my-prizes.sample.json",
    "mall_orders_raw": "mall-orders.sample.json",
}

MENU_FIXTURE = "menu.sample.json"


def load_wallet_fixtures(fixture_dir: Path) -> PointsWallet:
    """离线模式的钱包样例。"""
    payloads: dict[str, object] = {}
    for key, filename in WALLET_FIXTURES.items():
        path = fixture_dir / filename
        payloads[key] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return parse_wallet(**payloads)


def load_menu_fixture(fixture_dir: Path) -> Menu | None:
    """离线模式的演示菜单（结构照抄真实接口的两层形态）。"""
    path = fixture_dir / MENU_FIXTURE
    if not path.exists():
        return None
    menu = menu_from_mcp(json.loads(path.read_text(encoding="utf-8")))
    menu.source = "fixture"
    return menu if menu.items else None


def build_extras(
    orders: list[Order],
    coupon_names: list[str],
    menu: Menu,
    table: dict,
    fit_request: FitRequest | None,
    *,
    wallet: PointsWallet | None = None,
    stores: list[dict[str, str]] | None = None,
) -> tuple[ReportExtras, CouponPortfolio, HabitReport]:
    """把几个新板块需要的数据一次算好。"""
    portfolio = CouponPortfolio.from_names(coupon_names)
    habit_report = build_habit_report(orders, portfolio, menu)
    profile = build_nutrition_profile(orders, table)
    fit = fit_meals(menu, table, fit_request) if (fit_request and table and menu) else None

    # 拿用户最常点的那个习惯当成"想吃什么"，给出凑单建议
    topups = []
    if habit_report.habits:
        signature = habit_report.habits[0].signature
        wish_items = []
        for name in signature:
            hit = menu.find(name)
            wish_items.append(WishItem(name, 1, hit.price if hit else 0.0))
        topups = advise_topup(Wish(items=wish_items), portfolio, menu)

    extras = ReportExtras(
        habits=habit_report,
        portfolio=portfolio,
        topups=topups,
        nutrition=profile,
        meal_fit=fit,
        menu_source=menu.source,
        wallet=wallet or PointsWallet(),
        stores=stores or [],
    )
    return extras, portfolio, habit_report


def _print_saver(
    args: argparse.Namespace,
    portfolio: CouponPortfolio,
    menu: Menu,
    quoter: Callable[[list[dict]], OfficialQuote | None] | None = None,
) -> None:
    """现场比价：用户说想吃什么，当场算怎么点最省。"""
    if not args.wish:
        return
    wish = wish_from_text(args.wish, menu)
    if not wish:
        return

    print()
    print(f"   你想点：{args.wish}（原价 {wish.gross:.2f} 元）")

    unknown = [i.name for i in wish.items if i.unit_price <= 0]
    if unknown:
        print(f"   ⚠️  「{'、'.join(unknown)}」没有价格参考，下面的金额不完整")

    plans = build_plans(wish, portfolio)
    if plans:
        best = plans[0]
        if best.discount > 0:
            print(f"   最省：{best.label}，实付 {best.payable:.2f} 元，省 {best.discount:.2f} 元")
        else:
            print("   没有能算出确定金额的券可用，按原价点就是最省的")
        for plan in plans[1:4]:
            print(f"     · 也可以：{plan.explain()}")

    for advice in advise_topup(wish, portfolio, menu):
        print(f"   · {advice.explain()}")

    # 官方核价：把上面这些"我们的估算"交给麦当劳自己算一遍。
    # 只有拿到门店实时菜单（才有商品编码）时才能做。
    if quoter is not None and menu.is_live:
        codes: list[dict] = []
        for item in wish.items:
            hit = menu.find(item.name)
            if hit is not None and hit.code:
                codes.append({"productCode": hit.code, "quantity": item.qty})
        if codes:
            quote = quoter(codes)
            if quote is not None and quote.is_valid:
                if quote.discount > 0:
                    detail = f"，原价 {quote.original:.2f} 元，省 {quote.discount:.2f} 元"
                else:
                    detail = ""
                print(
                    f"   官方核价：应付 {quote.payable:.2f} 元{detail}"
                    " —— 这个价是麦当劳自己算的，不是我们估的"
                )


def _print_fit(extras: ReportExtras) -> None:
    """营养搭配：在用户给的限制下找最省的一套。"""
    fit = extras.meal_fit
    if fit is None:
        return
    print()
    print(f"   营养搭配（穷举了 {fit.candidates_scanned} 种组合）：{fit.headline()}")
    for option in fit.options[1:4]:
        print(f"     · 也可以：{option.explain()}")


DEFAULT_OUT = "docs/demo.html"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _stderr_tracer(message: str) -> None:
    """把 MCP 调用日志写到 stderr。

    刻意不走 stdout —— 脚本的 stdout 要保持是"纯结果"，
    这样 `run.py | jq` 之类的管道用法才不会被日志污染。
    """
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------


def load_offline(fixture_dir: Path) -> LoadedData:
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
        warnings.append("没找到券的演示数据，这次不检查优惠券")

    # 样例里也给一份积分快照与活动，保证离线模式能覆盖这两条分支
    account = AccountPoints(
        available=46.5, accumulative=61.5, expired=15.0, currency="麦享会积分"
    )
    campaigns_file = fixture_dir / "campaigns.sample.json"
    campaigns_raw: object = (
        json.loads(campaigns_file.read_text(encoding="utf-8"))
        if campaigns_file.exists()
        else None
    )
    promotions = parse_campaign_titles(campaigns_raw) if campaigns_raw else []
    events = parse_campaign_events(campaigns_raw) if campaigns_raw else []

    warnings.append("这次用的是内置演示数据，不是你的真实账单")
    demo_menu = load_menu_fixture(fixture_dir)
    return LoadedData(
        orders=orders,
        coupons=coupons,
        warnings=warnings,
        source="内置演示数据",
        account=account,
        promotions=promotions,
        events=events,
        nutrition=load_nutrition_table(fixture_dir),
        menu=demo_menu,
        wallet=load_wallet_fixtures(fixture_dir),
        stores=stores_from_orders(orders),
        store_code="",
        quoter=None,
    )


def load_online(args: argparse.Namespace) -> LoadedData:
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
    events = parse_campaign_events(bundle.campaigns_raw)

    warnings = list(bundle.warnings)
    if bundle.enriched:
        warnings.append(f"已为 {bundle.enriched} 笔订单补齐了优惠明细")
    if not orders:
        warnings.append("没读到你的历史订单。如果你确实在麦当劳点过单，可能是账号没对上")
    # 营养表只认接口返回的真实数据 ——
    # 拿内置样例冒充真实营养数据会让报告里的数字变成编的。
    table = parse_nutrition_foods(bundle.nutrition_raw) if bundle.nutrition_raw else {}
    if not table:
        warnings.append("没读到官方营养数据，这次不算营养画像")

    # 真实门店菜单：这才是「你现在去吃要多少钱」的正确基准。
    # 拿不到就退回历史均价，并如实说明价格是过去的。
    parsed_menu = menu_from_mcp(bundle.meals_raw) if bundle.meals_raw else None
    menu = parsed_menu if (parsed_menu and parsed_menu.items) else None
    if menu is None:
        warnings.append("没读到门店实时菜单，比价改用它给出的历史成交价，可能已经过期")

    wallet = parse_wallet(
        products_raw=bundle.points_products_raw,
        lottery_raw=bundle.lottery_raw,
        prizes_raw=bundle.my_prizes_raw,
        mall_orders_raw=bundle.mall_orders_raw,
    )
    # 门店以「你自己点过单的店」为主 —— 官方「附近门店」接口在用户
    # 没有收藏餐厅时会直接返回失败，只靠它会得到一张空卡片。
    own_stores = stores_from_orders(orders)
    seen_stores = {s["name"] for s in own_stores}
    stores = own_stores + [
        s for s in parse_stores(bundle.stores_raw) if s["name"] not in seen_stores
    ]
    quoter = (
        build_quoter(client, bundle.store_code, bundle.order_type, bundle.be_type)
        if menu and bundle.store_code
        else None
    )

    return LoadedData(
        orders=orders,
        coupons=coupons,
        warnings=warnings,
        source="你的麦当劳账户",
        account=account,
        promotions=promotions,
        events=events,
        nutrition=table,
        menu=menu,
        wallet=wallet,
        stores=stores,
        store_code=bundle.store_code,
        quoter=quoter,
    )


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
    parser.add_argument(
        "--ics-out",
        default="",
        help="把最近的活动导出成 .ics 日历文件，可导入手机日历（如 mcd-events.ics）",
    )
    parser.add_argument(
        "--wish",
        default="",
        help='想点什么，用逗号分隔，如 "巨无霸:2,中薯条" —— 会当场比价并判断凑单值不值',
    )
    parser.add_argument("--fit-calories", type=float, default=None, help="营养搭配的热量上限（大卡）")
    parser.add_argument("--fit-protein", type=float, default=None, help="营养搭配的蛋白质下限（克）")
    parser.add_argument("--fit-budget", type=float, default=None, help="营养搭配的价格上限（元）")
    parser.add_argument("-v", "--verbose", action="store_true", help="把每次 MCP 调用打印到 stderr")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        data = load_offline(Path(args.fixtures)) if args.offline else load_online(args)
    except MCPAuthError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        print("   想先看效果可以加 --offline 用内置样例跑一遍。", file=sys.stderr)
        return 2
    except (MCPError, FileNotFoundError) as exc:
        print(f"❌ 取数失败：{exc}", file=sys.stderr)
        return 3

    orders, coupons = data.orders, data.coupons
    warnings, source = data.warnings, data.source
    account, promotions = data.account, data.promotions
    table = data.nutrition

    metrics = compute_metrics(orders, top_n=max(args.top, 12))
    personas = select_personas(metrics, limit=args.personas)
    audit = run_audit(orders, metrics, coupons, account)

    # 省钱 / 营养两块共用一份菜单与营养表。
    # 优先用门店真实菜单；拿不到才退回「从历史订单推出来的均价」。
    menu = data.menu or menu_from_orders(orders)
    fit_request = None
    if args.fit_calories or args.fit_protein or args.fit_budget:
        fit_request = FitRequest(
            max_calories=args.fit_calories,
            min_protein=args.fit_protein,
            max_price=args.fit_budget,
        )
    extras, portfolio, habit_report = build_extras(
        orders,
        [c.name for c in coupons],
        menu,
        table,
        fit_request,
        wallet=data.wallet,
        stores=data.stores,
    )
    annual_waste = project_annual_waste(orders, audit.estimated_missed)

    html_text = render_html(
        metrics,
        personas,
        audit,
        headline=headline(metrics),
        source=source,
        warnings=warnings,
        account=account,
        promotions=promotions,
        extras=extras,
    )
    out_path = write_report(html_text, args.out)

    if args.ics_out:
        ics_events = [
            CalendarEvent(date=e.date, title=e.title, note="来自麦当劳活动日历")
            for e in data.events
        ]
        if ics_events:
            ics_path = write_ics(build_ics(ics_events), args.ics_out)
            print(f"   活动日历已导出：{ics_path}（可以导进手机日历）")
        else:
            print("   没读到带日期的活动，这次不导出日历")

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
        f"   一共 {metrics.total_orders} 单，花了 {metrics.total_paid:.2f} 元，"
        f"省下 {metrics.total_saved:.2f} 元，"
        f"其中 {audit.coupon_utilization * 100:.0f}% 的订单用上了优惠"
    )
    if account is not None and (account.available or account.expired):
        # 为 0 的项就不提了 —— 说"0 分快到期了"只会让人困惑
        parts = [f"积分还剩 {account.available:g} 分"]
        if account.at_risk > 0:
            parts.append(f"{account.at_risk:g} 分快到期了")
        if account.expired > 0:
            parts.append(f"已经有 {account.expired:g} 分过期作废")
        print("   " + "，".join(parts))

    if args.markdown:
        print()
        print(
            render_markdown_summary(
                metrics,
                personas,
                audit,
                headline=headline(metrics),
                account=account,
                extras=extras,
            )
        )

    _print_saver(args, portfolio, menu, data.quoter)
    _print_fit(extras)

    wallet_lines = data.wallet.advice(account)
    if wallet_lines:
        print()
        print("   积分的账：")
        for line in wallet_lines:
            print(f"     · {line}")

    if habit_report.has_data:
        print()
        print(f"   {habit_report.headline()}")
        if annual_waste >= 1:
            print(
                f"   按你现在的消费节奏，光是把该用的券用上，一年大约能少浪费 {annual_waste:.2f} 元"
            )

    for warning in warnings:
        print(f"   ⚠️  {warning}")

    return 0
