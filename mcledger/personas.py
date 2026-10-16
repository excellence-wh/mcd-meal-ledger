"""消费人格引擎 —— 规则驱动 + 证据可追溯。

    人格标签不是让 LLM 自由发挥编出来的，而是由一组
    可读、可测试、可解释的规则在确定性统计数据上判定出来的。

    每个标签都必须附带 **触发证据**（具体数字），这样：
      1. 结论可被用户核对，不是玄学；
      2. 新增/调整规则不会影响其他标签；
      3. 结果可复现 —— 同样的订单必得同样的标签。

    规则评分 = 该特征"超出阈值多少"，用于在命中多条时挑出
    最能代表这个人的前几条。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .analytics import DAY_PARTS, WEEKDAY_NAMES, Metrics, money

# --------------------------------------------------------------------------


@dataclass
class Persona:
    """一个命中的消费人格标签。"""

    key: str
    emoji: str
    title: str
    tagline: str  # 一句话人设
    evidence: str  # 触发证据（必须带数字）
    score: float  # 匹配强度，越大越贴切

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.emoji} {self.title} (score={self.score:.2f})"


Rule = Callable[[Metrics], "tuple[float, str] | None"]


def _daypart_share(m: Metrics, part: str) -> float:
    if m.total_orders <= 0:
        return 0.0
    return m.daypart.get(part, 0) / m.total_orders


def _channel_share(m: Metrics, channel: str) -> float:
    if m.total_orders <= 0:
        return 0.0
    hit = sum(c.orders for c in m.channels if c.channel == channel)
    return hit / m.total_orders


def _late_night_share(m: Metrics) -> float:
    """22:00 - 04:00 的下单占比。"""
    if m.total_orders <= 0 or not m.has_time_data:
        return 0.0
    count = sum(m.hourly[h] for h in (22, 23, 0, 1, 2, 3))
    return count / m.total_orders


def _scaled(value: float, threshold: float, cap: float = 3.0) -> float:
    """把"超出阈值的程度"折算成 0~cap 的分数。"""
    if threshold <= 0:
        return 0.0
    return min(max(value / threshold - 1.0, 0.0) * 1.2 + 1.0, cap)


# --------------------------------------------------------------------------
# 规则表
# --------------------------------------------------------------------------


def _r_coupon_master(m: Metrics):
    if m.total_orders >= 5 and m.save_rate >= 0.30 and m.discounted_order_rate >= 0.8:
        return (
            _scaled(m.save_rate, 0.30),
            f"{m.total_orders} 单里 {m.discounted_orders} 单用上了优惠，"
            f"整体折扣率 {m.save_rate * 100:.1f}%，累计省下 {money(m.total_saved)}",
        )
    return None


def _r_full_price_hero(m: Metrics):
    if m.total_orders >= 3 and m.free_order_rate >= 0.4:
        return (
            _scaled(m.free_order_rate, 0.40),
            f"{m.free_orders}/{m.total_orders} 单是按原价结的，"
            f"平均每单比用券多花约 {money(m.avg_saved_per_order)}",
        )
    return None


def _r_night_watcher(m: Metrics):
    share = _late_night_share(m)
    if m.total_orders >= 4 and share >= 0.20:
        return (share, f"{share * 100:.0f}% 的单下在 22:00–04:00，共 {int(share * m.total_orders)} 单")
    return None


def _r_dawn_patrol(m: Metrics):
    share = _daypart_share(m, "早餐")
    if m.total_orders >= 4 and share >= 0.30:
        return (share, f"早餐时段占 {share * 100:.0f}%，共 {m.daypart.get('早餐', 0)} 单")
    return None


def _r_noon_clock(m: Metrics):
    share = _daypart_share(m, "午餐")
    if m.total_orders >= 4 and share >= 0.45:
        return (share, f"午餐时段占 {share * 100:.0f}%，共 {m.daypart.get('午餐', 0)} 单")
    return None


def _r_afternoon_survivor(m: Metrics):
    share = _daypart_share(m, "下午茶")
    if m.total_orders >= 4 and share >= 0.25:
        return (share, f"14:00–17:00 占 {share * 100:.0f}%，共 {m.daypart.get('下午茶', 0)} 单")
    return None


def _r_supper_routine(m: Metrics):
    share = _daypart_share(m, "晚餐")
    if m.total_orders >= 5 and share >= 0.50:
        return (share, f"晚餐时段占 {share * 100:.0f}%，共 {m.daypart.get('晚餐', 0)} 单")
    return None


def _r_single_item_devotee(m: Metrics):
    if not m.top_items or m.total_items <= 0:
        return None
    top = m.top_items[0]
    share = top.qty / m.total_items
    if top.qty >= 8 and share >= 0.20:
        return (
            share,
            f"「{top.name}」点了 {top.qty} 份，占全部 {m.total_items} 份餐品的 {share * 100:.0f}%",
        )
    return None


def _r_menu_collector(m: Metrics):
    if m.unique_item_count >= 25:
        return (
            _scaled(float(m.unique_item_count), 25.0),
            f"尝试过 {m.unique_item_count} 种不同餐品，菜单覆盖面相当广",
        )
    return None


def _r_store_loyal(m: Metrics):
    if m.stores and m.total_orders >= 3:
        top = m.stores[0]
        share = top.orders / m.total_orders
        if share >= 0.70:
            return (share, f"{top.orders}/{m.total_orders} 单都在「{top.name}」，占 {share * 100:.0f}%")
    return None


def _r_delivery_resident(m: Metrics):
    share = _channel_share(m, "delivery")
    if m.total_orders >= 3 and share >= 0.60:
        return (share, f"麦乐送占 {share * 100:.0f}%，共 {int(round(share * m.total_orders))} 单")
    return None


def _r_drive_thru_driver(m: Metrics):
    share = _channel_share(m, "drive_thru")
    if m.total_orders >= 4 and share >= 0.30:
        return (share, f"得来速占 {share * 100:.0f}%，共 {int(round(share * m.total_orders))} 单")
    return None


def _r_dine_in_local(m: Metrics):
    share = _channel_share(m, "dine_in")
    if m.total_orders >= 4 and share >= 0.70:
        return (share, f"到店取餐占 {share * 100:.0f}%，共 {int(round(share * m.total_orders))} 单")
    return None


def _r_weekday_ritual(m: Metrics):
    if not m.has_time_data or m.total_orders < 5 or m.peak_weekday < 0:
        return None
    share = m.weekday[m.peak_weekday] / m.total_orders
    if share >= 0.32:
        return (
            share,
            f"{WEEKDAY_NAMES[m.peak_weekday]}是你的主场，{m.weekday[m.peak_weekday]}/{m.total_orders} 单，占 {share * 100:.0f}%",
        )
    return None


def _r_maximalist(m: Metrics):
    if m.total_orders >= 3 and m.avg_items_per_order >= 4.0:
        return (
            _scaled(m.avg_items_per_order, 4.0),
            f"平均每单 {m.avg_items_per_order:.1f} 份餐品，单笔规模可观",
        )
    return None


def _r_minimalist(m: Metrics):
    if m.total_orders >= 3 and 0 < m.avg_items_per_order <= 1.6:
        return (
            1.6 / m.avg_items_per_order,
            f"平均每单只有 {m.avg_items_per_order:.1f} 份，目标明确、绝不加购",
        )
    return None


def _r_streak_king(m: Metrics):
    if m.streaks.longest >= 5:
        return (
            _scaled(float(m.streaks.longest), 5.0),
            f"最长连续 {m.streaks.longest} 天没断过（截至 {m.streaks.longest_end}）",
        )
    return None


def _r_social_diner(m: Metrics):
    share = _channel_share(m, "group")
    if m.total_orders >= 4 and share >= 0.20:
        return (share, f"团餐占 {share * 100:.0f}%，有把麦门搬进办公室/宿舍的倾向")
    return None


def _r_long_hauler(m: Metrics):
    if m.total_orders >= 30 and m.span_days >= 180:
        return (
            _scaled(float(m.total_orders), 30.0),
            f"横跨 {m.span_days} 天留下 {m.total_orders} 笔记录，是实打实的老主顾",
        )
    return None


def _r_peak_hour_sniper(m: Metrics):
    if not m.has_time_data or m.total_orders < 5 or m.peak_hour < 0:
        return None
    share = m.hourly[m.peak_hour] / m.total_orders
    if share >= 0.22:
        return (share, f"{m.peak_hour}:00 前后是你的高频下单窗口，占 {share * 100:.0f}%")
    return None


def _r_big_order_operator(m: Metrics):
    if m.total_orders >= 4 and m.avg_order_value >= 60:
        evidence = f"平均每单 {money(m.avg_order_value)}，单笔规模在全站属于头部"
        if m.worst_order is not None:
            evidence += f"；单笔最高那单花了 {money(m.worst_order.paid)}"
        return (_scaled(m.avg_order_value, 60.0), evidence)
    return None


def _r_small_frequent(m: Metrics):
    if m.total_orders >= 4 and 0 < m.avg_order_value <= 25:
        return (
            25.0 / max(m.avg_order_value, 1.0),
            f"平均每单 {money(m.avg_order_value)}，走的是一份主食就走的路线",
        )
    return None


def _r_dessert_healer(m: Metrics):
    share = m.category_share("甜品", "冰淇淋")
    if m.total_orders >= 4 and share >= 0.12:
        return (share, f"甜品类占全部餐品的 {share * 100:.0f}%，是稳定的情绪补给")
    return None


def _r_coffee_addict(m: Metrics):
    share = m.category_share("咖啡")
    if m.total_orders >= 4 and share >= 0.12:
        return (share, f"咖啡类占全部餐品的 {share * 100:.0f}%，麦咖啡是你的开机键")
    return None


def _r_workday_routine(m: Metrics):
    share = m.weekday_share((0, 1, 2, 3, 4))
    if m.total_orders >= 6 and share >= 0.85:
        return (share, f"{share * 100:.0f}% 的单下在工作日，麦门是通勤动线上的一站")
    return None


RULES: tuple[Rule, ...] = (
    _r_coupon_master,
    _r_full_price_hero,
    _r_night_watcher,
    _r_dawn_patrol,
    _r_noon_clock,
    _r_afternoon_survivor,
    _r_supper_routine,
    _r_single_item_devotee,
    _r_menu_collector,
    _r_store_loyal,
    _r_delivery_resident,
    _r_drive_thru_driver,
    _r_dine_in_local,
    _r_weekday_ritual,
    _r_maximalist,
    _r_minimalist,
    _r_streak_king,
    _r_social_diner,
    _r_long_hauler,
    _r_peak_hour_sniper,
    _r_big_order_operator,
    _r_small_frequent,
    _r_dessert_healer,
    _r_coffee_addict,
    _r_workday_routine,
)

# key -> (emoji, 标题, 一句话人设)
_PERSONA_META: dict[str, tuple[str, str, str]] = {
    "_r_coupon_master": ("🧮", "拆券精算师", "优惠从不落空，麦门券规则背得比菜单还熟"),
    "_r_full_price_hero": ("😇", "原价无名英雄", "从不研究优惠券，到店就点，纯粹得让人心疼"),
    "_r_night_watcher": ("🌙", "巨无霸守夜人", "深夜亮着的灯里，有一盏是为你和薯条留的"),
    "_r_dawn_patrol": ("🌅", "清晨第一缕麦香", "一天的开机键，是早餐套餐"),
    "_r_noon_clock": ("🕛", "午间打卡机", "午休时间的坐标，稳定得像打卡记录"),
    "_r_afternoon_survivor": ("☕", "三点续命人", "下午的困意，需要一个甜筒来解决"),
    "_r_supper_routine": ("🍽️", "晚餐规律主义者", "晚餐这件事，你从不将就也从不缺席"),
    "_r_single_item_devotee": ("🍔", "单品磕到底", "菜单再厚，你的答案只有一个"),
    "_r_menu_collector": ("📖", "菜单集邮家", "来都来了，不妨都尝尝"),
    "_r_store_loyal": ("📍", "门店钉子户", "店员可能已经认识你了"),
    "_r_delivery_resident": ("🛵", "麦乐送常驻员", "沙发到餐桌的距离，是你的主战场"),
    "_r_drive_thru_driver": ("🚗", "得来速老司机", "不下车，不排队，拿了就走"),
    "_r_dine_in_local": ("🏪", "到店派掌门", "现炸的薯条值得等那三分钟"),
    "_r_weekday_ritual": ("📅", "固定日仪式感", "某个星期几，被你和麦门悄悄约定好了"),
    "_r_maximalist": ("💪", "极大满足主义者", "点单从不手软，主打一个尽兴"),
    "_r_minimalist": ("🎯", "极简点单主义者", "目标明确，绝不为满减多买一样"),
    "_r_streak_king": ("🔥", "连击王", "连续下单的纪录，是你和麦门的默契"),
    "_r_social_diner": ("👥", "社交型食客", "一个人的麦门是快餐，一群人的麦门是团建"),
    "_r_long_hauler": ("⏳", "资深麦门老友", "时间够长，数据够厚，故事也够多"),
    "_r_peak_hour_sniper": ("🎯", "高频时段狙击手", "你有一个属于自己的麦门时刻"),
    "_r_big_order_operator": ("🎪", "大单操盘手", "点单从不小气，一单就是一小桌"),
    "_r_small_frequent": ("🍟", "轻食快闪客", "一份主食，拿了就走，效率至上"),
    "_r_dessert_healer": ("🍦", "甜品治愈系", "正餐只是前戏，甜筒才是正题"),
    "_r_coffee_addict": ("☕", "麦咖啡常客", "咖啡因是刚需，麦咖啡是习惯"),
    "_r_workday_routine": ("💼", "工作日规律派", "麦门是你通勤动线上固定的一站"),
}


def evaluate_personas(m: Metrics) -> list[Persona]:
    """跑全部规则，返回按匹配强度排序的人格列表。"""
    hits: list[Persona] = []
    for rule in RULES:
        result = rule(m)
        if result is None:
            continue
        score, evidence = result
        if score <= 0:
            continue
        emoji, title, tagline = _PERSONA_META.get(
            rule.__name__, ("✨", "麦门常客", "麦当劳的熟面孔")
        )
        hits.append(
            Persona(
                key=rule.__name__,
                emoji=emoji,
                title=title,
                tagline=tagline,
                evidence=evidence,
                score=round(score, 4),
            )
        )
    hits.sort(key=lambda p: (-p.score, p.title))
    return hits


def select_personas(m: Metrics, limit: int = 3) -> list[Persona]:
    """挑出最能代表用户的 top-N 人格。"""
    return evaluate_personas(m)[:limit]


def headline(m: Metrics) -> str:
    """给一份数据配一句总纲（不依赖 LLM，纯模板）。"""
    if not m.total_orders:
        return "还没有足够的订单记录，先去麦门创造一些数据吧"
    personas = select_personas(m, limit=1)
    prefix = f"{personas[0].emoji} {personas[0].title}" if personas else "✨ 麦门常客"
    return (
        f"{m.total_orders} 单、{money(m.total_paid)}、省下 {money(m.total_saved)} —— {prefix}"
    )


__all__ = [
    "Persona",
    "RULES",
    "evaluate_personas",
    "select_personas",
    "headline",
    "DAY_PARTS",
    "WEEKDAY_NAMES",
]
