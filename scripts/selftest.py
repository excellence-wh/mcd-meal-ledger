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

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcledger.analytics import compute_metrics, money  # noqa: E402
from mcledger.audit import LEVEL_ALERT, run_audit  # noqa: E402
from mcledger.mcp_client import parse_account, parse_campaign_titles  # noqa: E402
from mcledger.models import (  # noqa: E402
    Order,
    OrderItem,
    classify_item,
    dedupe_orders,
    normalize_channel,
    parse_coupons,
    parse_datetime,
    parse_orders,
    pick,
    to_float,
    to_int,
)
from mcledger.personas import evaluate_personas, headline, select_personas  # noqa: E402
from mcledger.report import render_html, render_markdown_summary  # noqa: E402


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

    t.eq("省得最多的一单", m.best_order.order_id, "O4")
    t.eq("花得最多的一单", m.worst_order.order_id, "O2")

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

    # 全部原价 → 应出现"全部订单均为原价"
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
    t.check("HTML 含审计板块", "账单审计" in html_a)
    t.check("HTML 含免责声明", "非麦当劳官方产品" in html_a)
    t.check("HTML 自包含（无外链脚本/样式）", "<script" not in html_a and "http" not in html_a.split("<footer>")[0].replace("http-equiv", ""))

    md = render_markdown_summary(m, personas, audit, headline=headline(m))
    t.check("Markdown 摘要含核心数字", "累计实付" in md)
    t.check("Markdown 摘要含审计", "账单优化" in md or "账单审计" in md)


# ==========================================================================
# 5. 真实 MCP 契约测试
#
# 下面的报文是 2026-10-16 真实调用 mcp.mcd.cn 抓回来的原样结构。
# 把它们固化成测试，是为了防止以后有人"优化"别名表时
# 把 orderProductList / realTotalAmount 这类关键字段弄丢 ——
# 那会让整份年报静默变成空壳，而且脱敏样例根本测不出来。
# ==========================================================================

REAL_ORDER_LIST = {
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

REAL_ORDER_DETAIL = {
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
REAL_DISCOUNTED_DETAIL = {
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

REAL_ACCOUNT = {
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

REAL_AVAILABLE_COUPONS = {
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
    real_campaigns = {
        "success": True,
        "code": 200,
        "data": {
            "currentTime": "2026-10-16 14:39:51",
            "dailyList": [
                {
                    "date": "2026-10-16",
                    "today": True,
                    "events": [
                        {"activityTitle": "1+1随心配", "activityStage": 3},
                        {"activityTitle": "麦乐送满减", "activityStage": 3},
                    ],
                },
                {
                    "date": "2026-10-17",
                    "today": False,
                    "events": [{"activityTitle": "下期活动"}],
                },
            ],
            "subscribedEvents": [],
        },
    }
    titles = parse_campaign_titles(real_campaigns)
    t.eq("活动标题被解析", titles, ["1+1随心配", "麦乐送满减"])
    t.eq("非今日活动不混入", "下期活动" in titles, False)
    t.eq("空活动不崩溃", parse_campaign_titles(None), [])

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
        any("可领" in f.title for f in ra.findings),
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
    t.check("HTML 含积分板块", "积分账户" in html)
    t.check("HTML 含套餐拆解", "套餐拆解后的单品榜" in html)


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
    ):
        before = len(t.failures)
        fn(t)
        status = "✅" if len(t.failures) == before else "❌"
        print(f"{status} {name}")
    return t.report()


if __name__ == "__main__":
    raise SystemExit(main())
