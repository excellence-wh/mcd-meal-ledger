"""确定性统计引擎 —— 纯标准库、零依赖、可断言。

    这个模块存在的唯一理由：LLM 算不准聚合统计。

    "我今年在麦当劳花了多少钱" 这种问题，让模型自己口算，它会给你一个
    看起来很自信但完全错误的数字。正确做法是 —— 取数交给 MCP，
    算术交给这段代码，表达才交给 Agent。

    同一份输入永远得到同一份输出，因此结果可测试、可复现、可断言。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise

from .models import CHANNEL_LABELS, Order, classify_item

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 时段划分（左闭右开）
DAY_PARTS: tuple[tuple[str, int, int], ...] = (
    ("深夜", 0, 6),
    ("早餐", 6, 11),
    ("午餐", 11, 14),
    ("下午茶", 14, 17),
    ("晚餐", 17, 21),
    ("夜宵", 21, 24),
)


def money(value: float) -> str:
    """统一金额格式：¥1,234.50"""
    return f"¥{value:,.2f}"


def _part_of(hour: int) -> str:
    if hour < 0:
        return "未知"
    for name, start, end in DAY_PARTS:
        if start <= hour < end:
            return name
    return "未知"


# --------------------------------------------------------------------------
# 统计子结构
# --------------------------------------------------------------------------


@dataclass
class ItemStat:
    name: str
    qty: int
    spend: float
    orders: int

    @property
    def avg_unit_price(self) -> float:
        return round(self.spend / self.qty, 2) if self.qty else 0.0


@dataclass
class MonthStat:
    month: str  # YYYY-MM
    orders: int
    paid: float
    saved: float

    @property
    def label(self) -> str:
        return f"{int(self.month[5:7])}月" if len(self.month) == 7 else self.month


@dataclass
class ChannelStat:
    channel: str
    label: str
    orders: int
    paid: float

    @property
    def share(self) -> float:
        return self.orders


@dataclass
class StoreStat:
    name: str
    orders: int
    paid: float


@dataclass
class StreakInfo:
    longest: int = 0
    longest_end: str = ""
    current: int = 0


@dataclass
class Metrics:
    """一份完整的消费画像。字段全部是确定性计算的结果。"""

    total_orders: int = 0
    total_paid: float = 0.0
    total_gross: float = 0.0
    total_saved: float = 0.0
    total_items: int = 0
    total_calories: float = 0.0

    avg_order_value: float = 0.0
    avg_items_per_order: float = 0.0
    avg_saved_per_order: float = 0.0
    save_rate: float = 0.0

    first_order: datetime | None = None
    last_order: datetime | None = None
    active_days: int = 0
    span_days: int = 0

    monthly: list[MonthStat] = field(default_factory=list)
    hourly: list[int] = field(default_factory=lambda: [0] * 24)
    weekday: list[int] = field(default_factory=lambda: [0] * 7)
    daypart: dict[str, int] = field(default_factory=dict)
    top_items: list[ItemStat] = field(default_factory=list)
    top_components: list[ItemStat] = field(default_factory=list)  # 套餐拆解后的单品榜
    channels: list[ChannelStat] = field(default_factory=list)
    stores: list[StoreStat] = field(default_factory=list)
    streaks: StreakInfo = field(default_factory=StreakInfo)
    category_counts: dict[str, int] = field(default_factory=dict)  # 品类 -> 份数
    combo_orders: int = 0  # 含套餐的订单数

    free_orders: int = 0  # 完全没享受任何优惠的单
    discounted_orders: int = 0
    best_order: Order | None = None  # 省得最多的一单
    worst_order: Order | None = None  # 花得最多的一单
    peak_hour: int = -1
    peak_weekday: int = -1
    peak_daypart: str = ""

    @property
    def has_time_data(self) -> bool:
        return self.first_order is not None

    @property
    def free_order_rate(self) -> float:
        return round(self.free_orders / self.total_orders, 4) if self.total_orders else 0.0

    @property
    def discounted_order_rate(self) -> float:
        return round(self.discounted_orders / self.total_orders, 4) if self.total_orders else 0.0

    @property
    def unique_item_count(self) -> int:
        return len(self.top_items)

    def category_share(self, *names: str) -> float:
        """某（几）个品类占总份数的比例。品类缺失时返回 0，不会误判。"""
        total = sum(self.category_counts.values())
        if total <= 0:
            return 0.0
        hit = sum(qty for name, qty in self.category_counts.items() if name in names)
        return hit / total

    def weekday_share(self, days: Iterable[int]) -> float:
        """指定星期（0=周一）的下单占比。"""
        if self.total_orders <= 0 or not self.has_time_data:
            return 0.0
        wanted = set(days)
        hit = sum(self.weekday[d] for d in wanted if 0 <= d < 7)
        return hit / self.total_orders


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def compute_metrics(orders: list[Order], top_n: int = 12) -> Metrics:
    """从订单列表计算全部指标。"""
    m = Metrics()
    if not orders:
        return m

    m.total_orders = len(orders)
    m.total_paid = round(sum(o.paid for o in orders), 2)
    m.total_gross = round(sum(o.gross for o in orders), 2)
    m.total_saved = round(sum(o.saved for o in orders), 2)
    m.total_items = sum(o.total_qty for o in orders)
    m.total_calories = round(sum(o.calories for o in orders), 1)

    m.avg_order_value = round(m.total_paid / m.total_orders, 2)
    m.avg_items_per_order = round(m.total_items / m.total_orders, 2)
    m.avg_saved_per_order = round(m.total_saved / m.total_orders, 2)
    m.save_rate = round(m.total_saved / m.total_gross, 4) if m.total_gross > 0 else 0.0

    timed = [o for o in orders if o.created_at is not None]
    # 显式过滤一次，让类型检查器也能确定这里全是 datetime（而非 Optional）
    stamps: list[datetime] = [o.created_at for o in orders if o.created_at is not None]
    if stamps:
        m.first_order = min(stamps)
        m.last_order = max(stamps)
        m.active_days = len({o.day_key for o in timed})
        m.span_days = max((m.last_order - m.first_order).days + 1, 1)

    # ---- 月度趋势 ----------------------------------------------------
    by_month: dict[str, list[Order]] = defaultdict(list)
    for order in orders:
        by_month[order.month_key].append(order)
    m.monthly = [
        MonthStat(
            month=key,
            orders=len(group),
            paid=round(sum(o.paid for o in group), 2),
            saved=round(sum(o.saved for o in group), 2),
        )
        for key, group in sorted(by_month.items())
    ]

    # ---- 时段 / 星期 -------------------------------------------------
    for order in timed:
        m.hourly[order.hour] += 1
        m.weekday[order.weekday] += 1
        part = _part_of(order.hour)
        m.daypart[part] = m.daypart.get(part, 0) + 1

    if timed:
        m.peak_hour = max(range(24), key=lambda h: m.hourly[h])
        m.peak_weekday = max(range(7), key=lambda d: m.weekday[d])
        m.peak_daypart = max(m.daypart, key=lambda k: m.daypart[k])

    # ---- 单品榜 ------------------------------------------------------
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"qty": 0.0, "spend": 0.0, "orders": 0})
    for order in orders:
        seen: set[str] = set()
        for item in order.items:
            bucket = agg[item.name]
            bucket["qty"] += item.qty
            bucket["spend"] += item.paid_amount or item.gross
            if item.name not in seen:
                bucket["orders"] += 1
                seen.add(item.name)
    m.top_items = sorted(
        (
            ItemStat(name=name, qty=int(v["qty"]), spend=round(v["spend"], 2), orders=int(v["orders"]))
            for name, v in agg.items()
        ),
        key=lambda s: (-s.qty, -s.spend, s.name),
    )[:top_n]

    # ---- 品类分布 + 套餐拆解 -------------------------------------------
    # 注意：top_items 被截断了，品类与套餐统计必须用全量数据，否则占比会失真。
    #
    # 套餐处理原则：带 comboItemList 的行（如「精选单人餐三件套」）
    #   - 拆出的单品进 top_components（用户真正认的是这些名字）；
    #   - 品类计数用拆解后的单品，**不用**套餐本身，避免重复计数；
    #   - 金额仍归套餐行，不对子项分摊（子项没有独立价格，硬分摊会造假数）。
    comp_agg: dict[str, dict[str, float]] = defaultdict(lambda: {"qty": 0.0, "orders": 0})

    def _add_category(label: str, qty: int) -> None:
        m.category_counts[label] = m.category_counts.get(label, 0) + qty

    for order in orders:
        seen_components: set[str] = set()
        for item in order.items:
            if item.components:
                m.combo_orders += 1
                for comp_name, comp_qty in item.components:
                    total_qty = comp_qty * item.qty
                    comp_agg[comp_name]["qty"] += total_qty
                    if comp_name not in seen_components:
                        comp_agg[comp_name]["orders"] += 1
                        seen_components.add(comp_name)
                    _add_category(classify_item(comp_name), total_qty)
            else:
                _add_category(item.category or "其他", item.qty)

    m.top_components = sorted(
        (
            ItemStat(name=name, qty=int(v["qty"]), spend=0.0, orders=int(v["orders"]))
            for name, v in comp_agg.items()
        ),
        key=lambda s: (-s.qty, s.name),
    )[:top_n]

    # ---- 渠道分布 ----------------------------------------------------
    channel_agg: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for order in orders:
        channel_agg[order.channel][0] += 1
        channel_agg[order.channel][1] += order.paid
    m.channels = sorted(
        (
            ChannelStat(
                channel=key,
                label=CHANNEL_LABELS.get(key, key),
                orders=int(v[0]),
                paid=round(v[1], 2),
            )
            for key, v in channel_agg.items()
        ),
        key=lambda c: -c.orders,
    )

    # ---- 门店分布 ----------------------------------------------------
    store_agg: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for order in orders:
        if order.store:
            store_agg[order.store][0] += 1
            store_agg[order.store][1] += order.paid
    m.stores = sorted(
        (StoreStat(name=k, orders=int(v[0]), paid=round(v[1], 2)) for k, v in store_agg.items()),
        key=lambda s: (-s.orders, s.name),
    )[:10]

    # ---- 优惠结构 ----------------------------------------------------
    for order in orders:
        if order.had_discount:
            m.discounted_orders += 1
        else:
            m.free_orders += 1

    if orders:
        m.best_order = max(orders, key=lambda o: (o.saved, -o.paid))
        m.worst_order = max(orders, key=lambda o: o.paid)

    m.streaks = _compute_streaks(stamps)
    return m


def _compute_streaks(stamps: list[datetime]) -> StreakInfo:
    """计算连续下单天数（最长连击 + 当前连击）。"""
    info = StreakInfo()
    if not stamps:
        return info

    days = sorted({stamp.date() for stamp in stamps})
    longest = 1
    current_run = 1
    longest_end = days[0]

    for prev, cur in pairwise(days):
        if (cur - prev).days == 1:
            current_run += 1
        else:
            current_run = 1
        if current_run > longest:
            longest = current_run
            longest_end = cur

    info.longest = longest
    info.longest_end = longest_end.strftime("%Y-%m-%d")

    # 当前连击：从最后一天往前数（later 是较晚的日期）
    tail = 1
    for later, earlier in pairwise(reversed(days)):
        if (later - earlier).days == 1:
            tail += 1
        else:
            break
    info.current = tail
    return info


def monthly_series_svg(monthly: list[MonthStat], width: int = 640, height: int = 180) -> str:
    """月度消费柱状图（内联 SVG，无外部依赖）。"""
    if not monthly:
        return _empty_chart(width, height, "暂无月度数据")

    pad_l, pad_r, pad_t, pad_b = 8, 8, 24, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    peak = max((s.paid for s in monthly), default=0) or 1.0
    slot = plot_w / len(monthly)
    bar_w = min(slot * 0.55, 46)

    bars: list[str] = []
    for index, stat in enumerate(monthly):
        h = max(plot_h * (stat.paid / peak), 2.0)
        x = pad_l + slot * index + (slot - bar_w) / 2
        y = pad_t + plot_h - h
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'rx="5" fill="url(#mcdBar)"/>'
        )
        bars.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle" '
            f'font-size="10" fill="#8a7a63">{stat.paid:.0f}</text>'
        )
        bars.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{pad_t + plot_h + 18:.1f}" text-anchor="middle" '
            f'font-size="11" fill="#6b5c46">{stat.label}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="月度消费趋势">'
        f'<defs><linearGradient id="mcdBar" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="#FFC72C"/><stop offset="100%" stop-color="#DA291C"/>'
        f"</linearGradient></defs>"
        + "".join(bars)
        + "</svg>"
    )


def hourly_series_svg(hourly: list[int], width: int = 640, height: int = 150) -> str:
    """24 小时下单分布（内联 SVG）。"""
    if not any(hourly):
        return _empty_chart(width, height, "暂无时段数据")

    pad_l, pad_r, pad_t, pad_b = 8, 8, 20, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    peak = max(hourly) or 1
    slot = plot_w / 24
    bar_w = max(slot * 0.62, 3)

    bars: list[str] = []
    for hour in range(24):
        count = hourly[hour]
        h = max(plot_h * (count / peak), 1.5)
        x = pad_l + slot * hour + (slot - bar_w) / 2
        y = pad_t + plot_h - h
        color = "#DA291C" if count == peak else "#FFC72C"
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="2.5" fill="{color}"/>'
        )
        if hour % 3 == 0:
            bars.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{pad_t + plot_h + 16:.1f}" text-anchor="middle" '
                f'font-size="10" fill="#8a7a63">{hour}</text>'
            )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="24小时下单分布">' + "".join(bars) + "</svg>"
    )


def weekday_series_svg(weekday: list[int], width: int = 640, height: int = 150) -> str:
    """星期分布（内联 SVG）。"""
    if not any(weekday):
        return _empty_chart(width, height, "暂无星期数据")

    pad_l, pad_r, pad_t, pad_b = 8, 8, 20, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    peak = max(weekday) or 1
    slot = plot_w / 7
    bar_w = min(slot * 0.5, 54)

    bars: list[str] = []
    for day in range(7):
        count = weekday[day]
        h = max(plot_h * (count / peak), 1.5)
        x = pad_l + slot * day + (slot - bar_w) / 2
        y = pad_t + plot_h - h
        color = "#DA291C" if count == peak else "#FFC72C"
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="4" fill="{color}"/>'
        )
        bars.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle" font-size="10" '
            f'fill="#8a7a63">{count}</text>'
        )
        bars.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{pad_t + plot_h + 17:.1f}" text-anchor="middle" '
            f'font-size="11" fill="#6b5c46">{WEEKDAY_NAMES[day]}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="星期分布">' + "".join(bars) + "</svg>"
    )


def _empty_chart(width: int, height: int, text: str) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">'
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" font-size="13" '
        f'fill="#b3a68f">{text}</text></svg>'
    )
