"""优惠审计引擎 —— 把"年度报告"升级成"账单审计"。

    普通的年报告诉你「你花了多少」，然后就没有然后了。

    这个模块多做一件事：**在确定性的历史数据里找出你的钱漏在哪**，
    并且每一条结论都能被追溯到具体订单和具体数字。

    重要原则（也是这个项目的诚实底线）：

        所有「本可省」金额都是 **估算**，且只用用户自己已经实现的
        平均折扣率做基准，绝不虚构一个"理论最优价"来吓人。
        宁可少报，也不给用户一个到店付不出来的假数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .analytics import Metrics, money
from .models import AccountPoints, Coupon, Order

# 审计结论的严重级别
LEVEL_ALERT = "alert"  # 明确在漏钱
LEVEL_NOTICE = "notice"  # 值得注意
LEVEL_TIP = "tip"  # 机会点


@dataclass
class AuditFinding:
    """一条审计发现。"""

    level: str
    title: str
    detail: str
    amount: float = 0.0  # 估算影响金额（元），0 表示无法量化
    action: str = ""  # 可执行的下一步动作


@dataclass
class AuditResult:
    """完整审计结论。"""

    full_price_orders: int = 0
    full_price_paid: float = 0.0
    baseline_discount_rate: float = 0.0  # 用户自己已达成的平均折扣率
    estimated_missed: float = 0.0  # 估算错过金额
    coupon_utilization: float = 0.0  # 用券单占比
    unbindable_coupons: int = 0  # 可领未领的券数量
    expiring_coupons: list[Coupon] = field(default_factory=list)
    stale_top_items: list[tuple[str, int]] = field(default_factory=list)
    total_delivery_fee: float = 0.0
    account_available: float = 0.0  # 可用积分
    account_at_risk: float = 0.0  # 即将过期积分（负资产）
    account_expired: float = 0.0  # 已经过期损失的积分
    findings: list[AuditFinding] = field(default_factory=list)

    @property
    def grade(self) -> str:
        """给用户一个直观的「优惠利用率等级」。"""
        if self.coupon_utilization >= 0.8:
            return "S"
        if self.coupon_utilization >= 0.6:
            return "A"
        if self.coupon_utilization >= 0.4:
            return "B"
        if self.coupon_utilization >= 0.2:
            return "C"
        return "D"

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)


# --------------------------------------------------------------------------


def _baseline_discount_rate(orders: list[Order]) -> float:
    """用户自己已经实现过的平均折扣率 —— 作为"本可省"的估算基准。

    只用真实发生过的优惠水平做基准，不引入任何外部假设。
    """
    discounted = [o for o in orders if o.had_discount and o.gross > 0]
    if not discounted:
        return 0.0
    total_gross = sum(o.gross for o in discounted)
    total_saved = sum(o.saved for o in discounted)
    return round(total_saved / total_gross, 4) if total_gross > 0 else 0.0


def _find_top_items_without_discount(
    orders: list[Order], metrics: Metrics, limit: int = 3
) -> list[tuple[str, int]]:
    """找出"点得很勤但从来没在优惠单里出现过"的餐品。"""
    if not metrics.top_items:
        return []

    discounted_items: set[str] = set()
    for order in orders:
        if order.had_discount:
            discounted_items.update(item.name for item in order.items)

    stale: list[tuple[str, int]] = []
    for stat in metrics.top_items:
        if stat.qty >= 3 and stat.name not in discounted_items:
            stale.append((stat.name, stat.qty))
        if len(stale) >= limit:
            break
    return stale


def run_audit(
    orders: list[Order],
    metrics: Metrics,
    coupons: list[Coupon] | None = None,
    account: AccountPoints | None = None,
) -> AuditResult:
    """执行完整审计。

    account 为积分账户快照；传入后积分层面的审计结论才会生成。
    """
    result = AuditResult()
    if account is not None:
        result.account_available = account.available
        result.account_at_risk = account.at_risk
        result.account_expired = account.expired

    if not orders:
        # 没有订单时，积分层面的结论依然有效，不能一并丢掉
        if result.account_expired > 0 or result.account_at_risk > 0:
            result.findings = _build_points_findings(result)
        return result

    coupons = coupons or []

    # ---- 1. 原价单审计 -------------------------------------------------
    full_price = [o for o in orders if not o.had_discount]
    result.full_price_orders = len(full_price)
    result.full_price_paid = round(sum(o.paid for o in full_price), 2)
    result.baseline_discount_rate = _baseline_discount_rate(orders)
    result.coupon_utilization = metrics.discounted_order_rate

    if result.baseline_discount_rate > 0:
        result.estimated_missed = round(
            result.full_price_paid * result.baseline_discount_rate, 2
        )

    # ---- 2. 可领未领的券 -----------------------------------------------
    result.unbindable_coupons = len([c for c in coupons if c.source in ("available", "store")])
    result.expiring_coupons = [c for c in coupons if c.is_expiring_soon]

    # ---- 3. 高频但从未优惠的餐品 ---------------------------------------
    result.stale_top_items = _find_top_items_without_discount(orders, metrics)

    # ---- 4. 配送费沉淀 -------------------------------------------------
    result.total_delivery_fee = round(sum(o.delivery_fee for o in orders), 2)

    # ---- 5. 汇总成 findings --------------------------------------------
    result.findings = _build_findings(result, orders) + _build_points_findings(result)
    return result


def _build_findings(r: AuditResult, orders: list[Order]) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    # ① 原价单 → 最大的一条
    if r.full_price_orders > 0 and r.baseline_discount_rate > 0:
        rate_pct = r.baseline_discount_rate * 100
        findings.append(
            AuditFinding(
                level=LEVEL_ALERT,
                title=f"有 {r.full_price_orders} 单没用任何优惠",
                detail=(
                    f"这 {r.full_price_orders} 单一共花了 {money(r.full_price_paid)}。"
                    f"你其他订单平均能省下 {rate_pct:.1f}%，"
                    f"按这个水平算，这些单本来还能省约 {money(r.estimated_missed)}（估算）。"
                ),
                amount=r.estimated_missed,
                action="下次点单前，先去麦麦省把能领的券都领了，再下单。",
            )
        )
    elif r.full_price_orders >= 2 and r.baseline_discount_rate == 0:
        findings.append(
            AuditFinding(
                level=LEVEL_ALERT,
                title="每一单都是原价买的",
                detail=(
                    f"这 {r.full_price_orders} 单全是原价买的，一共 {money(r.full_price_paid)}，"
                    "一笔优惠都没用上。因为你还没有用过任何优惠，"
                    "我没法估算能省多少 —— 但能领的券就摆在那里。"
                ),
                amount=0.0,
                action="麦麦省里就有能领的券，点单前顺手领一下是最直接的省钱办法。",
            )
        )

    # ② 可领未领的券
    if r.unbindable_coupons > 0:
        findings.append(
            AuditFinding(
                level=LEVEL_NOTICE,
                title=f"有 {r.unbindable_coupons} 张券能领，但你还没领",
                detail=(
                    f"麦麦省里有 {r.unbindable_coupons} 张券可以领，你现在还没领。"
                    "券不会自己跑到你账户里，得点单前领了才有用。"
                ),
                action="去麦麦省把能领的券都领了，领了才用得上。",
            )
        )

    # ③ 即将过期的券
    if r.expiring_coupons:
        names = "、".join(c.name for c in r.expiring_coupons[:3])
        more = f"等 {len(r.expiring_coupons)} 张" if len(r.expiring_coupons) > 3 else ""
        findings.append(
            AuditFinding(
                level=LEVEL_NOTICE,
                title=f"{len(r.expiring_coupons)} 张券一周内就要过期",
                detail=f"「{names}」{more}就要过期了，过期就作废，找不回来。",
                action="最近几次点单先把这些券用掉，过期就作废了。",
            )
        )

    # ④ 高频单品从未优惠
    if r.stale_top_items:
        detail = "、".join(f"{name}（{qty} 份）" for name, qty in r.stale_top_items)
        findings.append(
            AuditFinding(
                level=LEVEL_TIP,
                title="你常点的几样，从来没用优惠买过",
                detail=f"{detail} 是你的高频单品，但历史订单里它们从未出现在任何优惠单中。",
                action="看看这家店有没有针对这几样的单品券或者套餐价。",
            )
        )

    # ⑤ 配送费沉淀
    if r.total_delivery_fee >= 20:
        findings.append(
            AuditFinding(
                level=LEVEL_TIP,
                title=f"配送费花了 {money(r.total_delivery_fee)}",
                detail=(
                    f"光是外送的配送费，你一共就付了 {money(r.total_delivery_fee)}。"
                    "自己去店里拿或者开车取餐都不收这笔钱，点够一定金额也能免。"
                ),
                action="不是非叫外送不可的时候，可以比比自己去店里拿便宜多少。",
            )
        )

    # ⑥ 单笔最大原价单
    worst_full = [o for o in orders if not o.had_discount]
    if worst_full and r.baseline_discount_rate > 0:
        worst = max(worst_full, key=lambda o: o.paid)
        potential = round(worst.paid * r.baseline_discount_rate, 2)
        if potential >= 1:
            when = worst.created_at.strftime("%Y-%m-%d %H:%M") if worst.created_at else "时间未知"
            findings.append(
                AuditFinding(
                    level=LEVEL_TIP,
                    title=f"最大的一笔原价单：{money(worst.paid)}",
                    detail=(
                        f"{when} 在「{worst.store or '某家店'}」的这一单没用任何优惠，"
                        f"按你平时的省钱水平算，多花了约 {money(potential)}。"
                    ),
                    amount=potential,
                    action="金额大的单子更值得用券，省下来的也是最多的。",
                )
            )

    findings.sort(key=lambda f: (f.level != LEVEL_ALERT, f.level != LEVEL_NOTICE, -f.amount))
    return findings


def _build_points_findings(r: AuditResult) -> list[AuditFinding]:
    """积分层面的审计结论（与订单无关，可独立成立）。

    积分的特殊性在于它会被动失效：放着不用，就在无声地亏。
    因此「已经过期的积分」是实打实的已实现损失，
    「即将过期的积分」是待实现的损失 —— 两者都值得单独提示。
    """
    findings: list[AuditFinding] = []

    if r.account_expired > 0:
        findings.append(
            AuditFinding(
                level=LEVEL_ALERT,
                title=f"有 {r.account_expired:g} 分积分过期没了",
                detail=(
                    f"你有 {r.account_expired:g} 分积分已经过期作废了。"
                    "积分不会一直留着，放着不用就过期，过期了找不回来。"
                ),
                amount=0.0,
                action="趁积分还没到期，去麦麦商城换点东西，或者用来抽奖。",
            )
        )

    if r.account_at_risk > 0:
        findings.append(
            AuditFinding(
                level=LEVEL_NOTICE,
                title=f"{r.account_at_risk:g} 积分即将到期",
                detail=(
                    f"你有 {r.account_at_risk:g} 分积分最近一两个月就要到期了，"
                    f"账户里一共还有 {r.account_available:g} 分。"
                ),
                action="先把快到期的用掉，别又白白浪费一次。",
            )
        )
    elif r.account_available > 0 and r.account_expired <= 0:
        findings.append(
            AuditFinding(
                level=LEVEL_TIP,
                title=f"账户有 {r.account_available:g} 可用积分",
                detail=(
                    f"你现在有 {r.account_available:g} 分积分，暂时没有快到期的。"
                    "平时留意一下到期时间，别攒太多了一起过期。"
                ),
                amount=0.0,
                action="去麦麦商城看看积分能换什么，总比放着过期强。",
            )
        )
    return findings
