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
from .models import Coupon, Order

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
    account: object | None = None,
) -> AuditResult:
    """执行完整审计。

    account 可以是 mcp_client.AccountPoints（避免模块循环依赖，这里只做鸭子类型）。
    """
    result = AuditResult()
    if account is not None:
        result.account_available = float(getattr(account, "available", 0.0) or 0.0)
        result.account_at_risk = float(getattr(account, "at_risk", 0.0) or 0.0)
        result.account_expired = float(getattr(account, "expired", 0.0) or 0.0)

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
    result.findings = _build_findings(result, orders, metrics) + _build_points_findings(result)
    return result


def _build_findings(
    r: AuditResult, orders: list[Order], metrics: Metrics
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    # ① 原价单 → 最大的一条
    if r.full_price_orders > 0 and r.baseline_discount_rate > 0:
        rate_pct = r.baseline_discount_rate * 100
        findings.append(
            AuditFinding(
                level=LEVEL_ALERT,
                title=f"{r.full_price_orders} 单没用任何优惠",
                detail=(
                    f"这 {r.full_price_orders} 单合计实付 {money(r.full_price_paid)}，"
                    f"而你其余订单的平均折扣率是 {rate_pct:.1f}%。"
                    f"若按你自己的这个水平，这部分本可省约 {money(r.estimated_missed)}（估算）。"
                ),
                amount=r.estimated_missed,
                action="下次点单前先跑一次麦麦省一键领券，再让助手比价下单。",
            )
        )
    elif r.full_price_orders >= 2 and r.baseline_discount_rate == 0:
        findings.append(
            AuditFinding(
                level=LEVEL_ALERT,
                title="全部订单均为原价",
                detail=(
                    f"{r.full_price_orders} 单全部按原价结算，合计 {money(r.full_price_paid)}，"
                    "历史记录中查不到任何优惠使用痕迹。因为还没有可参照的折扣水平，"
                    "这里不估算金额 —— 但可领的券是现成的。"
                ),
                amount=0.0,
                action="麦麦省里有可领的券，点餐前先领券是最直接的一步。",
            )
        )

    # ② 可领未领的券
    if r.unbindable_coupons > 0:
        findings.append(
            AuditFinding(
                level=LEVEL_NOTICE,
                title=f"有 {r.unbindable_coupons} 张券可领但未领取",
                detail=(
                    f"麦麦省当前有 {r.unbindable_coupons} 张券处于可领取状态。"
                    "券躺在券池里不会自己跳到你的账户，需要在点餐前领取才会生效。"
                ),
                action="用 auto-bind-coupons 一键领取全部可用券。",
            )
        )

    # ③ 即将过期的券
    if r.expiring_coupons:
        names = "、".join(c.name for c in r.expiring_coupons[:3])
        more = f" 等 {len(r.expiring_coupons)} 张" if len(r.expiring_coupons) > 3 else ""
        findings.append(
            AuditFinding(
                level=LEVEL_NOTICE,
                title=f"{len(r.expiring_coupons)} 张券 7 天内到期",
                detail=f"「{names}」{more}即将失效，过期即作废、无法找回。",
                action="优先在近期点单中核销这些券。",
            )
        )

    # ④ 高频单品从未优惠
    if r.stale_top_items:
        detail = "、".join(f"{name}（{qty} 份）" for name, qty in r.stale_top_items)
        findings.append(
            AuditFinding(
                level=LEVEL_TIP,
                title="常点餐品从未走过优惠",
                detail=f"{detail} 是你的高频单品，但历史订单里它们从未出现在任何优惠单中。",
                action="查询门店券池，看是否有针对该单品的单品券或组合价。",
            )
        )

    # ⑤ 配送费沉淀
    if r.total_delivery_fee >= 20:
        findings.append(
            AuditFinding(
                level=LEVEL_TIP,
                title=f"配送费累计 {money(r.total_delivery_fee)}",
                detail=(
                    f"外送订单累计支付配送费 {money(r.total_delivery_fee)}。"
                    "到店取餐与得来速不计配送费，部分满额订单也可减免。"
                ),
                action="非必要外送时，比较一下到店取餐的实际差价。",
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
                    title=f"最大一笔原价单：{money(worst.paid)}",
                    detail=(
                        f"{when} 在「{worst.store or '未知门店'}」的这笔订单未使用任何优惠，"
                        f"按你的平均折扣水平估算多花约 {money(potential)}。"
                    ),
                    amount=potential,
                    action="大额订单优先考虑用券，优惠绝对值也最大。",
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
                title=f"已有 {r.account_expired:g} 积分过期作废",
                detail=(
                    f"账户历史上有 {r.account_expired:g} 积分已经过期失效。"
                    "积分属于会自然损耗的资产，放着不用就是净损失，且过期后无法追回。"
                ),
                amount=0.0,
                action="在积分到期前到麦麦商城兑换，或用于积分抽奖。",
            )
        )

    if r.account_at_risk > 0:
        findings.append(
            AuditFinding(
                level=LEVEL_NOTICE,
                title=f"{r.account_at_risk:g} 积分即将到期",
                detail=(
                    f"当前有 {r.account_at_risk:g} 积分将在最近一两个月内到期，"
                    f"账户可用积分共 {r.account_available:g}。"
                ),
                action="优先核销即将到期的部分，避免重复上次的过期损失。",
            )
        )
    elif r.account_available > 0 and r.account_expired <= 0:
        findings.append(
            AuditFinding(
                level=LEVEL_TIP,
                title=f"账户有 {r.account_available:g} 可用积分",
                detail=(
                    f"当前可用积分 {r.account_available:g}，暂无临期压力。"
                    "建议定期关注到期时间，避免积累过多后集中失效。"
                ),
                amount=0.0,
                action="到麦麦商城看看积分能换什么，通常存在比放任过期更好的选择。",
            )
        )
    return findings
