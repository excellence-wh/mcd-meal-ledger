"""生成 fixtures 脱敏样例数据。

为什么要有这个脚本而不是手写 JSON：
  1. 样例数据要足够真实（含跨月、跨渠道、跨时段、含原价单与优惠单）；
  2. 它必须是**可复现**的 —— 固定随机种子 + 确定性编码，谁跑都一样；
  3. 它还要刻意制造一些"脏数据"（缺字段、异形外壳、字符串金额），
     用来证明 models.py 的宽容取值真的扛得住。

用法：python scripts/make_fixtures.py
"""

from __future__ import annotations

import json
import random
import sys
import zlib
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcledger.models import dedupe_orders, parse_orders  # noqa: E402

FIXTURE_DIR = ROOT / "fixtures"

SEED = 1024
random.seed(SEED)


def _product_code(name: str) -> str:
    """确定性的餐品编码。

    ⚠️ 这里不能用内置 hash()：Python 对字符串的 hash 带进程级随机化
    （PYTHONHASHSEED），同一份代码两次运行会得到不同的值，
    会让 fixtures 文件每次生成都不一样，"可复现"就成了一句空话。
    crc32 是确定性的，跨进程、跨机器都稳定。
    """
    return f"MC{zlib.crc32(name.encode('utf-8')) % 90000 + 10000}"

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
                "productCode": _product_code(name),
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


def _coupon(cid: str, name: str, kind: str, expire: str, **extra: object) -> dict:
    """构造一张样例券。"""
    return {"couponId": cid, "name": name, "type": kind, "expireTime": expire, **extra}


def build_coupons() -> dict:
    """三路券池的样例：可领取 / 我已有 / 门店专属。"""
    return {
        "available": [
            _coupon("A1001", "满40减10", "threshold", "2026-10-20 23:59:59", threshold=40, value=10),
            _coupon("A1002", "中薯条买一送一", "free_gift", "2026-10-22 23:59:59"),
            _coupon("A1003", "巨无霸立减4元", "item_price", "2026-11-08 23:59:59", value=4),
            _coupon("A1004", "麦乐送满39免配送费", "threshold", "2026-10-19 23:59:59", threshold=39),
            _coupon("A1005", "任意消费95折", "percent", "2026-12-01 23:59:59", value=5),
        ],
        "my": [
            _coupon("M2001", "到店取餐满25减5", "threshold", "2026-10-18 23:59:59", threshold=25, value=5),
            _coupon("M2002", "新客立减8元", "item_price", "2026-11-30 23:59:59", value=8),
        ],
        "store": [
            _coupon(
                "S3001",
                "望京SOHO店专属：满30减6",
                "threshold",
                "2026-10-25 23:59:59",
                threshold=30,
                value=6,
            ),
        ],
    }


# 每份餐品的三大营养素与矿物质：(蛋白质g, 脂肪g, 碳水g, 钠mg, 钙mg)
# 热量沿用 MENU 里的值，不重复写一遍。
_MACROS: dict[str, tuple[float, float, float, float, float]] = {
    "巨无霸": (26, 25, 42, 950, 120),
    "麦辣鸡腿堡": (25, 27, 45, 1100, 90),
    "板烧鸡腿堡": (27, 19, 44, 890, 110),
    "双层吉士汉堡": (24, 23, 33, 900, 210),
    "麦香鱼": (15, 16, 33, 570, 130),
    "麦乐鸡（5块）": (14, 13, 12, 480, 20),
    "中薯条": (4, 16, 43, 260, 20),
    "大薯条": (5, 20, 54, 330, 25),
    "中可乐": (0, 0, 39, 15, 0),
    "大可乐": (0, 0, 49, 19, 0),
    "奥利奥麦旋风": (6, 10, 43, 130, 190),
    "原味圆筒冰淇淋": (3, 4, 21, 55, 100),
    "香芋派": (3, 13, 31, 180, 20),
    "中杯美式": (0, 0, 1, 5, 5),
    "中杯拿铁": (6, 6, 9, 90, 210),
    "吉士蛋麦满分": (15, 15, 26, 780, 260),
    "猪柳蛋麦满分": (21, 24, 28, 900, 230),
    "热香饼": (7, 11, 52, 490, 160),
    "满杯百香果": (1, 0, 44, 25, 20),
}


def build_nutrition() -> dict:
    """营养表样例，外壳用真实接口那种 {success, data: {list: [...]}} 形态。

    ⚠️ 字段名**刻意按奇偶交替混用两套命名**（calories/protein… 与
    energy/proteinG…），用来验证 nutrition.parse_nutrition_foods 的
    别名匹配是真的有效，而不是恰好撞上了唯一一种命名。
    """
    rows: list[dict] = []
    for index, (name, _price, kcal, _category) in enumerate(MENU):
        protein, fat, carbs, sodium, calcium = _MACROS.get(name, (0, 0, 0, 0, 0))
        if index % 2 == 0:
            rows.append(
                {
                    "foodName": name,
                    "calories": kcal,
                    "protein": protein,
                    "fat": fat,
                    "carbohydrate": carbs,
                    "sodium": sodium,
                    "calcium": calcium,
                }
            )
        else:
            rows.append(
                {
                    "foodName": name,
                    "energy": kcal,
                    "proteinG": protein,
                    "fatG": fat,
                    "carbohydrateG": carbs,
                    "sodiumMg": sodium,
                    "calciumMg": calcium,
                }
            )
    return {"success": True, "code": 200, "message": "请求成功", "data": {"list": rows}}


# 演示菜单。
#
# ⚠️ 结构**刻意照抄真实接口的两层形态**：
#     data.categories[].meals[].code  +  data.meals{code: {...}}
# 因为这是最容易写错、且写错了完全不报错的地方 —— 只有把样例做成
# 同样的形状，离线自测才可能拦住「菜单静默变空」这类回归。
# 里面也刻意留了两件带 originalPrice 的套餐，用来验证「菜单标称原价」这条链路。
_DEMO_MENU_ROWS: list[tuple[str, str, float, float, str]] = [
    ("1100", "巨无霸", 26.5, 0.0, "人气热卖"),
    ("1101", "麦辣鸡腿堡", 22.5, 0.0, "人气热卖"),
    ("1102", "板烧鸡腿堡", 23.5, 0.0, "人气热卖"),
    ("2110", "薯条", 14.0, 0.0, "小食甜品"),
    ("2111", "可乐", 9.5, 0.0, "饮品"),
    ("2112", "麦乐鸡", 13.5, 0.0, "小食甜品"),
    ("2113", "圆筒冰淇淋", 5.0, 0.0, "小食甜品"),
    ("2114", "香芋派", 7.5, 0.0, "小食甜品"),
    ("3110", "吉士蛋麦满分", 11.5, 0.0, "早餐"),
    # 带「标称原价」的套餐 —— 官方自己承认的折扣
    ("9900004064", "精选单人餐三件套", 34.5, 46.5, "人气套餐"),
    ("9900005466", "巨无霸三件套", 37.5, 50.5, "人气套餐"),
    ("9900016073", "龙焰鸡腿堡三件套", 26.9, 48.0, "人气套餐"),
]


def build_menu() -> dict:
    """演示菜单（两层结构，与真实接口一致）。"""
    meals: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = {}
    for code, name, price, original, category in _DEMO_MENU_ROWS:
        meals[code] = {
            "name": name,
            "image": "",
            "currentPrice": f"{price}",
            "originalPrice": f"{original}" if original else "0",
            "canWithOrder": False,
        }
        grouped.setdefault(category, []).append({"code": code})
    return {
        "success": True,
        "code": 200,
        "message": "请求成功",
        "data": {
            "categories": [{"name": k, "meals": v} for k, v in grouped.items()],
            "meals": meals,
        },
    }


# 演示用的积分商城商品。
#
# 刻意留下**价值写在商品名里**的形态（price 恒为 0）—— 这正是真实数据的样子，
# 也是「单位积分价值」唯一的数据来源。同时刻意制造一个很大的价值落差，
# 用来验证排序与「换错就亏多少倍」这条结论。
_DEMO_POINTS_PRODUCTS: list[tuple[str, str, str, str, str]] = [
    ("18.8元麦辣鸡腿汉堡两件套", "50", "0", "到店专用", ""),
    ("6.9元香芋派/菠萝派任选", "50", "0", "到店专用", ""),
    ("10.9元指定小食任选", "100", "0", "到店专用", ""),
    ("18.8元2份麦辣鸡翅", "200", "0", "到店专用", ""),
    ("16.9元中薯麦乐鸡组合", "200", "0", "到店专用", ""),
    ("22.9元2份板烧鸡腿堡", "500", "0", "到店专用", ""),
    ("21.9元巨无霸可乐组合", "800", "0", "到店专用", ""),
    ("28.8元巨无霸四件套", "800", "0", "到店专用", ""),
    ("酸黄瓜吧唧礼盒", "800", "0", "玩具", ""),
    ("罗技键鼠套装", "6666", "0", "玩具", ""),
    # point=0 的条目是**用钱买**的，不是积分商品 —— 必须能被正确区分出来
    ("麦当劳奇妙生日派对", "0", "50", "生日类派对", "一场给孩子办的生日会"),
]


def build_points_products() -> dict:
    """演示积分商城。

    注意 ``data`` 直接是数组（真实的接口就是这样），
    不是 ``{list: [...]}`` —— 按后者取会得到 0 条且不报错。
    """
    rows = [
        {
            "spuName": name,
            "spuId": index + 1,
            "spuImage": "",
            "point": points,
            "shopId": 2,
            "selling": selling,
            "catName": category,
            "status": 2,
            "price": cash,
        }
        for index, (name, points, cash, category, selling) in enumerate(_DEMO_POINTS_PRODUCTS)
    ]
    return {"success": True, "code": 200, "message": "请求成功", "data": rows}


def build_lottery() -> dict:
    """演示积分抽奖。奖项**只给名字、不给概率** —— 真实接口就是这样。"""
    prizes = [
        "下单立减3元券",
        "下单立减2元券",
        "下单立减1元券",
        "麦辣三件套5折券",
        "板烧三件套5折券",
        "麦旋风5折券",
        "30积分",
    ]
    return {
        "success": True,
        "code": 200,
        "message": "请求成功",
        "data": {
            "status": {"code": "SUCCESS", "message": "成功"},
            "activityCode": "dtx2",
            "activityStatusText": "进行中",
            "activityName": "麦麦积分抽奖",
            "drawPoint": "24",
            "drawTypeText": "消耗积分抽奖",
            "availablePoint": "46.5",
            "drawDecision": {
                "resourceEligible": True,
                "nextConsumption": {
                    "type": "POINTS",
                    "points": "24",
                    "chances": 0,
                    "text": "本次将消耗 24 积分",
                },
            },
            "prizes": [{"name": n, "imageUrl": "", "typeText": "优惠券"} for n in prizes],
        },
    }


def build_my_prizes() -> dict:
    """我抽到过的奖品。结构照真实接口。"""
    return {
        "success": True,
        "code": 200,
        "message": "请求成功",
        "data": {
            "status": {"code": "SUCCESS", "message": "成功"},
            "prizes": [{"prizeName": "下单立减2元券", "statusText": "已使用"}],
            "pageNum": 1,
            "pageSize": 10,
            "hasMore": False,
            "truncated": False,
        },
    }


def build_mall_orders() -> dict:
    """麦麦商城订单。

    ⚠️ 保留**真实观察到的空结构**：``data`` 是数组，数组里一层分页对象
    （``hasNext`` / ``lastId`` / ``list``）。本项目联调所用的账号确实没有
    商城订单，所以这里如实留空，不编造条目。
    """
    return {
        "success": True,
        "code": 200,
        "message": "请求成功",
        "data": [{"hasNext": False, "lastId": 0, "list": []}],
    }


def build_campaigns() -> dict:
    """活动日历。

    结构照真实的 ``data.dailyList[].events[].articleDto.title``，
    并且刻意让 ``activityTitle`` 为空（真实数据就是这样）——
    只有这样，离线测试才可能拦住「活动标题全空」这类静默失效。
    """
    today = "2026-10-16"

    def _day(date: str, is_today: bool, titles: list[str]) -> dict:
        return {
            "date": date,
            "today": is_today,
            "events": [
                {
                    "activityTitle": "",  # 真实数据里恒为空
                    "activityStage": 3,
                    "articleDto": {"title": t},
                }
                for t in titles
            ],
        }

    return {
        "success": True,
        "code": 200,
        "message": "请求成功",
        "data": {
            "currentTime": f"{today} 12:00:00",
            "dailyList": [
                _day(today, True, ["1+1随心配", "超值 9.9 元早餐两件套"]),
                _day("2026-10-17", False, ["麦乐送满减日"]),
                _day("2026-10-18", False, ["会员日 1 元加购"]),
            ],
            "subscribedEvents": [],
        },
    }


def _write_json(path: Path, payload: object) -> None:
    """统一用 LF + 结尾换行写盘。

    必须显式指定 newline：Windows 下文本模式默认会把 \\n 转成 \\r\\n，
    那样同一份代码在 Windows 与 Linux 上产出的 fixtures 字节不同，
    "可复现"就又成了一句空话。
    """
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
        fh.write("\n")


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    orders = build_orders()
    _write_json(FIXTURE_DIR / "orders.sample.json", orders)
    _write_json(FIXTURE_DIR / "coupons.sample.json", build_coupons())
    _write_json(FIXTURE_DIR / "nutrition.sample.json", build_nutrition())
    _write_json(FIXTURE_DIR / "menu.sample.json", build_menu())
    _write_json(FIXTURE_DIR / "points-products.sample.json", build_points_products())
    _write_json(FIXTURE_DIR / "lottery.sample.json", build_lottery())
    _write_json(FIXTURE_DIR / "my-prizes.sample.json", build_my_prizes())
    _write_json(FIXTURE_DIR / "mall-orders.sample.json", build_mall_orders())
    _write_json(FIXTURE_DIR / "campaigns.sample.json", build_campaigns())

    # 汇总用真实的解析器算，而不是另写一套粗略求和 ——
    # 否则生成脚本打印的数字会和年报里的数字对不上，审查者会以为是 bug。
    parsed = dedupe_orders(parse_orders(orders))
    total_paid = sum(o.paid for o in parsed)
    total_gross = sum(o.gross for o in parsed)

    print(f"✅ 已生成 {len(orders)} 条脱敏订单 -> fixtures/orders.sample.json")
    print(f"   解析后实付合计 ¥{total_paid:,.2f}（原价 ¥{total_gross:,.2f}）")
    print("✅ 已生成券样例 -> fixtures/coupons.sample.json")
    print("✅ 已生成菜单 / 积分商城 / 抽奖 / 商城订单样例")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
