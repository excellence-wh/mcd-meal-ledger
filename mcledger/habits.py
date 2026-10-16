"""习惯分析 —— 从你的真实订单里挖出"老样子"，再把账算到一年。

    这是本项目唯一别人做不了的事。

    榜上的省钱类项目都是"此时此刻"的工具：告诉它你想吃什么，
    它算当前最省的组合。但它们答不下面这个问题 ——

        「照你现在这样点，一年会多花多少钱？」

    因为它们看不到你的过去。而我们有你完整的历史订单。

    这个模块做两件事：

    1. **找习惯**：把历史订单归类，找出反复出现的点单组合
       （比如"周五午餐：巨无霸 + 中薯条 + 中可乐，出现过 12 次"）。
    2. **算年账**：把这个频率年化，算出照现在的点法一年花多少、
       改用券之后一年能省多少，并给出具体的改造清单。

    方法论上刻意保守：

    - 习惯必须**重复出现**才算习惯（默认至少 3 次），单次不算。
    - 年化系数用**真实观测跨度**推算，不假设"一周一次"这种主观频率。
    - 省下来的钱同样只算能从券名推出确定金额的部分。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .analytics import DAY_PARTS, WEEKDAY_NAMES, money
from .coupons import CouponPortfolio
from .models import Order
from .saver import Menu, Wish, WishItem, best_plan

# 一个习惯至少要出现这么多次才算"习惯"
DEFAULT_MIN_OCCURRENCES = 3


@dataclass
class HabitPattern:
    """一个反复出现的点单习惯。"""

    signature: tuple[str, ...]  # 餐品组合（排序后的名字）
    occurrences: int
    total_paid: float
    node_count: int  # 覆盖了多少个"星期 + 时段"格
    top_weekday: int  # 最常见的星期，-1 表示没有明显规律
    top_daypart: str  # 最常见时段
    top_channel: str
    coupon_rate: float  # 这些单里有多大比例用上了优惠
    avg_paid: float = 0.0
    order_ids: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """给用户看的习惯名，比如「周五午餐」。"""
        when = []
        if self.top_weekday >= 0:
            when.append(WEEKDAY_NAMES[self.top_weekday])
        if self.top_daypart and self.top_daypart != "未知":
            when.append(self.top_daypart)
        return "".join(when) if when else "不定时"

    @property
    def items_text(self) -> str:
        return " + ".join(self.signature)

    def describe(self) -> str:
        return (
            f"{self.label} 你会点「{self.items_text}」，"
            f"一共 {self.occurrences} 次，平均每次 {money(self.avg_paid)}"
        )


def _signature(order: Order) -> tuple[str, ...]:
    """用"套餐展开后的餐品名多重集"作为习惯指纹。

    用多重集而不是集合，是因为份数不同就是不同的点法：
    一份薯条和两份薯条不是同一个习惯。
    套餐会被拆开 —— 这样"精选单人餐三件套"和"板烧+薯条+冰咖"
    会被认成同一个习惯，符合直觉。
    """
    names: list[str] = []
    for item in order.items:
        if item.components:
            for comp_name, comp_qty in item.components:
                names.extend([comp_name] * (comp_qty * item.qty))
        else:
            names.extend([item.name] * item.qty)
    return tuple(sorted(names))


def _dominant(values: list[str | int]) -> tuple[object, int]:
    """找出出现次数最多的取值，以及它被多少个不同取值覆盖过（多样性）。"""
    counts: dict[object, int] = defaultdict(int)
    for v in values:
        counts[v] += 1
    if not counts:
        return None, 0
    best = max(counts, key=lambda k: (counts[k], str(k)))
    return best, len(counts)


def extract_habits(
    orders: list[Order],
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    top_n: int = 5,
) -> list[HabitPattern]:
    """从历史订单里找出反复出现的点单习惯。"""
    buckets: dict[tuple[str, ...], list[Order]] = defaultdict(list)
    for order in orders:
        sig = _signature(order)
        if sig:
            buckets[sig].append(order)

    habits: list[HabitPattern] = []
    for sig, group in buckets.items():
        if len(group) < min_occurrences:
            continue

        paid_list = [o.paid for o in group]
        weekday, _ = _dominant([o.weekday for o in group if o.weekday >= 0])
        daypart, _ = _dominant([_part_of(o) for o in group if o.created_at is not None])
        channel, _ = _dominant([o.channel for o in group])
        used = sum(1 for o in group if o.had_discount)

        habits.append(
            HabitPattern(
                signature=sig,
                occurrences=len(group),
                total_paid=round(sum(paid_list), 2),
                node_count=len({(o.weekday, _part_of(o)) for o in group}),
                top_weekday=int(weekday) if isinstance(weekday, int) else -1,
                top_daypart=str(daypart or "未知"),
                top_channel=str(channel or "unknown"),
                coupon_rate=round(used / len(group), 4),
                avg_paid=round(sum(paid_list) / len(group), 2),
                order_ids=[o.order_id for o in group],
            )
        )

    # 排在有价值的习惯前面：次数多的、金额大的优先
    habits.sort(key=lambda h: (-h.occurrences * h.avg_paid, -h.occurrences))
    return habits[:top_n]


def _part_of(order: Order) -> str:
    """订单落在哪个时段。"""
    if order.created_at is None:
        return "未知"
    hour = order.created_at.hour
    for name, start, end in DAY_PARTS:
        if start <= hour < end:
            return name
    return "未知"


# --------------------------------------------------------------------------
# 年账
# --------------------------------------------------------------------------


@dataclass
class HabitCost:
    """一个习惯的年度账。"""

    habit: HabitPattern
    annual_times: float  # 一年会重复多少次
    annual_spend: float  # 照现在这样点，一年花多少
    annual_savable: float  # 改用券之后，一年能省多少
    advice: str
    optimized_payable: float = 0.0

    @property
    def has_saving(self) -> bool:
        return self.annual_savable >= 1.0

    def explain(self) -> str:
        return self.advice


def _annual_factor(orders: list[Order]) -> float:
    """把观测期内的频次折算成年度频次。

    用真实观测跨度推算，而不是主观假设"一周几次"。
    """
    stamps = [o.created_at for o in orders if o.created_at is not None]
    if not stamps:
        return 0.0
    span_days = max((max(stamps) - min(stamps)).days + 1, 1)
    return 365.0 / span_days


def project_annual(
    orders: list[Order],
    habits: list[HabitPattern],
    portfolio: CouponPortfolio,
    menu: Menu | None = None,
) -> list[HabitCost]:
    """把每个习惯折算成年度账，并算出改用券能省多少。

    ⚠️ 这里刻意只使用 ``portfolio.reusable`` —— 一次性券（新客券之类）
    必须排除在外。否则「每次都用券」会拿一张一辈子只能领一次的券
    乘以一年几十次，把省下的钱算得严重虚高。
    """
    factor = _annual_factor(orders)
    if factor <= 0:
        return []

    reusable = portfolio.reusable
    by_id = {o.order_id: o for o in orders}
    results: list[HabitCost] = []

    for habit in habits:
        # 用这个习惯里的一次真实订单，还原出"想点什么"
        sample = next((by_id[oid] for oid in habit.order_ids if oid in by_id), None)
        if sample is None:
            continue

        wish_items: list[WishItem] = []
        for item in sample.items:
            if item.components:
                for comp_name, comp_qty in item.components:
                    unit = _unit_price_from_menu(menu, comp_name)
                    wish_items.append(
                        WishItem(name=comp_name, qty=comp_qty * item.qty, unit_price=unit)
                    )
            else:
                unit = _unit_price_from_menu(menu, item.name) or item.unit_paid
                wish_items.append(WishItem(name=item.name, qty=item.qty, unit_price=unit))

        plan = best_plan(Wish(items=wish_items), reusable)
        optimized = plan.payable if plan else sample.paid
        annual_times = round(habit.occurrences * factor, 1)
        annual_spend = round(annual_times * habit.avg_paid, 2)
        annual_savable = round(annual_times * max(habit.avg_paid - optimized, 0.0), 2)

        if annual_savable >= 1.0 and plan is not None:
            advice = (
                f"「{habit.items_text}」你一年大概会点 {annual_times:g} 次，"
                f"照现在的点法一年花 {money(annual_spend)}。"
                f"如果每次都用券点（{plan.label}），一次省 "
                f"{money(habit.avg_paid - optimized)}，一年能省 {money(annual_savable)}。"
            )
        elif habit.coupon_rate == 0 and annual_spend > 0:
            advice = (
                f"「{habit.items_text}」一年大概 {annual_times:g} 次，"
                f"一年花 {money(annual_spend)}，而且这几次你一次优惠都没用上。"
                "手上这些券里没找到能直接算清楚金额的，"
                "但去麦麦省看看有没有针对这几样的券，大概率还能再省一点。"
            )
        else:
            advice = (
                f"「{habit.items_text}」一年大概 {annual_times:g} 次，"
                f"一年花 {money(annual_spend)}。"
                "这个组合暂时没找到能算清楚的省钱改法，"
                "不建议为了省钱而换掉你爱吃的东西。"
            )

        results.append(
            HabitCost(
                habit=habit,
                annual_times=annual_times,
                annual_spend=annual_spend,
                annual_savable=annual_savable,
                advice=advice,
                optimized_payable=optimized,
            )
        )

    results.sort(key=lambda c: (-c.annual_savable, -c.annual_spend))
    return results


def _unit_price_from_menu(menu: Menu | None, name: str) -> float:
    """尽量从菜单拿实时单价；拿不到就返回 0，由调用方用历史单价兜底。"""
    if not menu:
        return 0.0
    hit = menu.find(name)
    return hit.price if hit else 0.0


def total_annual_saving(costs: list[HabitCost]) -> float:
    """所有习惯加起来，一年总共能省多少。

    注意：这里只做简单相加，不同习惯之间可能有重叠（比如两个习惯都点了可乐）。
    因此结果标注为「粗略估算」。
    """
    return round(sum(c.annual_savable for c in costs), 2)


@dataclass
class HabitReport:
    """习惯分析的整体结论。"""

    habits: list[HabitPattern] = field(default_factory=list)
    costs: list[HabitCost] = field(default_factory=list)
    span_days: int = 0
    observed_orders: int = 0
    excluded_one_time: list[str] = field(default_factory=list)  # 做长期预测时排除的券
    annual_factor: float = 0.0

    @property
    def total_saving(self) -> float:
        return total_annual_saving(self.costs)

    @property
    def has_data(self) -> bool:
        return bool(self.habits)

    def notes(self) -> list[str]:
        """预测口径的说明 —— 把"怎么算的"和"没算什么"讲清楚。"""
        lines: list[str] = []
        if self.span_days > 0:
            lines.append(
                f"年化方式：你的 {self.observed_orders} 单覆盖 {self.span_days} 天，"
                f"按这个真实跨度折算成一年（系数 {self.annual_factor:.2f}），"
                "不是假设你一周点几次。"
            )
        if self.excluded_one_time:
            lines.append(
                "以下券是一次性的，算年度账时已排除，不然会把省下的钱算得虚高："
                + "、".join(self.excluded_one_time)
            )
        lines.append("习惯之间可能有重叠（比如两个习惯都点了可乐），所以总计是粗略估算。")
        return lines

    def headline(self) -> str:
        if not self.habits:
            return "订单还不够多，暂时看不出稳定的点单习惯（同一个组合至少要出现 3 次）"
        if self.total_saving >= 1:
            return (
                f"从你 {self.observed_orders} 单里认出了 {len(self.habits)} 个固定习惯，"
                f"照现在的点法改一改，一年大概能省 {money(self.total_saving)}（粗略估算）"
            )
        return (
            f"从你 {self.observed_orders} 单里认出了 {len(self.habits)} 个固定习惯，"
            "暂时没找到能算清楚的省钱改法"
        )


def build_habit_report(
    orders: list[Order],
    portfolio: CouponPortfolio,
    menu: Menu | None = None,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
) -> HabitReport:
    """一步到位：找习惯 + 算年账。"""
    habits = extract_habits(orders, min_occurrences=min_occurrences)
    costs = project_annual(orders, habits, portfolio, menu)
    stamps = [o.created_at for o in orders if o.created_at is not None]
    span = max((max(stamps) - min(stamps)).days + 1, 1) if stamps else 0
    return HabitReport(
        habits=habits,
        costs=costs,
        span_days=span,
        observed_orders=len(orders),
        excluded_one_time=[r.raw for r in portfolio.one_time],
        annual_factor=round(_annual_factor(orders), 3),
    )


def project_annual_waste(orders: list[Order], estimated_missed: float) -> float:
    """把「这段时间错过的优惠」按同样口径年化。

    这是比"习惯改造"更直接的年度数字：
    不需要你改变吃什么，只要在同样的点法上把券用上。
    """
    factor = _annual_factor(orders)
    return round(estimated_missed * factor, 2)
