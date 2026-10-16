#!/usr/bin/env python
"""麦门账本自测 —— 纯标准库，无 pytest 依赖。

    python scripts/selftest.py

覆盖四层：
  1. 解析层  —— models.py 的宽容取值（脏数据、异形外壳、时间戳）
  2. 统计层  —— analytics.py 的聚合结果与手工核算逐项对齐
  3. 业务层  —— personas.py 规则判定、audit.py 审计结论
  4. 端到端  —— 离线样例跑通、HTML 产物可复现

每一次改核心逻辑，都必须先跑这个文件。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcledger  # noqa: E402
from mcledger.analytics import compute_metrics, money, stores_from_orders  # noqa: E402
from mcledger.audit import LEVEL_ALERT, LEVEL_NOTICE, LEVEL_TIP, run_audit  # noqa: E402
from mcledger.coupons import (  # noqa: E402
    KIND_FREE_GIFT,
    KIND_NTH_ITEM,
    KIND_PRODUCT,
    KIND_THRESHOLD,
    KIND_UNKNOWN,
    CouponPortfolio,
    parse_coupon_rule,
)
from mcledger.habits import build_habit_report, extract_habits, project_annual_waste  # noqa: E402
from mcledger.ics import CalendarEvent, build_ics  # noqa: E402
from mcledger.mcp_client import (  # noqa: E402
    CENTS_PER_YUAN,
    MCPClient,
    MCPError,
    ToolCall,
    _enrich_orders,
    parse_campaign_events,
    parse_campaign_titles,
    parse_official_quote,
    parse_stores,
)
from mcledger.models import (  # noqa: E402
    Order,
    OrderItem,
    classify_item,
    dedupe_orders,
    normalize_channel,
    parse_account,
    parse_compact_table,
    parse_coupons,
    parse_datetime,
    parse_orders,
    pick,
    rows_of,
    to_float,
    to_int,
)
from mcledger.nutrition import (  # noqa: E402
    FitRequest,
    build_nutrition_profile,
    fit_meals,
    lookup_fact,
    parse_nutrition_foods,
    total_nutrition,
)
from mcledger.personas import evaluate_personas, headline, select_personas  # noqa: E402
from mcledger.report import (  # noqa: E402
    render_html,
    render_markdown_summary,
    write_json_snapshot,
    write_report,
)
from mcledger.saver import (  # noqa: E402
    Menu,
    MenuItem,
    Wish,
    WishItem,
    advise_topup,
    build_plans,
    compare_bundle,
    coupon_brief,
    menu_from_mcp,
)
from mcledger.wallet import (  # noqa: E402
    parse_mall_orders,
    parse_points_products,
    parse_wallet,
)


class Runner:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[tuple[str, str]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
        else:
            self.failures.append((name, detail or "断言失败"))

    def eq(self, name: str, actual: object, expected: object) -> None:
        ok = actual == expected
        self.check(name, ok, f"期望 {expected!r}，实际 {actual!r}")

    def close(self, name: str, actual: float, expected: float, tol: float = 0.01) -> None:
        ok = abs(actual - expected) <= tol
        self.check(name, ok, f"期望 {expected}±{tol}，实际 {actual}")

    def report(self) -> int:
        total = self.passed + len(self.failures)
        print(f"\n{'=' * 56}")
        if self.failures:
            print(f"❌ {self.passed} passed, {len(self.failures)} failed  (共 {total} 项)")
            for name, detail in self.failures:
                print(f"   ✗ {name}: {detail}")
            return 1
        print(f"✅ {self.passed} passed, 0 failed  (共 {total} 项)")
        return 0


# ==========================================================================
# 1. 解析层
# ==========================================================================


def test_parsing(t: Runner) -> None:
    t.close("to_float 中文币种", to_float("¥25.5元"), 25.5)
    t.close("to_float 千分位", to_float("1,234.50"), 1234.5)
    t.close("to_float None", to_float(None), 0.0)
    t.close("to_float 空串", to_float(""), 0.0)
    t.close("to_float 已是数字", to_float(12.5), 12.5)
    t.eq("to_int 字符串", to_int("2"), 2)
    t.eq("to_int 四舍五入", to_int(2.6), 3)

    t.eq("pick 扁平键", pick({"a": 1}, "b", "a"), 1)
    t.eq("pick 嵌套路径", pick({"store": {"name": "国贸店"}}, "storeName", "store.name"), "国贸店")
    t.eq("pick 跳过空值", pick({"a": "", "b": "hit"}, "a", "b"), "hit")
    t.eq("pick 全缺返回默认", pick({}, "a", "b", default="x"), "x")

    t.eq("parse_datetime ISO", parse_datetime("2026-03-05T12:30:00Z"), datetime(2026, 3, 5, 12, 30))
    t.eq("parse_datetime 空格", parse_datetime("2026-03-05 12:30:00"), datetime(2026, 3, 5, 12, 30))
    t.eq("parse_datetime 斜杠", parse_datetime("2026/03/05"), datetime(2026, 3, 5))
    t.check(
        "parse_datetime 毫秒时间戳",
        parse_datetime(1770000000000) is not None,
        "毫秒时间戳应能解析",
    )
    t.eq("parse_datetime 垃圾输入", parse_datetime("不是时间"), None)

    t.eq("渠道 到店", normalize_channel("到店取餐"), "dine_in")
    t.eq("渠道 麦乐送", normalize_channel("麦乐送"), "delivery")
    t.eq("渠道 得来速", normalize_channel("drivethru"), "drive_thru")
    t.eq("渠道 团餐", normalize_channel("企业团餐"), "group")
    t.eq("渠道 未知", normalize_channel(None), "unknown")

    # 三种响应外壳
    rows = [{"orderId": "A", "items": [{"name": "巨无霸", "qty": 1, "price": 25.5}]}]
    t.eq("外壳①裸数组", len(parse_orders(rows)), 1)
    t.eq("外壳②data", len(parse_orders({"data": rows})), 1)
    t.eq("外壳③data.list", len(parse_orders({"data": {"list": rows}})), 1)
    t.eq("外壳④空输入", len(parse_orders(None)), 0)

    dirty = {
        "orderId": "D1",
        "orderTime": "2026-09-18 12:40:00",
        "orderType": "到店取餐",
        "storeName": "国贸店",
        "items": [{"name": "巨无霸", "qty": 1, "price": "¥25.5元"}],
        "totalAmount": "¥38.50",
        "payAmount": "31.50",
    }
    parsed = parse_orders([dirty])[0]
    t.close("脏数据 原价", parsed.gross, 38.5)
    t.close("脏数据 实付", parsed.paid, 31.5)
    t.close("脏数据 省下", parsed.saved, 7.0)
    t.eq("脏数据 渠道", parsed.channel, "dine_in")

    # 只有实付 + 优惠 → 反推原价
    reverse = parse_orders([{"orderId": "D2", "actualAmount": 41.0, "totalDiscount": 6.0}])[0]
    t.close("反推原价 = 实付 + 优惠", reverse.gross, 47.0)

    # 去重 + 排序
    raw = [
        {"orderId": "B", "orderTime": "2026-02-01 10:00:00"},
        {"orderId": "A", "orderTime": "2026-01-01 10:00:00"},
        {"orderId": "A", "orderTime": "2026-01-01 10:00:00"},
    ]
    cleaned = dedupe_orders(parse_orders(raw))
    t.eq("去重后数量", len(cleaned), 2)
    t.eq("按时间正序", [o.order_id for o in cleaned], ["A", "B"])

    coupons = parse_coupons(
        {"data": [{"couponId": "C1", "name": "满40减10", "type": "threshold", "threshold": 40, "value": 10}]},
        source="available",
    )
    t.eq("券解析", len(coupons), 1)
    t.eq("券来源标记", coupons[0].source, "available")


# ==========================================================================
# 2. 统计层
# ==========================================================================


def _mk_order(oid, dt, channel, store, items, gross, paid, coupons=None) -> Order:
    return Order(
        order_id=oid,
        created_at=dt,
        channel=channel,
        store=store,
        items=items,
        gross=gross,
        paid=paid,
        discount=round(gross - paid, 2),
        coupons=coupons or [],
    )


SAMPLE = [
    _mk_order(
        "O1", datetime(2026, 3, 2, 12, 10), "dine_in", "国贸店",
        [OrderItem("巨无霸", qty=1, unit_price=25.5, paid_amount=22.5),
         OrderItem("中薯条", qty=1, unit_price=13.0, paid_amount=13.0)],
        38.5, 35.5, ["巨无霸立减3元"],
    ),
    _mk_order(
        "O2", datetime(2026, 3, 2, 23, 40), "delivery", "望京SOHO店",
        [OrderItem("巨无霸", qty=2, unit_price=25.5, paid_amount=51.0)],
        51.0, 51.0, [],
    ),
    _mk_order(
        "O3", datetime(2026, 3, 9, 12, 30), "dine_in", "国贸店",
        [OrderItem("麦辣鸡腿堡", qty=1, unit_price=22.5, paid_amount=22.5)],
        22.5, 22.5, [],
    ),
    _mk_order(
        "O4", datetime(2026, 3, 9, 12, 45), "dine_in", "国贸店",
        [OrderItem("巨无霸", qty=1, unit_price=25.5, paid_amount=17.85)],
        25.5, 17.85, ["巨无霸单品直降"],
    ),
]


def test_analytics(t: Runner) -> None:
    m = compute_metrics(SAMPLE)

    t.eq("订单总数", m.total_orders, 4)
    t.close("累计实付", m.total_paid, 35.5 + 51.0 + 22.5 + 17.85)
    t.close("累计原价", m.total_gross, 38.5 + 51.0 + 22.5 + 25.5)
    t.close("累计省下", m.total_saved, 3.0 + 0.0 + 0.0 + 7.65)
    t.close("平均客单价", m.avg_order_value, (35.5 + 51.0 + 22.5 + 17.85) / 4)
    t.eq("用券单数", m.discounted_orders, 2)
    t.eq("原价单数", m.free_orders, 2)
    t.close("原价单占比", m.free_order_rate, 0.5)
    t.close("折后省率", m.save_rate, 10.65 / 137.5, tol=0.0005)

    t.eq("月度分组数", len(m.monthly), 1)
    t.eq("月度订单数", m.monthly[0].orders, 4)
    t.eq("时段峰值小时", m.peak_hour, 12)
    t.eq("星期峰值（3/2 与 3/9 均为周一）", m.peak_weekday, 0)
    t.eq("渠道分布首位", m.channels[0].channel, "dine_in")
    t.eq("门店分布首位", m.stores[0].name, "国贸店")
    t.eq("门店单量", m.stores[0].orders, 3)

    top_names = [i.name for i in m.top_items]
    t.eq("单品榜首", top_names[0], "巨无霸")
    t.eq("巨无霸总数", m.top_items[0].qty, 4)

    t.eq("省得最多的一单", m.best_order.order_id if m.best_order else None, "O4")
    t.eq("花得最多的一单", m.worst_order.order_id if m.worst_order else None, "O2")

    t.eq("空输入不炸", compute_metrics([]).total_orders, 0)
    t.eq("金额格式", money(1234.5), "¥1,234.50")
    t.eq("空数据品类占比", compute_metrics([]).category_share("甜品"), 0.0)

    # 品类与星期占比（用带品类/时间的样本验证）
    cat_orders = [
        _mk_order(
            f"K{i}", datetime(2026, 4, 6 + i, 12, 0), "dine_in", "店",
            [OrderItem("圆筒冰淇淋", qty=1, unit_price=5.0, paid_amount=5.0, category="甜品"),
             OrderItem("巨无霸", qty=1, unit_price=25.5, paid_amount=25.5, category="汉堡")],
            30.5, 30.5,
        )
        for i in range(5)
    ]
    km = compute_metrics(cat_orders)
    t.close("品类占比 甜品 = 50%", km.category_share("甜品"), 0.5)
    t.close("品类占比 多品类合并", km.category_share("甜品", "汉堡"), 1.0)
    t.eq("未知品类占比为 0", km.category_share("咖啡"), 0.0)
    t.close("工作日占比（4/6–4/10 全为工作日）", km.weekday_share((0, 1, 2, 3, 4)), 1.0)
    t.close("周末占比", km.weekday_share((5, 6)), 0.0)
    t.check(
        "甜品规则命中",
        any(p.key == "_r_dessert_healer" for p in evaluate_personas(km)),
        "50% 甜品应命中甜品治愈系",
    )

    # 客单价规则
    big = [
        _mk_order(f"G{i}", datetime(2026, 5, 1 + i, 12, 0), "dine_in", "店",
                  [OrderItem("巨无霸", 3, 25.5, 76.5)], 76.5, 76.5)
        for i in range(4)
    ]
    t.check(
        "大单规则命中",
        any(p.key == "_r_big_order_operator" for p in evaluate_personas(compute_metrics(big))),
        "客单价 76.5 应命中大单操盘手",
    )

    # 连击：连续 5 天
    streak_orders = [
        _mk_order(f"S{i}", datetime(2026, 5, 1 + i, 12, 0), "dine_in", "店",
                  [OrderItem("圆筒", qty=1, unit_price=5.0, paid_amount=5.0)], 5.0, 5.0)
        for i in range(5)
    ]
    t.eq("最长连击", compute_metrics(streak_orders).streaks.longest, 5)

    # 无时间数据不应崩溃
    no_time = [_mk_order("N1", None, "dine_in", None, [OrderItem("X", 1, 10.0, 10.0)], 10.0, 10.0)]
    nm = compute_metrics(no_time)
    t.eq("无时间数据 订单数", nm.total_orders, 1)
    t.check("无时间数据 has_time_data=False", nm.has_time_data is False)


# ==========================================================================
# 3. 业务层
# ==========================================================================


def test_personas(t: Runner) -> None:
    m = compute_metrics(SAMPLE)
    hits = {p.key for p in evaluate_personas(m)}
    t.check("命中原价英雄（原价占 50%）", "_r_full_price_hero" in hits, f"实际命中 {hits}")
    t.check("命中门店钉子户（国贸店 75%）", "_r_store_loyal" in hits, f"实际命中 {hits}")

    # 构造一个高折扣用户 → 应命中拆券精算师
    coupon_orders = [
        _mk_order(f"C{i}", datetime(2026, 6, 1 + i, 12, 0), "dine_in", "店",
                  [OrderItem("巨无霸", 1, 25.5, 15.0)], 25.5, 15.0, ["大额券"])
        for i in range(10)
    ]
    cm = compute_metrics(coupon_orders)
    keys = {p.key for p in evaluate_personas(cm)}
    t.check("命中拆券精算师（折扣率 41%）", "_r_coupon_master" in keys, f"实际命中 {keys}")
    t.check("不建议原价英雄", "_r_full_price_hero" not in keys, f"实际命中 {keys}")

    # 深夜占比高 → 守夜人
    night = [
        _mk_order(f"N{i}", datetime(2026, 7, 1 + i % 5, 23, 30), "delivery", "店",
                  [OrderItem("巨无霸", 1, 25.5, 25.5)], 25.5, 25.5)
        for i in range(10)
    ]
    nkeys = {p.key for p in evaluate_personas(compute_metrics(night))}
    t.check("命中守夜人", "_r_night_watcher" in nkeys, f"实际命中 {nkeys}")

    t.eq("人格数量上限", len(select_personas(cm, limit=2)), 2)
    t.check("人格有证据文案", all(p.evidence for p in select_personas(cm)))
    t.check("标题非空", bool(headline(cm)))
    t.eq("空数据人格为空", len(evaluate_personas(compute_metrics([]))), 0)


def test_audit(t: Runner) -> None:
    m = compute_metrics(SAMPLE)
    a = run_audit(SAMPLE, m, [])

    t.eq("原价单数", a.full_price_orders, 2)
    t.close("原价单实付", a.full_price_paid, 51.0 + 22.5)
    t.close("基准折扣率（仅统计有优惠的单）", a.baseline_discount_rate, 10.65 / 64.0, tol=0.001)
    t.check("估算可省金额 > 0", a.estimated_missed > 0, f"实际 {a.estimated_missed}")
    t.check("存在告警级结论", any(f.level == LEVEL_ALERT for f in a.findings))
    t.check("等级属于 SABCD", a.grade in ("S", "A", "B", "C", "D"), f"实际 {a.grade}")

    # 全部原价 → 应出现「每一单都是原价买的」这条结论
    full = [
        _mk_order(f"F{i}", datetime(2026, 8, 1 + i, 12, 0), "dine_in", "店",
                  [OrderItem("巨无霸", 1, 25.5, 25.5)], 25.5, 25.5)
        for i in range(4)
    ]
    fa = run_audit(full, compute_metrics(full), [])
    t.eq("全原价 基准折扣率=0", fa.baseline_discount_rate, 0.0)
    t.eq("全原价 估算可省=0（不虚构）", fa.estimated_missed, 0.0)
    t.check("全原价 有告警", any("原价" in f.title for f in fa.findings))

    t.eq("空输入审计不炸", run_audit([], compute_metrics([]), []).full_price_orders, 0)


# ==========================================================================
# 4. 端到端
# ==========================================================================


def test_end_to_end(t: Runner) -> None:
    fixture = ROOT / "fixtures" / "orders.sample.json"
    t.check("样例文件存在", fixture.exists(), str(fixture))
    if not fixture.exists():
        return

    raw = json.loads(fixture.read_text(encoding="utf-8"))
    orders = dedupe_orders(parse_orders(raw))
    t.check("样例订单 > 100 条", len(orders) > 100, f"实际 {len(orders)}")

    m = compute_metrics(orders)
    manual_paid = round(sum(o.paid for o in orders), 2)
    t.close("统计实付与逐单求和一致", m.total_paid, manual_paid)
    t.check("存在原价单", m.free_orders > 0, f"原价单 {m.free_orders}")
    t.check("存在优惠单", m.discounted_orders > 0)
    t.check("时间跨度 > 200 天", m.span_days > 200, f"实际 {m.span_days}")
    t.check("月度分组 ≥ 9 组", len(m.monthly) >= 9, f"实际 {len(m.monthly)}")

    coupons_raw = json.loads((ROOT / "fixtures" / "coupons.sample.json").read_text(encoding="utf-8"))
    all_coupons = []
    for source, payload in coupons_raw.items():
        all_coupons.extend(parse_coupons(payload, source=source))
    t.check("样例券 > 5 张", len(all_coupons) > 5, f"实际 {len(all_coupons)}")

    personas = select_personas(m, limit=3)
    audit = run_audit(orders, m, all_coupons)

    fixed = datetime(2026, 10, 16, 14, 18, 0)
    html_a = render_html(m, personas, audit, headline=headline(m), warnings=[], generated_at=fixed)
    html_b = render_html(m, personas, audit, headline=headline(m), warnings=[], generated_at=fixed)

    t.check("HTML 非空", len(html_a) > 5000, f"实际 {len(html_a)} 字符")
    t.eq("同输入产出完全一致（可复现）", html_a, html_b)
    t.check("HTML 含标题", "麦门账本" in html_a)
    t.check("HTML 含账单板块", "钱漏在哪" in html_a)
    t.check("HTML 含免责声明", "非麦当劳官方产品" in html_a)
    head = html_a.split("<footer>")[0].replace("http-equiv", "")
    t.check("HTML 自包含（无外链脚本/样式）", "<script" not in html_a and "http" not in head)

    md = render_markdown_summary(m, personas, audit, headline=headline(m))
    t.check("Markdown 摘要含核心数字", "累计实付" in md)
    t.check("Markdown 摘要含账单板块", "钱漏在哪" in md)


# ==========================================================================
# 5. 真实 MCP 契约测试
#
# 下面的报文是 2026-10-16 真实调用 mcp.mcd.cn 抓回来的原样结构。
# 把它们固化成测试，是为了防止以后有人"优化"别名表时
# 把 orderProductList / realTotalAmount 这类关键字段弄丢 ——
# 那会让整份年报静默变成空壳，而且脱敏样例根本测不出来。
# ==========================================================================

REAL_ORDER_LIST: dict[str, Any] = {
    "success": True,
    "code": 200,
    "message": "请求成功",
    "data": {
        "list": [
            {
                "orderId": "1030766560000567244392805855",
                "orderType": "1",
                "createTime": "2026-01-01 15:47:26",
                "beType": "1",
                "beCode": "",
                "storeCode": "1960183",
                "storeName": "麦当劳天门万达广场餐厅",
                "orderStatus": "订单已完成",
                "orderProductList": [
                    {
                        "productCode": "520689",
                        "productName": "小杯鲜萃咖啡",
                        "quantity": 1,
                        "comboItemList": [],
                    }
                ],
                "realTotalAmount": "10",
            },
            {
                "orderId": "1030711850000567244682228887",
                "orderType": "1",
                "createTime": "2026-01-01 15:44:16",
                "beType": "1",
                "beCode": "",
                "storeCode": "1960183",
                "storeName": "麦当劳天门万达广场餐厅",
                "orderStatus": "订单已完成",
                "orderProductList": [
                    {
                        "productCode": "9900004064",
                        "productName": "精选单人餐三件套",
                        "quantity": 1,
                        "comboItemList": [
                            {"itemName": "新升级板烧鸡腿堡", "itemQuantity": 1},
                            {"itemName": "中薯条", "itemQuantity": 1},
                            {"itemName": "鲜萃冰咖", "itemQuantity": 1},
                        ],
                    }
                ],
                "realTotalAmount": "36.5",
            },
        ]
    },
}

REAL_ORDER_DETAIL: dict[str, Any] = {
    "success": True,
    "code": 200,
    "message": "请求成功",
    "data": {
        "orderId": "1030711850000567244682228887",
        "orderStatus": "订单已完成",
        "status": "40",
        "storeName": "麦当劳天门万达广场餐厅",
        "storeAddress": "竟陵陆羽大道与竟东路交汇处万达广场",
        "orderProductList": [
            {
                "productName": "精选单人餐三件套",
                "quantity": 1,
                "price": "36.5",
                "comboItemList": [
                    {"itemName": "新升级板烧鸡腿堡", "itemQuantity": 1},
                    {"itemName": "中薯条", "itemQuantity": 1},
                    {"itemName": "鲜萃冰咖", "itemQuantity": 1},
                ],
            }
        ],
        "totalAmount": "36.5",
        "realTotalAmount": "36.5",
        "totalDiscountAmount": "0",
        "couponList": [],
        "createTime": "2026-01-01 15:44:16",
        "deliveryPrice": "0",
        "realDeliveryPrice": "0",
        "productPrice": "36.5",
        "takeWay": "外带",
    },
}

# 带优惠的真实结构变体：验证原价 / 实付 / 优惠 / 券 能被正确拆分
REAL_DISCOUNTED_DETAIL: dict[str, Any] = {
    "success": True,
    "code": 200,
    "data": {
        "orderId": "X-DISCOUNT-1",
        "orderProductList": [
            {"productName": "巨无霸", "quantity": 1, "price": "25.5", "comboItemList": []},
            {"productName": "中薯条", "quantity": 1, "price": "13.0", "comboItemList": []},
        ],
        "totalAmount": "47.0",
        "realTotalAmount": "21.9",
        "totalDiscountAmount": "25.1",
        "couponList": [{"couponName": "1+1随心配"}],
        "createTime": "2026-06-18 12:30:00",
        "beType": "1",
    },
}

REAL_ACCOUNT: dict[str, Any] = {
    "success": True,
    "code": 200,
    "data": {
        "availablePoint": "46.5",
        "accumulativePoint": "61.5",
        "currency": "麦享会积分",
        "currentMouthExpirePoint": "0",
        "expiredPoint": "15",
        "frozenPoint": "0",
        "usedPoint": "0",
    },
}

REAL_AVAILABLE_COUPONS: dict[str, Any] = {
    "success": True,
    "code": 200,
    "data": [
        {
            "couponName": "中薯条买一送一",
            "couponImage": "https://img.mcd.cn/cms/images/88890902eaa7a54d.png",
            "couponStatus": "CAN_GET",
            "label": "可领取",
        },
        {
            "couponName": "王牌小食拼",
            "couponStatus": "CAN_GET",
            "label": "可领取",
        },
    ],
}


def test_real_mcp_contract(t: Runner) -> None:
    # ---- 1. order-list 真实结构 ---------------------------------------
    orders = parse_orders(REAL_ORDER_LIST)
    t.eq("真实 order-list 解析出 2 单", len(orders), 2)

    first, combo = orders[0], orders[1]
    # ⚠️ 回归断言：orderProductList 必须在别名表里，否则明细整体为空
    t.eq("真实明细键 orderProductList 可解析", len(first.items), 1)
    t.eq("真实餐品名", first.items[0].name, "小杯鲜萃咖啡")
    t.eq("真实数量", first.items[0].qty, 1)
    # ⚠️ 回归断言：realTotalAmount 必须被识别为实付
    t.close("realTotalAmount 识别为实付", first.paid, 10.0)
    t.close("仅有实付时原价不虚构", first.gross, 10.0)
    # ⚠️ 回归断言：beType/orderType 是数字编码，必须能映射
    t.eq("beType=1 映射为到店取餐", first.channel, "dine_in")
    t.eq("真实门店名", first.store, "麦当劳天门万达广场餐厅")

    # ---- 2. 套餐拆解 ---------------------------------------------------
    t.eq("套餐行被识别为套餐", combo.items[0].is_combo, True)
    t.eq("套餐拆出 3 个子项", len(combo.items[0].components), 3)
    t.eq(
        "套餐子项名正确",
        [c[0] for c in combo.items[0].components],
        ["新升级板烧鸡腿堡", "中薯条", "鲜萃冰咖"],
    )

    combo_metrics = compute_metrics(orders)
    t.eq("套餐订单计数", combo_metrics.combo_orders, 1)
    t.check("单品榜含套餐拆解结果", len(combo_metrics.top_components) == 3,
            f"实际 {[c.name for c in combo_metrics.top_components]}")
    # 咖啡 = 独立的「小杯鲜萃咖啡」1 份 + 套餐拆出的「鲜萃冰咖」1 份
    t.close("拆解后咖啡品类含量为 2", combo_metrics.category_counts.get("咖啡", 0), 2)
    t.close("拆解后汉堡品类含量为 1", combo_metrics.category_counts.get("汉堡", 0), 1)
    t.close("拆解后小食品类含量为 1", combo_metrics.category_counts.get("小食", 0), 1)

    # ---- 3. query-order 真实结构（含原价 / 优惠 / 券）------------------
    # 注意：真实使用时 detail 会被合并进 order-list 的列表项再统一解析，
    # 所以这里也走同一条路径，而不是单独解析一个 detail 对象。
    detail = parse_orders([REAL_ORDER_DETAIL["data"]])[0]
    t.close("totalAmount 识别为原价", detail.gross, 36.5)
    t.close("realTotalAmount 识别为实付", detail.paid, 36.5)
    t.eq("明细行带单价", detail.items[0].unit_price, 36.5)

    disc = parse_orders([REAL_DISCOUNTED_DETAIL["data"]])[0]
    t.close("有优惠时原价正确", disc.gross, 47.0)
    t.close("有优惠时实付正确", disc.paid, 21.9)
    t.close("有优惠时省下正确", disc.saved, 25.1)
    t.eq("券名被解析", disc.coupons, ["1+1随心配"])
    t.check("有优惠单被标记", disc.had_discount is True)

    # ---- 3b. 补全合并路径（order-list 行 + query-order 详情）------------
    # 复现 _enrich_orders 的合并逻辑：详情为主体，从列表项补齐渠道字段。
    list_row = REAL_ORDER_LIST["data"]["list"][1]
    detail_data = dict(REAL_ORDER_DETAIL["data"])
    detail_data["totalAmount"] = "47.0"
    detail_data["realTotalAmount"] = "21.9"
    detail_data["totalDiscountAmount"] = "25.1"
    detail_data["couponList"] = [{"couponName": "1+1随心配"}]
    merged = dict(detail_data)
    for key in ("orderType", "beType", "beCode", "storeCode", "storeName", "orderStatus"):
        if merged.get(key) in (None, "") and list_row.get(key) not in (None, ""):
            merged[key] = list_row[key]

    m_orders = parse_orders([merged])
    t.eq("合并后仍能解析为 1 单", len(m_orders), 1)
    t.close("合并后原价来自详情", m_orders[0].gross, 47.0)
    t.close("合并后实付来自详情", m_orders[0].paid, 21.9)
    t.close("合并后省下金额正确", m_orders[0].saved, 25.1)
    t.eq("合并后渠道来自列表项", m_orders[0].channel, "dine_in")
    t.eq("合并后门店名保留", m_orders[0].store, "麦当劳天门万达广场餐厅")
    t.eq("合并后券名保留", m_orders[0].coupons, ["1+1随心配"])

    # ---- 4. 渠道编码映射（官方 inputSchema 定义）-----------------------
    t.eq("beType=2 麦乐送", normalize_channel(None, be_type="2"), "delivery")
    t.eq("beType=5 得来速", normalize_channel(None, be_type="5"), "drive_thru")
    t.eq("beType=6 团餐", normalize_channel(None, be_type="6"), "group")
    t.eq("orderType=2 兜底为外送", normalize_channel(None, order_type="2"), "delivery")
    t.eq("beType 优先级高于 orderType", normalize_channel(None, be_type="5", order_type="1"), "drive_thru")
    t.eq("整数形式的编码也能映射", normalize_channel(None, be_type=1), "dine_in")

    # ---- 5. 品类识别 ---------------------------------------------------
    t.eq("品类 咖啡", classify_item("小杯鲜萃咖啡"), "咖啡")
    t.eq("品类 咖啡（冰咖）", classify_item("鲜萃冰咖"), "咖啡")
    t.eq("品类 汉堡（板烧）", classify_item("新升级板烧鸡腿堡"), "汉堡")
    t.eq("品类 小食（薯条）", classify_item("中薯条"), "小食")
    t.eq("品类 甜品（麦旋风）", classify_item("奥利奥麦旋风"), "甜品")
    t.eq("无法识别归入其他", classify_item("精选单人餐三件套"), "其他")

    # ---- 6. 积分账户 ---------------------------------------------------
    acc = parse_account(REAL_ACCOUNT)
    t.close("可用积分", acc.available, 46.5)
    t.close("累计积分", acc.accumulative, 61.5)
    t.close("已过期积分", acc.expired, 15.0)
    t.close("临期风险积分", acc.at_risk, 0.0)
    t.eq("积分币种", acc.currency, "麦享会积分")
    t.eq("空账户不崩溃", parse_account(None).available, 0.0)

    # ---- 7. 券解析（真实只有 couponName / couponStatus）-----------------
    real_coupons = parse_coupons(REAL_AVAILABLE_COUPONS, source="available")
    t.eq("真实券可解析", len(real_coupons), 2)
    t.eq("真实券名", real_coupons[0].name, "中薯条买一送一")
    t.eq("真实券来源", real_coupons[0].source, "available")
    t.eq("缺 couponId 时自动编号", real_coupons[0].coupon_id, "CPN-0000")

    # ---- 7b. 活动日历（真实结构：data.dailyList[].events[].activityTitle）----
    # ⚠️ 真实数据里 activityTitle 恒为空字符串，真标题在 articleDto.title
    real_campaigns = {
        "success": True,
        "code": 200,
        "data": {
            "currentTime": "2026-10-16 14:39:51",
            "dailyList": [
                {
                    "date": "2026-10-12",
                    "today": False,
                    "events": [
                        {"activityTitle": "", "articleDto": {"title": "往期活动不该出现"}}
                    ],
                },
                {
                    "date": "2026-10-16",
                    "today": True,
                    "events": [
                        {"activityTitle": "", "articleDto": {"title": "麦当劳联动G-DRAGON"}},
                        {"activityTitle": "", "articleDto": {"title": "麦当劳 X PEACEMINUSONE"}},
                        {"activityTitle": "", "articleDto": {"title": "麦当劳 X PEACEMINUSONE"}},  # 重复
                        {"activityTitle": "", "articleDto": {"highlights": "仅 highlights 可用"}},
                        {"activityTitle": "直接给标题的活动"},
                        {"activityTitle": "", "articleDto": {}},  # 完全无标题，应跳过
                    ],
                },
            ],
            "subscribedEvents": [],
        },
    }
    titles = parse_campaign_titles(real_campaigns)
    t.eq(
        "活动标题从 articleDto.title 解析",
        titles,
        ["麦当劳联动G-DRAGON", "麦当劳 X PEACEMINUSONE", "仅 highlights 可用", "直接给标题的活动"],
    )
    t.eq("往期日期不混入", "往期活动不该出现" in titles, False)
    t.eq("标题去重", len(titles), len(set(titles)))
    t.eq("空活动不崩溃", parse_campaign_titles(None), [])
    t.eq("limit 生效", len(parse_campaign_titles(real_campaigns, limit=2)), 2)

    # ---- 8. 积分审计结论 -----------------------------------------------
    real_orders = parse_orders([REAL_ORDER_DETAIL["data"], REAL_DISCOUNTED_DETAIL["data"]])
    t.eq("真实订单可进入统计", len(real_orders), 2)
    rm = compute_metrics(real_orders)
    ra = run_audit(real_orders, rm, real_coupons, acc)
    t.close("审计带出已过期积分", ra.account_expired, 15.0)
    t.check(
        "已过期积分触发告警",
        any(f.level == LEVEL_ALERT and "过期" in f.title for f in ra.findings),
        f"实际结论：{[f.title for f in ra.findings]}",
    )
    t.check(
        "结论含可领未领券",
        any("能领" in f.title for f in ra.findings),
        f"实际结论：{[f.title for f in ra.findings]}",
    )

    # 无订单但有积分问题时，审计结论不能为空
    empty_audit = run_audit([], compute_metrics([]), [], acc)
    t.check(
        "零订单时积分结论仍成立",
        any("过期" in f.title for f in empty_audit.findings),
        f"实际 {[f.title for f in empty_audit.findings]}",
    )

    # ---- 9. 关键回归：若漏掉 realTotalAmount，审计会静默失效 -------------
    degraded = parse_orders(
        [
            {
                "orderId": "D1",
                "beType": "1",
                "createTime": "2026-03-01 12:00:00",
                "orderProductList": [{"productName": "巨无霸", "quantity": 1}],
                "realTotalAmount": "25.5",
            }
        ]
    )
    t.close("未识别 realTotalAmount 会导致实付为 0（本断言用于锁死修复）", degraded[0].paid, 25.5)

    # ---- 10. HTML 能承载真实结构 ---------------------------------------
    html = render_html(rm, [], ra, headline="测试", account=acc, generated_at=datetime(2026, 10, 16, 15, 0))
    t.check("真实数据可生成 HTML", "麦门账本" in html)
    t.check("HTML 含积分板块", "你的积分" in html)
    t.check("HTML 含套餐拆解", "把套餐拆开" in html)


# ==========================================================================
# 6. 工程健壮性守卫
#
# 这一组守的是"非功能性但会静默退化"的东西：
# 可复现性、库不得污染 stdout、资源上限是否真的生效。
# 它们的共同点是：坏了也不会报错，只会让承诺变成空话。
# ==========================================================================


def test_engineering_guards(t: Runner) -> None:
    # ---- 1. fixtures 可复现性 -------------------------------------------
    spec = importlib.util.spec_from_file_location(
        "make_fixtures", ROOT / "scripts" / "make_fixtures.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    committed = json.loads((ROOT / "fixtures" / "orders.sample.json").read_text(encoding="utf-8"))
    t.eq("夹具生成器产出与已提交文件完全一致（可复现）", mod.build_orders(), committed)

    gen_src = (ROOT / "scripts" / "make_fixtures.py").read_text(encoding="utf-8")
    t.check(
        "生成器不使用内置 hash()（其随机化会破坏可复现性）",
        "abs(hash(" not in gen_src,
        "PYTHONHASHSEED 会让每次运行结果不同",
    )
    t.eq("餐品编码确定", mod._product_code("巨无霸"), mod._product_code("巨无霸"))

    # ---- 2. 库不得直接 print（污染调用方 stdout）------------------------
    lib_src = (ROOT / "mcledger" / "mcp_client.py").read_text(encoding="utf-8")
    t.check(
        "MCP 库内不含 print（日志必须经 trace 回调）",
        "print(" not in lib_src,
        "库自己写 stdout 会破坏 run.py --json-out | jq 之类的管道用法",
    )
    trace_log: list[str] = []
    probe = MCPClient(token="dummy-token", trace=trace_log.append)
    probe._emit("hello")
    t.eq("trace 回调被正确调用", trace_log, ["hello"])
    silent = MCPClient(token="dummy-token")
    silent._emit("should be dropped")  # 无回调时必须静默，不抛异常
    t.check("未设置 trace 时静默不报错", True)

    # ---- 3. 补全的资源上限按"尝试次数"计 --------------------------------
    class _FailingClient:
        """模拟「接口持续失败」的 MCP 客户端（满足 ToolCaller 协议）。"""

        def __init__(self) -> None:
            self.calls = 0
            self.tools: list[str] = []
            self.arguments: list[dict | None] = []

        def call_tool(self, name: str, arguments: dict | None = None) -> ToolCall:
            self.calls += 1
            self.tools.append(name)
            self.arguments.append(arguments)
            raise MCPError("模拟接口持续失败")

    stub = _FailingClient()
    warnings: list[str] = []
    fake_orders = [{"orderId": f"X{i:03d}"} for i in range(50)]
    enriched = _enrich_orders(stub, fake_orders, limit=5, warnings=warnings)
    t.eq("补全失败时返回 0", enriched, 0)
    t.eq("接口持续失败时仍只发起 5 次请求", stub.calls, 5)
    t.eq("补全只调用 query-order 一个工具", set(stub.tools), {"query-order"})
    t.eq("补全时正确传入 orderId", stub.arguments[0], {"orderId": "X000"})
    t.check(
        "达到上限时给出清楚的人话提示",
        any("只补齐了前" in w and "没算进去" in w for w in warnings),
        f"warnings={warnings[:3]}",
    )

    # ---- 4. 金额字段不会被误当成数量 ------------------------------------
    money_like = parse_orders(
        [{"orderId": "Q1", "items": [{"name": "巨无霸", "amount": "25.5"}]}]
    )[0]
    t.eq("amount（金额）不会被误解析为 26 份", money_like.items[0].qty, 1)

    # ---- 5. 时间解析覆盖微秒格式 ----------------------------------------
    t.eq(
        "parse_datetime 微秒格式",
        parse_datetime("2026-03-05T12:30:00.123456Z"),
        datetime(2026, 3, 5, 12, 30, 0, 123456),
    )
    t.eq(
        "parse_datetime 带微秒的空格格式",
        parse_datetime("2026-03-05 12:30:00.500000"),
        datetime(2026, 3, 5, 12, 30, 0, 500000),
    )

    # ---- 6. 报告 CSS 与审计级别保持同步 ----------------------------------
    # 审计结论的 level 会被直接当作 CSS 类名使用，两边一旦脱钩，
    # 结论块就会静默失去配色。
    report_src = (ROOT / "mcledger" / "report.py").read_text(encoding="utf-8")
    for level in (LEVEL_ALERT, LEVEL_NOTICE, LEVEL_TIP):
        t.check(
            f"报告 CSS 覆盖审计级别 {level}",
            f".finding.{level}" in report_src,
            f"缺少 .finding.{level} 样式，结论块会失去配色",
        )

    # ---- 7. 仓库文本文件不得混用 CRLF ------------------------------------
    crlf_files = [p.name for p in (ROOT / "mcledger").glob("*.py") if b"\r\n" in p.read_bytes()]
    t.eq("mcledger 下无 CRLF 文件", crlf_files, [])

    # ---- 8. 公开 API 必须完整可导入 --------------------------------------
    # __all__ 里写了但实际不存在，是最容易悄悄发生的 API 事故：
    # 文档/skill 里写的 `from mcledger import X` 会在别人机器上炸。
    missing = [name for name in mcledger.__all__ if not hasattr(mcledger, name)]
    t.eq("mcledger.__all__ 中的名字全部存在", missing, [])
    exported = set(mcledger.__all__)
    required = {
        "AccountPoints",
        "AuditResult",
        "Coupon",
        "Metrics",
        "Order",
        "OrderItem",
        "Persona",
        "classify_item",
        "compute_metrics",
        "dedupe_orders",
        "evaluate_personas",
        "headline",
        "money",
        "parse_account",
        "parse_coupons",
        "parse_orders",
        "render_html",
        "render_markdown_summary",
        "run_audit",
        "select_personas",
        "write_report",
    }
    t.eq("公开 API 覆盖全部领域能力", sorted(required - exported), [])

    # ---- 9. 生成物不得带平台相关行尾 --------------------------------------
    # 若写盘时不指定 newline，Windows 会把 \n 转成 \r\n，
    # 同一个输入在 Windows / Linux 上产出的文件字节就不同了。
    with tempfile.TemporaryDirectory() as tmpdir:
        html_path = Path(tmpdir) / "out.html"
        json_path = Path(tmpdir) / "out.json"
        write_report("<html>ok</html>", html_path)
        write_json_snapshot({"a": 1}, json_path)
        t.check("HTML 产物为 LF 行尾", b"\r\n" not in html_path.read_bytes())
        t.check("JSON 产物为 LF 行尾", b"\r\n" not in json_path.read_bytes())
        t.check("JSON 产物以换行结尾", json_path.read_bytes().endswith(b"\n"))

    # ---- 10. 面向用户的文案必须说人话 ------------------------------------
    # 用户不需要知道 order-list / JSON-RPC / --offline 是什么东西。
    # 这些词出现在提示语或报告里，只会让人觉得"这工具看不懂"。
    jargon = (
        "order-list",
        "query-order",
        "query-my-coupons",
        "available-coupons",
        "query-store-coupons",
        "query-meals",
        "query-my-account",
        "campaign-calendar",
        "auto-bind-coupons",
        "calculate-price",
        "list-nutrition-foods",
        "mall-points-products",
        "mall-order-list",
        "query-lottery-info",
        "query-my-prizes",
        "query-nearby-stores",
        "tools/list",
        "streamablehttp",
        "JSON-RPC",
        "--offline",
        "Token",
        "MCP",
    )
    offenders: list[str] = []
    for rel in ("mcledger/mcp_client.py", "mcledger/cli.py"):
        for no, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if "warnings.append" not in line and "warnings.extend" not in line:
                continue
            offenders += [f"{rel}:{no} 含「{word}」" for word in jargon if word in line]
    t.eq("用户可见提示里没有内部术语", offenders, [])

    footer = report_src.split("<footer>")[1].split("</footer>")[0]
    t.eq("报告页脚里没有内部术语", [word for word in jargon if word in footer], [])

    # ---- .gitattributes 必须保护两类「内容不能变」的文件 ----
    # 这两条都不是洁癖：参赛声明被改动会让报名失败（已发生过的真实风险），
    # .ics 的 CRLF 被改写会让导出的日历变成不合规文件 ——
    # 而且**本地完全看不出来**（本地文件没被改），只有别人 clone 下来才会坏。
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    t.check(
        "参赛声明被标记为不可做行尾转换",
        any(line.strip().startswith("CONTEST_DECLARATION.md") and "-text" in line
            for line in attrs.splitlines()),
    )
    t.check(
        "导出的 .ics 被标记为不可做行尾转换（RFC 5545 要求 CRLF）",
        any(line.strip().startswith("*.ics") and "-text" in line
            for line in attrs.splitlines()),
    )
    t.check(
        "报告正文不提命令行参数",
        "--offline" not in report_src,
        "打开 HTML 的人不知道命令行参数是什么",
    )

    # ---- 11. 简报必须带上调用方传入的积分数据 ----------------------------
    # 这条是回归守卫：曾经 render_markdown_summary 被调用时漏传了 account，
    # 结果积分板块永远不出现，而且不报错。
    probe_metrics = compute_metrics(
        parse_orders([{"orderId": "M1", "realTotalAmount": "10", "beType": "1"}])
    )
    probe_account = parse_account({"availablePoint": "88", "expiredPoint": "12"})
    probe_md = render_markdown_summary(
        probe_metrics, [], run_audit([], probe_metrics, [], probe_account), account=probe_account
    )
    t.check("Markdown 简报会显示积分", "你的积分" in probe_md, "传了 account 就必须显示")
    t.check("Markdown 简报会显示过期积分", "12" in probe_md)


# ==========================================================================
# 7. 省钱助手 / 营养搭配 / 习惯年账
# ==========================================================================

SAVER_MENU = Menu(
    source="mcp",
    items=[
        MenuItem("巨无霸", 25.5),
        MenuItem("中薯条", 13.0),
        MenuItem("中可乐", 10.0),
        MenuItem("原味圆筒冰淇淋", 5.0),
        MenuItem("板烧鸡腿堡", 23.5),
    ],
)


def test_coupon_rules(t: Runner) -> None:
    rule = parse_coupon_rule("满40减10")
    t.eq("满减券类型", rule.kind, KIND_THRESHOLD)
    t.close("满减门槛", rule.threshold, 40.0)
    t.close("满减面额", rule.value, 10.0)
    t.close("够门槛时减满额", rule.discount_for(45.0), 10.0)
    t.close("不够门槛时不减", rule.discount_for(30.0), 0.0)
    t.close("还差多少钱", rule.shortfall(30.0), 10.0)

    # ⚠️ 危险回归：曾经把「第2杯半价」当成整单半价，会算出严重偏低的假价
    t.close("第2杯半价绝不按整单五折算", parse_coupon_rule("第2杯半价").discount_for(100.0), 0.0)
    t.eq("第2杯半价归为第N件优惠", parse_coupon_rule("第2杯半价").kind, KIND_NTH_ITEM)

    # 折扣：95 折与 9.5 折都表示"付 95%"
    t.close("95折 = 付 95%", parse_coupon_rule("任意消费95折").value, 0.95)
    t.close("9.5折 = 付 95%", parse_coupon_rule("9.5折").value, 0.95)

    # 适用餐品提取：必须先剥掉"立减X元"再取名字，否则会留下一个"立"字
    t.eq("单品直降的适用范围", parse_coupon_rule("巨无霸立减4元").scope, ("巨无霸",))
    t.check("买一送一归为买赠", parse_coupon_rule("中薯条买一送一").kind == KIND_FREE_GIFT)

    # 门店名限定的是"哪家店"，不是"买哪个餐品"，必须当成全场券
    t.check("门店券按全场处理", parse_coupon_rule("望京SOHO店专属：满30减6").is_general)

    # 券名本身就是商品 —— 不能包装成"买一送一"这种没依据的说法
    t.eq("商品券单独归类", parse_coupon_rule("王牌小食拼").kind, KIND_PRODUCT)
    t.eq("空券名归为无法解析", parse_coupon_rule("").kind, KIND_UNKNOWN)

    # 一次性券标记
    t.check("新客券被标为一次性", parse_coupon_rule("新客立减8元").one_time)
    t.check("普通券不是一次性", not parse_coupon_rule("满40减10").one_time)

    portfolio = CouponPortfolio.from_names(["满40减10", "中薯条买一送一", "第2杯半价", "新客立减8元"])
    t.eq("能算出金额的券数", len(portfolio.computable), 2)
    t.eq("排除一次性后剩余券数", len(portfolio.reusable.rules), 3)
    t.eq("识别出的一次性券", [r.raw for r in portfolio.one_time], ["新客立减8元"])

    brief = "\n".join(coupon_brief(portfolio))
    t.check("券的交代里说明了哪些算不出来", "算不出确定金额" in brief)


def test_saver(t: Runner) -> None:
    portfolio = CouponPortfolio.from_names(["满40减10", "巨无霸立减4元", "任意消费95折"])

    wish = Wish(
        items=[
            WishItem("巨无霸", 1, 25.5),
            WishItem("中薯条", 1, 13.0),
            WishItem("中可乐", 1, 10.0),
        ]
    )
    t.close("想点的原价", wish.gross, 48.5)

    plans = build_plans(wish, portfolio)
    t.check("生成了多个方案", len(plans) >= 3)
    t.eq("最省的方案用满减", plans[0].coupon, "满40减10")
    t.close("最省方案的实付", plans[0].payable, 38.5)
    t.check("方案按实付升序", all(plans[i].payable <= plans[i + 1].payable for i in range(len(plans) - 1)))

    # 凑单判断：加购花的钱 < 券面额才值得
    cheap = Wish(items=[WishItem("巨无霸", 1, 25.5), WishItem("中可乐", 1, 10.0)])  # 35.5
    advice = advise_topup(cheap, portfolio, SAVER_MENU)
    t.check("差一点够满减时给出凑单建议", bool(advice), f"实际 {advice}")
    if advice:
        t.check("加 5 元圆筒换 10 元优惠 = 值得", advice[0].worth_it)
        t.close("净省金额", advice[0].net_change, -5.0)
        t.eq(
            "推荐加购的是最便宜的补位商品",
            advice[0].add_on.name if advice[0].add_on else None,
            "原味圆筒冰淇淋",
        )

    # 差太多就别凑
    small = Wish(items=[WishItem("巨无霸", 1, 25.5)])  # 25.5，差 14.5
    advice2 = advise_topup(small, portfolio, SAVER_MENU)
    t.check("差太多时明确劝退", bool(advice2) and not advice2[0].worth_it, f"实际 {advice2}")
    if advice2:
        t.close("劝退时的净多花金额", advice2[0].net_change, 13.5)

    # 没有菜单时不编造商品
    no_menu = advise_topup(small, portfolio, Menu())
    t.check("没有菜单时不编造补位商品", bool(no_menu) and no_menu[0].add_on is None)

    # 套餐 vs 单点
    check = compare_bundle([MenuItem("巨无霸", 25.5), MenuItem("中薯条", 13.0)], 40.0, "超值套餐")
    t.check("套餐比单点贵时会被识破", not check.bundle_is_cheaper)
    check2 = compare_bundle([MenuItem("巨无霸", 25.5), MenuItem("中薯条", 13.0)], 30.0, "超值套餐")
    t.check("套餐确实更便宜时也说清楚", check2.bundle_is_cheaper)


def test_habits(t: Runner) -> None:
    # 造 6 个一模一样的"周五午餐"，构成一个明确习惯
    base = datetime(2026, 3, 6, 12, 30)  # 2026-03-06 是周五
    orders = [
        _mk_order(
            f"H{i}",
            base + timedelta(days=i * 7),
            "dine_in",
            "店",
            [
                OrderItem("巨无霸", 1, 25.5, 25.5),
                OrderItem("原味圆筒冰淇淋", 1, 5.0, 5.0),
            ],
            30.5,
            30.5,
        )
        for i in range(6)
    ]
    habits = extract_habits(orders, min_occurrences=3)
    t.eq("找出 1 个习惯", len(habits), 1)
    t.eq("习惯出现次数", habits[0].occurrences, 6)
    t.eq("习惯指纹来自餐品组合（按名字排序）", habits[0].signature, ("原味圆筒冰淇淋", "巨无霸"))
    t.eq("识别出是周五", habits[0].top_weekday, 4)
    t.eq("识别出是午餐时段", habits[0].top_daypart, "午餐")
    t.check("习惯描述可读", "周五" in habits[0].label and "午餐" in habits[0].label)

    t.eq("只出现 2 次不算习惯", len(extract_habits(orders[:2], min_occurrences=3)), 0)

    # ⚠️ 关键回归：年度账必须排除一次性券
    # 新客券减 8 元比「满25减5」更划算，但它一辈子只能用一次。
    # 如果不排除，一年的省钱金额会被算得虚高。
    portfolio = CouponPortfolio.from_names(["新客立减8元", "到店取餐满25减5"])
    report = build_habit_report(orders, portfolio, SAVER_MENU)
    t.eq("报告里列出了被排除的一次性券", report.excluded_one_time, ["新客立减8元"])
    t.check("有习惯数据", report.has_data)
    if report.costs:
        # 30.5 元用「满25减5」= 25.5；若误用新客券会变成 22.5
        t.close("年度账用的是可复用的券而非新客券", report.costs[0].optimized_payable, 25.5)
    notes = "\n".join(report.notes())
    t.check("说明了年化方式", "年化方式" in notes)
    t.check("说明了排除一次性券的原因", "一次性" in notes)

    t.check("年化浪费金额为正", project_annual_waste(orders, 100.0) > 0)


def _fact_name(fact: object) -> str | None:
    """取营养数据的名字；匹配不到时返回 None，方便直接断言。"""
    return getattr(fact, "name", None)


def test_nutrition(t: Runner) -> None:
    # 两套字段名都要能认出来
    mixed = {
        "success": True,
        "data": {
            "list": [
                {"foodName": "巨无霸", "calories": 495, "protein": 26, "fat": 25, "carbohydrate": 42},
                {"foodName": "中薯条", "energy": 337, "proteinG": 4, "fatG": 16, "carbohydrateG": 43},
            ]
        },
    }
    table = parse_nutrition_foods(mixed)
    t.eq("两套字段名都能解析", len(table), 2)
    t.close("第一套命名", table["巨无霸"].calories, 495.0)
    t.close("第二套命名", table["中薯条"].calories, 337.0)
    t.close("蛋白质别名匹配", table["中薯条"].protein, 4.0)

    total = total_nutrition([("巨无霸", 2), ("中薯条", 1)], table)
    t.close("合计热量", total.calories, 495 * 2 + 337)
    t.close("能对上营养表的件数", float(total.known_items), 3.0)
    t.close("覆盖率满分", total.coverage, 1.0)

    partial = total_nutrition([("巨无霸", 1), ("不存在的餐品", 1)], table)
    t.close("对不上的会被记为未知", float(partial.unknown_items), 1.0)
    t.close("覆盖率反映真实情况", partial.coverage, 0.5)

    # 约束筛选
    menu = Menu(
        source="mcp",
        items=[MenuItem("巨无霸", 25.5), MenuItem("中薯条", 13.0), MenuItem("中可乐", 10.0)],
    )
    menu_table = parse_nutrition_foods(
        {"data": {"list": [
            {"foodName": "巨无霸", "calories": 495, "protein": 26},
            {"foodName": "中薯条", "calories": 337, "protein": 4},
            {"foodName": "中可乐", "calories": 155, "protein": 0},
        ]}}
    )
    fit = fit_meals(menu, menu_table, FitRequest(max_calories=200))
    t.check("热量上限生效", all(o.total.calories <= 200 for o in fit.options), f"{fit.options}")

    # 至少要有一样主食，避免推荐出"三杯饮料"这种荒唐组合
    drink_menu = Menu(source="mcp", items=[MenuItem("中可乐", 10.0), MenuItem("满杯百香果", 12.0)])
    drink_table = parse_nutrition_foods(
        {"data": {"list": [
            {"foodName": "中可乐", "calories": 155, "protein": 0},
            {"foodName": "满杯百香果", "calories": 179, "protein": 1},
        ]}}
    )
    t.eq("全是饮料时不推荐", len(fit_meals(drink_menu, drink_table, FitRequest()).options), 0)

    # 个人画像
    orders = [
        _mk_order("N1", datetime(2026, 5, 1, 12, 0), "dine_in", "店",
                  [OrderItem("巨无霸", 1, 25.5, 25.5)], 25.5, 25.5),
        _mk_order("N2", datetime(2026, 5, 2, 20, 0), "dine_in", "店",
                  [OrderItem("中薯条", 1, 13.0, 13.0)], 13.0, 13.0),
    ]
    profile = build_nutrition_profile(orders, table)
    t.eq("画像覆盖订单数", profile.orders_covered, 2)
    t.close("画像覆盖率", profile.coverage, 1.0)
    t.close("平均每单热量", profile.avg_calories_per_order, (495 + 337) / 2)
    t.check("画像有品类分布", bool(profile.by_category))
    t.check("画像有描述文字", len(profile.describe()) >= 3)

    # ---- 紧凑表格格式：list-nutrition-foods 的真实返回形态 ----
    # 为了省 token，接口把数组序列化成了一行表头 + 若干行数据的文本。
    # 按 JSON 解析会安静地拿到 0 条 —— 营养画像整块消失却不报错。
    compact = (
        "[3]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,"
        "carbohydrate,sodium,calcium}:\n"
        "  巨无霸,null,2145,513,27,26,42,961,120\n"
        "  可乐中杯,null,616,147,0,0,37,10,0\n"
        "  坏行,缺列\n"
    )
    rows = parse_compact_table(compact)
    t.eq("紧凑表格解析出行数（列数不足的那行被丢掉）", len(rows), 2)
    t.eq("紧凑表格的表头映射正确", rows[0].get("productName"), "巨无霸")
    t.eq("null 被还原成 None（不是字符串 'null'）", rows[0].get("nutritionDescription"), None)
    t.eq("数值列取到的是数值文本", rows[0].get("energyKcal"), "513")
    t.eq("不是这个格式就返回空（绝不猜）", parse_compact_table("{\"a\":1}"), [])
    t.eq("空字符串返回空", parse_compact_table(""), [])

    table2 = parse_nutrition_foods({"data": compact})
    t.eq("从紧凑表格能建出营养表", len(table2), 2)
    t.close("紧凑表格的热量被正确读入", table2["巨无霸"].calories, 513.0)

    # ---- 名字大小杯匹配 ----
    # 营养表叫「可乐中杯」，菜单叫「可乐」—— 直接查字典会一条都匹配不上
    t.eq("菜单名直接查表匹配不上（这就是原来的问题）", table2.get("可乐"), None)
    hit = lookup_fact(table2, "可乐")
    t.eq("放宽后能匹配到中杯", _fact_name(hit), "可乐中杯")
    t.eq("完全一致时优先进口完全一致的", _fact_name(lookup_fact(table2, "巨无霸")), "巨无霸")
    t.eq("匹配不上时返回 None（不硬套别的餐品）", lookup_fact(table2, "完全不存在的东西"), None)
    t.eq("空名字返回 None", lookup_fact(table2, "  "), None)

    # 多个候选时优先「中」规格，且结果稳定
    sizes = parse_nutrition_foods(
        {"data": "[3]{productName,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
                 "  可乐小杯,100,0,0,25,5,0\n"
                 "  可乐大杯,200,0,0,50,15,0\n"
                 "  可乐中杯,147,0,0,37,10,0\n"}
    )
    t.eq("多个规格时优先取中杯", _fact_name(lookup_fact(sizes, "可乐")), "可乐中杯")
    t.eq(
        "同一输入重复调用结果一致",
        _fact_name(lookup_fact(sizes, "可乐")),
        _fact_name(lookup_fact(sizes, "可乐")),
    )
    total2 = total_nutrition([("巨无霸", 1), ("可乐", 1)], table2)
    t.eq("名称对不上时不再被算作「未匹配」", total2.unknown_items, 0)
    t.close("合计热量含匹配到的中杯可乐", total2.calories, 660.0)


# ==========================================================================
# 菜单 / 核价 / 积分钱包 / 日历导出
# ==========================================================================

# 真实 query-meals 的两层结构：categories[].meals[].code + meals{code: {...}}
REAL_MENU = {
    "success": True,
    "code": 200,
    "data": {
        "categories": [
            {"name": "人气\n热卖", "meals": [{"code": "1100", "tags": ["爆款"]}, {"code": "9900004064"}]},
            {"name": "饮品", "meals": [{"code": "2111"}]},
        ],
        "meals": {
            "1100": {"name": "巨无霸", "currentPrice": "26.5", "originalPrice": "0"},
            "9900004064": {"name": "精选单人餐三件套", "currentPrice": "34.5", "originalPrice": "46.5"},
            "2111": {"name": "可乐", "currentPrice": "9.5", "originalPrice": "9.5"},
            # 存在但没有任何品类引用 —— 不能让它凭空消失
            "9999": {"name": "未归类餐品", "currentPrice": "3.0", "originalPrice": "0"},
        },
    },
}

# 真实 calculate-price 的返回：**金额单位是分**
REAL_QUOTE = {
    "success": True,
    "code": 200,
    "data": {
        "originalPrice": 5000,
        "price": 5000,
        "discount": 0,
        "productList": [
            {"productCode": "1100", "productName": "巨无霸", "quantity": 1,
             "originalSubtotal": 2650, "subtotal": 2650},
            {"productCode": "4810", "productName": "中薯条", "quantity": 1,
             "originalSubtotal": 1400, "subtotal": 1400},
            {"productCode": "9900008751", "productName": "可乐", "quantity": 1,
             "originalSubtotal": 950, "subtotal": 950},
        ],
    },
}

# 真实的积分商城：data 是**数组本身**，价值写在商品名里
REAL_POINTS = {
    "success": True,
    "data": [
        {"spuName": "18.8元麦辣鸡腿汉堡两件套", "point": "50", "price": "0", "catName": "到店专用"},
        {"spuName": "21.9元巨无霸可乐组合", "point": "800", "price": "0", "catName": "到店专用"},
        {"spuName": "罗技键鼠套装", "point": "6666", "price": "0", "catName": "玩具"},
        {"spuName": "麦当劳奇妙生日派对", "point": "0", "price": "50", "catName": "生日类派对"},
    ],
}


def test_menu_and_quote(t: Runner) -> None:
    """菜单两层 join 与官方核价 —— 都是最容易静默失效的地方。"""
    menu = menu_from_mcp(REAL_MENU)
    t.eq("菜单解析出全部餐品（含未被品类引用的）", len(menu.items), 4)
    t.eq("菜单来源标记为门店实时", menu.source, "mcp")
    t.check("实时菜单可用于凑单建议", menu.is_live)

    big = menu.find("巨无霸")
    t.eq("菜单里有真实名字与价格", (big.name, big.price) if big else None, ("巨无霸", 26.5))
    t.eq("菜单带品类（且换行符被清理）", big.category if big else None, "人气 热卖")

    combo = menu.find("精选单人餐三件套")
    t.eq("套餐的标称原价被保留", combo.original_price if combo else None, 46.5)
    t.close("菜单标称折扣可算", combo.menu_discount if combo else 0.0, 12.0)

    cola = menu.find("可乐")
    t.eq("原价不高于现价时视为无效", cola.original_price if cola else "x", None)

    # 这条是本次修复的核心证据：旧写法按 data.list 取，一行都拿不到
    naive = rows_of(REAL_MENU, "list", "records", "items")
    t.eq("旧取法（找 list 键）确实拿 0 条 —— 静默失效的根源", len(naive), 0)
    t.eq("空输入不崩溃", len(menu_from_mcp(None).items), 0)
    t.eq("结构不对时返回空菜单而不是抛异常", len(menu_from_mcp({"data": "乱七八糟"}).items), 0)

    # ---- 官方核价 ----
    t.eq("核价单位常量是 100（忘了除就是 100 倍）", CENTS_PER_YUAN, 100)
    quote = parse_official_quote(REAL_QUOTE)
    t.check("核价能被解析", quote is not None)
    if quote:
        t.close("核价应付（分→元）", quote.payable, 50.0)
        t.close("核价原价（分→元）", quote.original, 50.0)
        t.close("三件加起来等于总价", sum(line.payable for line in quote.lines), 50.0)
        t.eq("核价明细行数", len(quote.lines), 3)
        t.eq("明细带餐品名", quote.lines[0].name, "巨无霸")
        t.close("明细小计也换成元", quote.lines[0].payable, 26.5)
        t.check("核价结果标记为有效", quote.is_valid)
    t.eq("空核价返回 None", parse_official_quote(None), None)

    # ---- 门店 ----
    stores = parse_stores(
        {"data": [{"storeCode": "1950564", "storeName": "麦当劳北京建外SOHO(A座)餐厅",
                   "address": "东三环中路39号院", "longitude": 0.0, "latitude": 0.0,
                   "businessStartTime": "07:00", "businessEndTime": "22:00"}]}
    )
    t.eq("门店解析出 1 家", len(stores), 1)
    t.eq("门店名正确", stores[0]["name"] if stores else "", "麦当劳北京建外SOHO(A座)餐厅")
    t.check(
        "门店结构里不含经纬度（官方恒为 0.0，不能当地图坐标）",
        "longitude" not in (stores[0] if stores else {}),
    )
    t.eq("空门店不崩溃", parse_stores(None), [])

    # ---- 门店应当能从历史订单推出来（不依赖用户收藏过餐厅）----
    own = stores_from_orders(
        [
            _mk_order("S1", datetime(2026, 1, 1, 12), "dine_in", "国贸店", [], 30.0, 30.0),
            _mk_order("S2", datetime(2026, 1, 2, 12), "dine_in", "国贸店", [], 40.0, 40.0),
            _mk_order("S3", datetime(2026, 1, 3, 12), "dine_in", "望京店", [], 25.0, 25.0),
        ]
    )
    t.eq("门店去重并按单数排序", [s["name"] for s in own], ["国贸店", "望京店"])
    t.eq("门店带上了点单次数与金额", own[0]["address"], "在这里点过 2 单 · ¥70.00")
    no_store = [_mk_order("S4", None, "dine_in", "", [], 10.0, 10.0)]
    t.eq("没有门店信息时不编造", stores_from_orders(no_store), [])


def test_points_wallet(t: Runner) -> None:
    """积分的账 —— 单位积分价值是这个项目独有的审计点。"""
    wallet = parse_wallet(products_raw=REAL_POINTS)

    t.eq("data 是数组也能解析出商品", len(wallet.products), 4)
    t.eq("point=0 的花钱商品被区分出来", len(wallet.cash_only), 1)
    t.eq("真正能用积分换的商品数", len(wallet.redeemable), 3)
    t.eq("实物商品的每积分价值算不出", wallet.products[2].yuan_per_point, None)
    t.check("实物被标记出来", wallet.products[2].physical)

    ranked = wallet.rankable
    t.eq("可排序的商品数（实物不计入）", len(ranked), 2)
    t.eq("最划算的是名字里价值最高的那件", ranked[0].name, "18.8元麦辣鸡腿汉堡两件套")
    t.close("每积分价值 = 18.8 / 50", ranked[0].yuan_per_point or 0.0, 0.376, tol=0.0005)
    t.close("价值落差 = 0.376 / 0.027375", wallet.value_spread() or 0.0, 13.72, tol=0.02)
    t.close("最便宜的一件要 50 积分", wallet.cheapest_requirement() or 0.0, 50.0)

    # 换不起的时候要如实说，而不是假装能换
    tiny = parse_account({"data": {"availablePoint": "46.5"}})
    lines = wallet.advice(tiny)
    t.check("积分不够时明确说换不了", any("暂时换不了" in x for x in lines))
    t.check("并说明还差多少", any("3.5" in x for x in lines))

    # ---- 抽奖：官方不公布概率，就不许估期望值 ----
    lottery_raw = {
        "data": {
            "activityName": "麦麦积分抽奖",
            "activityStatusText": "进行中",
            "drawPoint": "24",
            "availablePoint": "46.5",
            "prizes": [{"name": "下单立减3元券"}, {"name": "30积分"}, {"name": "下单立减3元券"}],
        }
    }
    lw = parse_wallet(lottery_raw=lottery_raw)
    t.check("抽奖被识别为进行中", bool(lw.lottery and lw.lottery.is_open))
    t.eq("够抽几次算得对（46.5 // 24）", lw.lottery.affordable_draws if lw.lottery else -1, 1)
    t.eq("奖池去重保序", list(lw.lottery.prizes) if lw.lottery else [], ["下单立减3元券", "30积分"])
    advice = lw.advice(tiny)
    t.check("明确声明算不出期望值（不编概率）", any("算不出" in x for x in advice))
    t.check(
        "建议里不出现任何「期望能抽到多少元」这类数字",
        not any("期望" in x for x in advice),
    )

    # ---- 商城订单：data[0].list 这层怪嵌套 ----
    t.eq("真实观察到的空结构不崩溃",
         parse_mall_orders({"data": [{"hasNext": False, "lastId": 0, "list": []}]}), [])
    mall = parse_mall_orders(
        {"data": [{"hasNext": False, "lastId": 0, "list": [
            {"orderId": "M1001", "createTime": "2026-09-01 10:00:00",
             "realTotalAmount": 45.0, "productName": "酸黄瓜吧唧礼盒"}
        ]}]}
    )
    t.eq("商城订单从 data[0].list 解析出来", len(mall), 1)
    t.close("商城订单金额", mall[0].paid if mall else 0.0, 45.0)
    t.eq("商城订单标题", mall[0].title if mall else "", "酸黄瓜吧唧礼盒")
    t.eq("直接解析商品列表也能用（data 是数组）", len(parse_points_products(REAL_POINTS)), 4)
    # 按 data.list 直接取会落到那个分页对象上，一条真订单都拿不到
    page_only = {"data": [{"hasNext": False, "list": []}]}
    naive_orders = [
        r for r in rows_of(page_only) if isinstance(r, dict) and r.get("orderId")
    ]
    t.eq("旧取法（按 data.list 取）拿不到真订单", naive_orders, [])


def test_calendar_export(t: Runner) -> None:
    """活动导出 .ics —— 格式错了日历软件会直接拒收。"""
    events = [
        CalendarEvent("2026-10-16", "1+1随心配"),
        CalendarEvent("2026-10-17", "麦乐送满减日,第二份半价"),
    ]
    stamp = datetime(2026, 10, 16, 8, 0, 0)
    text = build_ics(events, stamp=stamp)

    t.check("有日历头与尾", text.startswith("BEGIN:VCALENDAR") and text.rstrip().endswith("END:VCALENDAR"))
    t.eq("事件数量正确", text.count("BEGIN:VEVENT"), 2)
    t.check("日期用整天格式（VALUE=DATE）", "DTSTART;VALUE=DATE:20261016" in text)
    t.check("逗号被转义", "麦乐送满减日\\,第二份半价" in text)
    t.check("用 CRLF 行尾（RFC 5545 要求）", "\r\n" in text)
    t.check("时间戳按参数固定（结果可复现）", "DTSTAMP:20261016T080000Z" in text)
    t.eq("同一活动两次导出字节一致（UID 稳定）", build_ics(events, stamp=stamp), text)

    long_title = "长" * 60
    folded = build_ics([CalendarEvent("2026-10-16", long_title)], stamp=stamp)
    t.check(
        "超长行按字节折行（中文字符 3 字节，按字符数折会超限）",
        all(len(line.encode("utf-8")) <= 75 for line in folded.split("\r\n")),
    )
    t.eq("非法日期被跳过，不塞错日期进用户日历",
         build_ics([CalendarEvent("bad-date", "x")], stamp=stamp).count("BEGIN:VEVENT"), 0)
    t.check("空事件列表仍产出合法日历",
          build_ics([], stamp=stamp).count("BEGIN:VCALENDAR") == 1)

    # ---- 活动：往期不进日历，且事件要带日期 ----
    campaigns = {
        "data": {
            "currentTime": "2026-10-16 12:00:00",
            "dailyList": [
                {"date": "2026-10-12", "today": False,
                 "events": [{"activityTitle": "", "articleDto": {"title": "往期活动"}}]},
                {"date": "2026-10-16", "today": True,
                 "events": [{"activityTitle": "", "articleDto": {"title": "1+1随心配"}}]},
                {"date": "2026-10-18", "today": False,
                 "events": [{"activityTitle": "", "articleDto": {"title": "会员日"}}]},
            ],
        }
    }
    evs = parse_campaign_events(campaigns)
    t.eq("事件数（今天 + 未来，不含往期）", len(evs), 2)
    t.check("每条事件都带日期", all(e.date for e in evs))
    t.eq("按日期升序", [e.date for e in evs], ["2026-10-16", "2026-10-18"])
    t.eq("往期活动不进日历", any("往期" in e.title for e in evs), False)
    t.eq("空活动不崩溃", parse_campaign_events(None), [])


# ==========================================================================


def main() -> int:
    t = Runner()
    for name, fn in (
        ("解析层", test_parsing),
        ("统计层", test_analytics),
        ("业务层 · 人格", test_personas),
        ("业务层 · 审计", test_audit),
        ("端到端", test_end_to_end),
        ("真实 MCP 契约", test_real_mcp_contract),
        ("工程健壮性守卫", test_engineering_guards),
        ("券规则解析", test_coupon_rules),
        ("省钱助手", test_saver),
        ("习惯年账", test_habits),
        ("营养搭配", test_nutrition),
        ("菜单与官方核价", test_menu_and_quote),
        ("积分的账", test_points_wallet),
        ("日历导出", test_calendar_export),
    ):
        before = len(t.failures)
        fn(t)
        status = "✅" if len(t.failures) == before else "❌"
        print(f"{status} {name}")
    return t.report()


if __name__ == "__main__":
    raise SystemExit(main())
