"""钱包的账 —— 点餐之外，你的积分还从哪里流出。

项目原先只算「点餐订单」这一本账。但那只是会员积分流向的一部分：
真实会员的积分还会通过三个口子进出，而这些口子在原版里一笔都没算过：

1. **积分商城兑换**（可换餐品券、实物周边）
2. **积分抽奖**（消耗积分博一次机会）
3. **麦麦商城下单**（用现金买周边/派对）

不算这三本，报告里的「账本」就是残缺的 —— 用户看到「积分 46.5」，
却不知道这 46.5 能换什么、换哪个最划算、以及为什么换不了。

本模块最有价值的产出是**单位积分价值**（元/积分）：

    18.8 元 2 份麦辣鸡翅   = 200 积分  →  0.0940 元/积分
    10.9 元指定小食任选     = 200 积分  →  0.0545 元/积分
    16.9 元麦辣可乐组合     = 500 积分  →  0.0338 元/积分

同样是积分，换错东西**价值差 2.8 倍**。这个数字官方不会告诉你，
第三方工具也没人算 —— 因为要先搞清楚「价值写在商品名里」这件事。

诚实底线：官方**不公布抽奖各奖概率**，所以本模块拒绝估算抽奖期望值，
只列奖池与消耗。宁可说「算不出来」，也不给一个看起来专业的假数字。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analytics import money
from .models import AccountPoints, rows_of, to_float, to_int, unwrap

# 「18.8元2份麦辣鸡翅」→ 18.8
# 只看「数字 + 元」，因为积分商城的 price 字段对券类商品恒为 0，
# 真正的价值只写在名字里。这是官方给的唯一线索。
_CASH_IN_NAME = re.compile(r"(\d+(?:\.\d+)?)\s*元")

# 实物商品（周边、鞋袜等）没有官方标价，无法折算成每积分价值
_PHYSICAL_CATEGORIES = ("实物", "玩具", "鞋袜", "周边")


def cash_value_from_name(name: str) -> float | None:
    """从商品名里读出现金价值；读不出就返回 None（不猜）。"""
    hit = _CASH_IN_NAME.search(name or "")
    if not hit:
        return None
    value = float(hit.group(1))
    return value if value > 0 else None


# --------------------------------------------------------------------------
# 积分商城的商品
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PointsProduct:
    """积分商城里的一件可兑换商品。"""

    name: str
    points: float
    cash_price: float = 0.0  # 官方的现金标价，券类商品恒为 0
    category: str | None = None
    selling: str = ""
    physical: bool = False

    @property
    def cash_value(self) -> float | None:
        """这件东西折成现金大概值多少。算不出就返回 None。"""
        if self.cash_price > 0:
            return round(self.cash_price, 2)
        return cash_value_from_name(self.name)

    @property
    def yuan_per_point(self) -> float | None:
        """每 1 积分能换回多少元 —— **越高越划算**。

        算不出（实物无标价、名字里也没写金额）时返回 None，
        绝不拿一个假数字去排序。
        """
        value = self.cash_value
        if value is None or self.points <= 0:
            return None
        return round(value / self.points, 4)

    def describe(self) -> str:
        value = self.cash_value
        rate = self.yuan_per_point
        if rate is None:
            return f"{self.name} —— {self.points:g} 积分（算不出单价，多半是实物）"
        return (
            f"{self.name} —— {self.points:g} 积分换 {money(value or 0)}，"
            f"每积分 {rate:.4f} 元"
        )


def parse_points_products(payload: object) -> list[PointsProduct]:
    """解析 mall-points-products。

    ⚠️ 它的 ``data`` 是**数组本身**，不是 ``{list: [...]}``。
    按 ``data.list`` 取会得到 0 条、且不报错 —— 又一处静默失效。
    """
    rows = rows_of(payload)
    node = unwrap(payload)
    if not rows and isinstance(node, list):
        rows = [r for r in node if isinstance(r, dict)]

    products: list[PointsProduct] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("spuName") or row.get("name") or "").strip()
        if not name:
            continue
        category = str(row.get("catName") or "").strip() or None
        products.append(
            PointsProduct(
                name=name,
                points=to_float(row.get("point")),
                cash_price=to_float(row.get("price")),
                category=category,
                selling=str(row.get("selling") or "").strip(),
                physical=any(word in (category or "") for word in _PHYSICAL_CATEGORIES),
            )
        )
    return products


# --------------------------------------------------------------------------
# 积分抽奖
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LotteryInfo:
    """积分抽奖活动信息。"""

    activity_name: str = ""
    status_text: str = ""
    draw_points: float = 0.0  # 单次抽奖消耗的积分
    available_points: float = 0.0
    prizes: tuple[str, ...] = ()

    @property
    def affordable_draws(self) -> int:
        """当前积分够抽几次。"""
        if self.draw_points <= 0:
            return 0
        return int(self.available_points // self.draw_points)

    @property
    def is_open(self) -> bool:
        return "进行中" in self.status_text and self.draw_points > 0

    def describe(self) -> str:
        if not self.is_open:
            return f"当前没有进行中的积分抽奖（{self.status_text or '状态未知'}）"
        return (
            f"抽一次要 {self.draw_points:g} 积分，你现在够抽 {self.affordable_draws} 次；"
            f"奖池共 {len(self.prizes)} 种"
        )


def parse_lottery(payload: object) -> LotteryInfo | None:
    """解析 query-lottery-info。没有进行中的活动时返回 None。"""
    node = unwrap(payload)
    if not isinstance(node, dict) or not node:
        return None

    prizes: list[str] = []
    for prize in node.get("prizes") or []:
        if isinstance(prize, dict):
            name = str(prize.get("name") or "").strip()
            if name and name not in prizes:
                prizes.append(name)

    return LotteryInfo(
        activity_name=str(node.get("activityName") or "").strip(),
        status_text=str(node.get("activityStatusText") or "").strip(),
        draw_points=to_float(node.get("drawPoint")),
        available_points=to_float(node.get("availablePoint")),
        prizes=tuple(prizes),
    )


def parse_my_prizes(payload: object) -> list[str]:
    """解析 query-my-prizes —— 我已经抽到过什么。"""
    node = unwrap(payload)
    if not isinstance(node, dict):
        return []
    rows = node.get("prizes")
    if not isinstance(rows, list):
        return []
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("prizeName") or row.get("name") or "").strip()
        if name:
            names.append(name)
    return names


# --------------------------------------------------------------------------
# 麦麦商城订单
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MallOrder:
    """麦麦商城的一笔订单（周边、派对等）。"""

    order_id: str
    created_at: str = ""
    paid: float = 0.0
    title: str = ""


def parse_mall_orders(payload: object) -> list[MallOrder]:
    """解析 mall-order-list。

    ⚠️ 它的 ``data`` 形状很怪：``[{"hasNext":..., "lastId":..., "list":[...]}]``
    —— 数组里包着一层分页对象，真正的订单在 ``data[0].list``。
    按 ``data.list`` 取会拿到 0 条（静默失效）。

    注意：本项目联调所用的账号没有商城订单，因此**订单条目内部字段未经
    真实样本验证**，这里只按通用键名宽容取值；取不到就如实显示为空，
    不编造条目。
    """
    node = unwrap(payload)
    page: object = node
    if isinstance(node, list):
        page = node[0] if node else {}
    if not isinstance(page, dict):
        return []

    rows = page.get("list")
    if not isinstance(rows, list):
        return []

    orders: list[MallOrder] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        order_id = str(row.get("orderId") or row.get("orderNo") or "").strip()
        if not order_id:
            continue
        orders.append(
            MallOrder(
                order_id=order_id,
                created_at=str(row.get("createTime") or row.get("createdAt") or "").strip(),
                paid=to_float(
                    row.get("realTotalAmount")
                    or row.get("payAmount")
                    or row.get("totalAmount")
                ),
                title=str(
                    row.get("productName") or row.get("spuName") or row.get("title") or ""
                ).strip(),
            )
        )
    return orders


# --------------------------------------------------------------------------
# 汇总与结论
# --------------------------------------------------------------------------


@dataclass
class PointsWallet:
    """积分与商城的完整账本。"""

    products: list[PointsProduct] = field(default_factory=list)
    lottery: LotteryInfo | None = None
    my_prizes: list[str] = field(default_factory=list)
    mall_orders: list[MallOrder] = field(default_factory=list)

    @property
    def redeemable(self) -> list[PointsProduct]:
        """真正能用积分兑换的商品。

        积分商城里混着一批 ``point = 0`` 的条目（生日派对、品鉴会、体验营），
        它们是**用现金买**的，不是积分商品。混在一起会让「最便宜要多少积分」
        这类结论失真，所以单独分开。
        """
        return [p for p in self.products if p.points > 0]

    @property
    def rankable(self) -> list[PointsProduct]:
        """能算出单位积分价值的商品，按**由划算到不划算**排序。"""
        items = [p for p in self.redeemable if p.yuan_per_point is not None]
        return sorted(items, key=lambda p: (-(p.yuan_per_point or 0.0), p.points))

    @property
    def cash_only(self) -> list[PointsProduct]:
        """只能花钱买、不能用积分换的条目（派对、品鉴会、体验营）。"""
        return [p for p in self.products if p.points <= 0]

    @property
    def has_data(self) -> bool:
        return bool(self.products or self.lottery or self.mall_orders)

    def affordable(self, points: float) -> list[PointsProduct]:
        """这些积分够换哪些东西。"""
        return [p for p in self.redeemable if p.points <= points]

    def cheapest_requirement(self) -> float | None:
        """最便宜的一件要多少积分 —— 用来说明「你离能换还差多少」。"""
        costs = [p.points for p in self.redeemable]
        return min(costs) if costs else None

    def value_spread(self) -> float | None:
        """最划算与最不划算之间差几倍 —— 用来量化「换错了有多亏」。"""
        rates = [p.yuan_per_point for p in self.rankable]
        if len(rates) < 2:
            return None
        low = min(r for r in rates if r)
        high = max(r for r in rates if r)
        if low <= 0:
            return None
        return round(high / low, 2)

    def advice(self, account: AccountPoints | None) -> list[str]:
        """给出「说人话」的积分使用结论。"""
        lines: list[str] = []
        points = account.available if account else 0.0

        if self.rankable:
            best = self.rankable[0]
            worst = self.rankable[-1]
            lines.append(
                f"同样花积分，换「{best.name}」最值 —— "
                f"{best.points:g} 积分换 {money(best.cash_value or 0)}，"
                f"每积分值 {best.yuan_per_point:.4f} 元"
            )
            spread = self.value_spread()
            if spread and spread >= 1.5:
                lines.append(
                    f"换错就亏：最划算的比最不划算的（{worst.name}，"
                    f"{worst.points:g} 积分）高 {spread:g} 倍"
                )
            lines.append("（按今天的兑换价算，活动价会变，换之前再看一眼）")

        if self.lottery and self.lottery.is_open:
            lines.append(self.lottery.describe())
            lines.append(
                "官方只公布了奖池、没公布各奖概率，所以算不出抽一次平均能抽到多少。"
                "这里只列成本和奖池，不替你估一个数。"
            )

        if self.mall_orders:
            total = round(sum(o.paid for o in self.mall_orders), 2)
            lines.append(
                f"商城另外花掉 {money(total)}（{len(self.mall_orders)} 笔，周边/派对等）"
            )

        requirement = self.cheapest_requirement()
        if requirement and points and points < requirement:
            lines.append(
                f"你现在 {points:g} 积分，暂时换不了东西 —— "
                f"最便宜的一件要 {requirement:g} 积分，还差 {requirement - points:g} 分"
            )

        return lines


def parse_wallet(
    products_raw: object = None,
    lottery_raw: object = None,
    prizes_raw: object = None,
    mall_orders_raw: object = None,
) -> PointsWallet:
    """把四路原始返回合成一个钱包账本。"""
    return PointsWallet(
        products=parse_points_products(products_raw) if products_raw is not None else [],
        lottery=parse_lottery(lottery_raw) if lottery_raw is not None else None,
        my_prizes=parse_my_prizes(prizes_raw) if prizes_raw is not None else [],
        mall_orders=parse_mall_orders(mall_orders_raw) if mall_orders_raw is not None else [],
    )


def parse_int(value: object, default: int = 0) -> int:
    """小工具：给上层做页码等整数解析时用（保持与本模块同一套宽容规则）。"""
    return to_int(value, default=default)
