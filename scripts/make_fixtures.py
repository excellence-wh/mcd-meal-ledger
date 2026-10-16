"""生成 fixtures 脱敏样例数据。

为什么要有这个脚本而不是手写 JSON：
  1. 样例数据要足够真实（含跨月、跨渠道、跨时段、含原价单与优惠单）；
  2. 它必须是**可复现**的 —— 固定随机种子，谁跑都一样；
  3. 它还要刻意制造一些"脏数据"（缺字段、异形外壳、字符串金额），
     用来证明 models.py 的宽容取值真的扛得住。

用法：python scripts/make_fixtures.py
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "fixtures"

SEED = 1024
random.seed(SEED)

# 名称, 单价, 热量(kcal)，分类
MENU: list[tuple[str, float, float, str]] = [
    ("巨无霸", 25.5, 495, "汉堡"),
    ("麦辣鸡腿堡", 22.5, 519, "汉堡"),
    ("板烧鸡腿堡", 23.5, 445, "汉堡"),
    ("双层吉士汉堡", 17.0, 437, "汉堡"),
    ("麦香鱼", 16.5, 336, "汉堡"),
    ("麦乐鸡（5块）", 13.5, 228, "小食"),
    ("中薯条", 13.0, 337, "小食"),
    ("大薯条", 15.5, 419, "小食"),
    ("中可乐", 10.0, 155, "饮料"),
    ("大可乐", 11.5, 194, "饮料"),
    ("奥利奥麦旋风", 13.5, 287, "甜品"),
    ("原味圆筒冰淇淋", 5.0, 132, "甜品"),
    ("香芋派", 7.5, 252, "甜品"),
    ("中杯美式", 13.0, 5, "咖啡"),
    ("中杯拿铁", 17.5, 116, "咖啡"),
    ("吉士蛋麦满分", 11.5, 302, "早餐"),
    ("猪柳蛋麦满分", 13.0, 393, "早餐"),
    ("热香饼", 12.0, 331, "早餐"),
    ("满杯百香果", 12.0, 179, "饮料"),
]

# 真实的人不会在菜单上均匀抽样 —— 每个人都有自己的固定偏好。
# 这里的权重故意做成"有口味倾向"的样子，让样例数据更接近真实分布。
ITEM_WEIGHTS = {
    "巨无霸": 14,
    "麦辣鸡腿堡": 11,
    "板烧鸡腿堡": 13,
    "双层吉士汉堡": 6,
    "麦香鱼": 4,
    "麦乐鸡（5块）": 7,
    "中薯条": 13,
    "大薯条": 5,
    "中可乐": 12,
    "大可乐": 5,
    "奥利奥麦旋风": 9,
    "原味圆筒冰淇淋": 11,
    "香芋派": 5,
    "中杯美式": 3,
    "中杯拿铁": 3,
    "吉士蛋麦满分": 9,
    "猪柳蛋麦满分": 7,
    "热香饼": 4,
    "满杯百香果": 5,
}

STORES = ["国贸店", "望京SOHO店", "五道口店"]
STORE_WEIGHTS = [72, 18, 10]  # 大多数人对某一家门店会有明显的路径依赖

# 时段权重：午餐最重，其次晚餐/下午茶，深夜少量
HOUR_POOL = (
    [8] * 4 + [9] * 5 + [10] * 3
    + [11] * 9 + [12] * 12 + [13] * 7
    + [14] * 4 + [15] * 3 + [16] * 3
    + [17] * 6 + [18] * 8 + [19] * 6 + [20] * 4
    + [21] * 3 + [22] * 2 + [23] * 2 + [0] * 1 + [1] * 1
)

COUPONS = [
    "巨无霸单品直降3元",
    "任意消费满35减7",
    "1+1随心配12.9元",
    "中薯条买一送一",
    "麦咖啡第二杯半价",
    "到店取餐满25减5",
    "新客立减8元",
]


def _pick_items(rng: random.Random, *, big: bool) -> list[dict]:
    """按口味权重组一个订单；同一餐品抽中多次则合并份数。big=True 时点得更多。"""
    count = rng.choices([1, 2, 3, 4, 5], weights=[22, 33, 25, 13, 7])[0]
    if big:
        count += 1

    names = [name for name, _, _, _ in MENU]
    weights = [ITEM_WEIGHTS[name] for name in names]
    picked: dict[str, int] = {}
    for _ in range(count):
        name = rng.choices(names, weights=weights)[0]
        picked[name] = picked.get(name, 0) + 1

    by_name = {name: (price, kcal, category) for name, price, kcal, category in MENU}
    items: list[dict] = []
    for name, qty in picked.items():
        price, kcal, category = by_name[name]
        unit = price
        # 9.9 元专区这种单品改价玩法
        if rng.random() < 0.10:
            unit = round(price * 0.7, 1)
        items.append(
            {
                "productName": name,
                "productCode": f"MC{abs(hash(name)) % 90000 + 10000}",
                "quantity": qty,
                "unitPrice": unit,
                "categoryName": category,
                "calories": kcal,
            }
        )
    return items


def build_orders() -> list[dict]:
    rng = random.Random(SEED)
    orders: list[dict] = []
    start = datetime(2026, 1, 6, 12, 0)
    day = start

    index = 0
    while day < datetime(2026, 10, 14):
        # 平均每周 1~3 单，偶尔连续几天连击
        weekday = day.weekday()
        base = {0: 0.34, 1: 0.30, 2: 0.34, 3: 0.42, 4: 0.55, 5: 0.40, 6: 0.26}[weekday]
        for _ in range(rng.choices([0, 1, 2], weights=[1 - base, base * 0.75, base * 0.25])[0]):
            index += 1
            hour = rng.choice(HOUR_POOL)
            minute = rng.choice([3, 11, 17, 24, 28, 35, 41, 47, 52, 58])
            created = day.replace(hour=hour, minute=minute, second=rng.choice([0, 12, 30]))

            channel = rng.choices(
                ["到店取餐", "麦乐送", "得来速"], weights=[64, 27, 9]
            )[0]
            store = rng.choices(STORES, weights=STORE_WEIGHTS)[0]

            items = _pick_items(rng, big=(channel == "到店取餐"))

            gross = round(sum(i["unitPrice"] * i["quantity"] for i in items), 2)

            # 约 27% 的订单是「原价单」—— 这正是审计引擎要抓的漏点
            roll = rng.random()
            if roll < 0.27:
                discount = 0.0
                used_coupons: list[str] = []
                paid = gross
            elif roll < 0.55:
                discount = round(min(3.0 + rng.random() * 4, gross * 0.3), 2)
                used_coupons = [rng.choice(COUPONS)]
                paid = round(gross - discount, 2)
            elif roll < 0.80:
                discount = round(min(7.0, gross * 0.25), 2) if gross >= 35 else round(gross * 0.18, 2)
                used_coupons = ["任意消费满35减7"] if gross >= 35 else ["到店取餐满25减5"]
                paid = round(max(gross - discount, gross * 0.55), 2)
            else:
                discount = round(gross * rng.uniform(0.28, 0.42), 2)
                used_coupons = rng.sample(COUPONS, 2)
                paid = round(max(gross - discount, gross * 0.5), 2)

            delivery_fee = 9.0 if channel == "麦乐送" else 0.0
            if channel == "麦乐送" and rng.random() < 0.4:
                delivery_fee = 0.0  # 满额免配送费

            order: dict = {
                "orderId": f"MC{created.strftime('%Y%m%d')}{index:04d}",
                "orderTime": created.strftime("%Y-%m-%d %H:%M:%S"),
                "orderType": channel,
                "storeName": store,
                "items": items,
                "totalAmount": gross,
                "payAmount": paid,
                "discountAmount": round(discount, 2),
            }
            if used_coupons:
                order["couponList"] = [{"couponName": name} for name in used_coupons]
            if delivery_fee:
                order["deliveryFee"] = delivery_fee
            orders.append(order)

        day += timedelta(days=1)

    # ---- 刻意注入的"脏数据"，验证宽容取值 ----------------------------
    # ① 用字符串金额 + 中文单位
    orders.append(
        {
            "orderId": "MC-DIRTY-0001",
            "orderTime": "2026-09-18 12:40:00",
            "orderType": "到店取餐",
            "storeName": "国贸店",
            "items": [
                {"name": "巨无霸", "qty": 1, "price": "¥25.5元"},
                {"name": "中薯条", "qty": 1, "price": "13"},
            ],
            "totalAmount": "¥38.50",
            "payAmount": "31.50",
            "discountAmount": "7",
        }
    )
    # ② 只有实付、没有原价（应能通过 原价=实付+优惠 反推）
    orders.append(
        {
            "orderNo": "MC-DIRTY-0002",
            "businessTime": 1770000000000,  # 毫秒时间戳
            "channel": "delivery",
            "store": {"name": "望京SOHO店"},
            "productList": [{"mealName": "板烧鸡腿堡", "num": 2, "price": 23.5}],
            "actualAmount": 41.0,
            "totalDiscount": 6.0,
        }
    )
    # ③ 只有原价、没有实付
    orders.append(
        {
            "orderId": "MC-DIRTY-0003",
            "orderTime": "2026-10-02 19:15:00",
            "orderType": "得来速",
            "storeName": "五道口店",
            "items": [{"name": "麦辣鸡腿堡", "qty": 1, "price": 22.5}],
            "totalAmount": 22.5,
        }
    )
    return orders


def build_coupons() -> dict:
    return {
        "available": [
            {"couponId": "A1001", "name": "满40减10", "type": "threshold", "threshold": 40, "value": 10, "expireTime": "2026-10-20 23:59:59"},
            {"couponId": "A1002", "name": "中薯条买一送一", "type": "free_gift", "expireTime": "2026-10-22 23:59:59"},
            {"couponId": "A1003", "name": "巨无霸立减4元", "type": "item_price", "value": 4, "expireTime": "2026-11-08 23:59:59"},
            {"couponId": "A1004", "name": "麦乐送满39免配送费", "type": "threshold", "threshold": 39, "expireTime": "2026-10-19 23:59:59"},
            {"couponId": "A1005", "name": "任意消费95折", "type": "percent", "value": 5, "expireTime": "2026-12-01 23:59:59"},
        ],
        "my": [
            {"couponId": "M2001", "name": "到店取餐满25减5", "type": "threshold", "threshold": 25, "value": 5, "expireTime": "2026-10-18 23:59:59"},
            {"couponId": "M2002", "name": "新客立减8元", "type": "item_price", "value": 8, "expireTime": "2026-11-30 23:59:59"},
        ],
        "store": [
            {"couponId": "S3001", "name": "望京SOHO店专属：满30减6", "type": "threshold", "threshold": 30, "value": 6, "expireTime": "2026-10-25 23:59:59"}
        ],
    }


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    orders = build_orders()
    (FIXTURE_DIR / "orders.sample.json").write_text(
        json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (FIXTURE_DIR / "coupons.sample.json").write_text(
        json.dumps(build_coupons(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total_paid = sum(float(o.get("payAmount") or o.get("actualAmount") or 0) for o in orders)
    print(f"✅ 已生成 {len(orders)} 条脱敏订单 -> fixtures/orders.sample.json")
    print(f"   脱敏实付合计约 ¥{total_paid:,.2f}")
    print("✅ 已生成券样例 -> fixtures/coupons.sample.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
