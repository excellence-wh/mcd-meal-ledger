"""年报生成器 —— 自包含 HTML + 内联 SVG，零外部资源。

    产出物是一张「可以直接双击打开、也可以直接截图分享」的麦门账本。
    没有 CDN、没有外部字体、没有 JS 依赖 —— 因为分享卡片一旦依赖网络，
    十有八九会在别人手机上碎掉。

    同时提供一份 Markdown 摘要，方便 Agent 在对话里直接念给用户听。
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .analytics import (
    WEEKDAY_NAMES,
    Metrics,
    hourly_series_svg,
    money,
    monthly_series_svg,
    weekday_series_svg,
)
from .audit import AuditResult
from .coupons import CouponPortfolio
from .habits import HabitReport
from .models import AccountPoints
from .nutrition import DISCLAIMER, FitResult, NutritionProfile
from .personas import Persona
from .saver import TopUpAdvice

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",
    "Microsoft YaHei","Segoe UI",Roboto,sans-serif;
  background:#FFF6E5;color:#2B2B2B;line-height:1.6;
  -webkit-font-smoothing:antialiased;padding:32px 16px;
}
.wrap{max-width:760px;margin:0 auto}
.card{background:#fff;border-radius:20px;padding:28px;
  box-shadow:0 6px 24px rgba(150,110,40,.10);margin-bottom:18px}
.hero{
  background:linear-gradient(135deg,#DA291C 0%,#B71C12 55%,#7A1009 100%);
  color:#fff;border-radius:24px;padding:34px 30px;margin-bottom:18px;
  position:relative;overflow:hidden
}
.hero::after{
  content:"";position:absolute;right:-60px;top:-60px;width:220px;height:220px;
  border-radius:50%;background:rgba(255,199,44,.22)
}
.brand{font-size:13px;letter-spacing:3px;opacity:.85;text-transform:uppercase}
.hero h1{font-size:32px;font-weight:800;margin:8px 0 10px;letter-spacing:-.5px}
.hero .sub{font-size:15px;opacity:.92}
.hero .headline{
  margin-top:18px;padding:14px 16px;border-radius:14px;
  background:rgba(255,255,255,.15);font-size:15px;font-weight:600;
  backdrop-filter:blur(4px)
}
h2{font-size:17px;font-weight:800;margin-bottom:16px;
  display:flex;align-items:center;gap:9px;color:#1F1F1F}
h2::before{content:"";width:5px;height:19px;border-radius:3px;
  background:linear-gradient(180deg,#FFC72C,#DA291C);display:inline-block}
.grid{display:grid;gap:12px}
.g4{grid-template-columns:repeat(auto-fit,minmax(148px,1fr))}
.stat{background:#FFFBF2;border:1px solid #F2E4C9;border-radius:14px;padding:16px}
.stat .k{font-size:12px;color:#9A8B72;font-weight:600;letter-spacing:.4px}
.stat .v{font-size:25px;font-weight:800;margin-top:6px;color:#1F1F1F;
  font-variant-numeric:tabular-nums}
.stat .v.accent{color:#DA291C}
.stat .v.gold{color:#C77E00}
.stat .n{font-size:11.5px;color:#A2947C;margin-top:3px}
.persona{display:flex;gap:14px;padding:15px;border-radius:14px;
  background:#FFFBF2;border:1px solid #F2E4C9;margin-bottom:11px}
.persona .emo{font-size:29px;line-height:1.1}
.persona .t{font-weight:800;font-size:15.5px}
.persona .tag{font-size:13px;color:#7A6C55;margin:2px 0 7px}
.persona .ev{font-size:12.5px;color:#4A4A4A;background:#fff;
  border-left:3px solid #FFC72C;border-radius:0 8px 8px 0;padding:7px 11px}
.rank{display:flex;align-items:center;gap:11px;padding:9px 0;
  border-bottom:1px dashed #F0E4CC}
.rank:last-child{border-bottom:none}
.rank .i{width:23px;height:23px;border-radius:7px;background:#FFC72C;color:#5A3A00;
  font-size:12px;font-weight:800;display:flex;align-items:center;
  justify-content:center;flex:none}
.rank .n{flex:1;font-size:14px;font-weight:600}
.rank .q{font-size:12.5px;color:#9A8B72;font-variant-numeric:tabular-nums}
.bar{height:6px;border-radius:3px;background:#FFE9A8;overflow:hidden;
  margin-top:5px;max-width:190px}
.bar span{display:block;height:100%;border-radius:3px;
  background:linear-gradient(90deg,#FFC72C,#DA291C)}
.finding{border-radius:14px;padding:15px;margin-bottom:11px;border:1px solid}
.finding.alert{background:#FFF3F1;border-color:#F8CFC7}
.finding.notice{background:#FFFBF0;border-color:#F5E3B8}
.finding.tip{background:#F5FAF2;border-color:#D8EBCE}
.finding .ft{font-weight:800;font-size:14.5px;display:flex;
  justify-content:space-between;gap:10px;align-items:baseline}
.finding .fd{font-size:13px;color:#4A4A4A;margin-top:7px}
.finding .fa{font-size:12.5px;color:#7A6C55;margin-top:8px;
  padding-left:11px;border-left:2px solid #D9C9A8}
.finding .amt{font-size:13px;font-weight:800;color:#DA291C;white-space:nowrap}
.grade{display:inline-flex;align-items:center;justify-content:center;
  width:46px;height:46px;border-radius:14px;background:#DA291C;color:#fff;
  font-size:23px;font-weight:900;margin-right:12px}
.graderow{display:flex;align-items:center;margin-bottom:16px}
.graderow .gt{font-size:13.5px;color:#7A6C55}
.graderow .gt b{display:block;font-size:15px;color:#1F1F1F;font-weight:800}
.charttitle{font-size:12.5px;font-weight:700;color:#8A7A63;
  margin:16px 0 5px;letter-spacing:.3px}
footer{font-size:12px;color:#A2947C;text-align:center;padding:20px 10px 8px;
  line-height:1.8}
.warn{font-size:12.5px;color:#9A6B00;background:#FFF8E3;border:1px solid #F2E0AF;
  border-radius:10px;padding:10px 13px;margin-top:14px}
.empty{text-align:center;color:#A2947C;padding:36px 10px;font-size:14px}
@media print{body{background:#fff;padding:0}.card{box-shadow:none}}
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _stat(label: str, value: str, note: str = "", accent: str = "") -> str:
    cls = f"v {accent}".strip()
    note_html = f'<div class="n">{_esc(note)}</div>' if note else ""
    return (
        f'<div class="stat"><div class="k">{_esc(label)}</div>'
        f'<div class="{cls}">{_esc(value)}</div>{note_html}</div>'
    )


def _persona_block(persona: Persona) -> str:
    return (
        '<div class="persona">'
        f'<div class="emo">{persona.emoji}</div><div>'
        f'<div class="t">{_esc(persona.title)}</div>'
        f'<div class="tag">{_esc(persona.tagline)}</div>'
        f'<div class="ev">{_esc(persona.evidence)}</div>'
        "</div></div>"
    )


def _finding_block(finding) -> str:
    amount = (
        f'<span class="amt">≈ {money(finding.amount)}</span>' if finding.amount >= 0.5 else ""
    )
    action = (
        f'<div class="fa">建议：{_esc(finding.action)}</div>' if finding.action else ""
    )
    return (
        f'<div class="finding {finding.level}">'
        f'<div class="ft"><span>{_esc(finding.title)}</span>{amount}</div>'
        f'<div class="fd">{_esc(finding.detail)}</div>{action}</div>'
    )


def _item_ranking(metrics: Metrics, limit: int = 6) -> str:
    if not metrics.top_items:
        return '<div class="empty">暂无餐品数据</div>'
    top_qty = metrics.top_items[0].qty or 1
    rows: list[str] = []
    for index, item in enumerate(metrics.top_items[:limit], start=1):
        pct = item.qty / top_qty * 100
        rows.append(
            f'<div class="rank"><div class="i">{index}</div>'
            f'<div style="flex:1"><div class="n">{_esc(item.name)}</div>'
            f'<div class="bar"><span style="width:{pct:.0f}%"></span></div></div>'
            f'<div class="q">{item.qty} 份 · {money(item.spend)}</div></div>'
        )
    return "".join(rows)


def _component_ranking(metrics: Metrics, limit: int = 6) -> str:
    """套餐拆解后的单品榜（数量口径，不含金额 —— 子项没有独立价格）。"""
    if not metrics.top_components:
        return ""
    top_qty = metrics.top_components[0].qty or 1
    rows: list[str] = []
    for index, item in enumerate(metrics.top_components[:limit], start=1):
        pct = item.qty / top_qty * 100
        rows.append(
            f'<div class="rank"><div class="i" style="background:#FFE9A8">{index}</div>'
            f'<div style="flex:1"><div class="n">{_esc(item.name)}</div>'
            f'<div class="bar"><span style="width:{pct:.0f}%"></span></div></div>'
            f'<div class="q">{item.qty} 份</div></div>'
        )
    return (
        '<div class="charttitle" style="margin-top:20px">把套餐拆开，你实际吃到的是</div>'
        '<div style="font-size:12.5px;color:#9A8B72;margin-bottom:8px">'
        f"共 {metrics.combo_orders} 份套餐被拆开统计，这样才看得清你实际在吃什么</div>" + "".join(rows)
    )


def _promotions_card(titles: list[str] | None) -> str:
    """当前进行中的活动（来自 campaign-calendar）。"""
    if not titles:
        return ""
    chips = "".join(
        f'<span style="display:inline-block;background:#FFF3D6;border:1px solid #F2E0AF;'
        f'border-radius:999px;padding:5px 13px;margin:0 8px 8px 0;font-size:12.5px;'
        f'color:#7A5A00">{_esc(t)}</span>'
        for t in titles
    )
    return (
        '<div class="card"><h2>最近有哪些活动</h2>'
        '<div style="font-size:12.5px;color:#9A8B72;margin-bottom:10px">'
        "来自麦麦日历，说明当下有哪些活动可以和你的券搭配着用</div>"
        f"{chips}</div>"
    )


def _points_card(account: AccountPoints | None) -> str:
    """积分板块。积分是会自然损耗的资产，值得单独一块。"""
    if account is None:
        return ""
    available = account.available
    at_risk = account.at_risk
    expired = account.expired
    accumulative = account.accumulative
    currency = account.currency or "积分"
    if not any((available, at_risk, expired, accumulative)):
        return ""

    stats = [
        _stat("现在还有", f"{available:g} 分", f"{currency}，一共攒过 {accumulative:g} 分"),
        _stat(
            "快过期了",
            f"{at_risk:g} 分",
            "再不花就没了" if at_risk > 0 else "暂时没有快到期的",
            "accent" if at_risk > 0 else "",
        ),
        _stat(
            "已经浪费掉",
            f"{expired:g} 分",
            "过期就找不回来了" if expired > 0 else "一分都没浪费",
            "accent" if expired > 0 else "",
        ),
    ]
    # 这里不再重复「积分过期」的提醒 —— 下面的账单板块已经专门讲了，
    # 同一件事说两遍只会让报告显得啰嗦。
    return f'<div class="card"><h2>你的积分</h2><div class="grid g4">{"".join(stats)}</div></div>'


def _period_text(metrics: Metrics) -> str:
    if not metrics.has_time_data or metrics.first_order is None:
        return "时间范围未知"
    first = metrics.first_order.strftime("%Y-%m-%d")
    last = metrics.last_order.strftime("%Y-%m-%d") if metrics.last_order else first
    return f"{first} ~ {last}" if first != last else first


@dataclass
class ReportExtras:
    """报告的可选加强板块。

    用一个小容器统一传，而不是往 render_html 上挂七八个参数 ——
    那样每加一个能力就要改一次签名，调用方也得跟着改。
    """

    habits: HabitReport | None = None
    portfolio: CouponPortfolio | None = None
    topups: list[TopUpAdvice] = field(default_factory=list)
    nutrition: NutritionProfile | None = None
    meal_fit: FitResult | None = None
    menu_source: str = ""


_MENU_SOURCE_LABELS: dict[str, str] = {
    "mcp": "这家店的实时菜单",
    "history": "从你自己的历史订单里推出的价格（是过去的价，仅供参考）",
    "fixture": "内置演示数据",
}


def _habits_card(habits: HabitReport | None) -> str:
    """习惯年账 —— 本项目唯一别人做不了的板块。"""
    if habits is None or not habits.has_data:
        return ""

    blocks: list[str] = []
    for index, cost in enumerate(habits.costs, start=1):
        badge = ""
        if cost.has_saving:
            badge = f'<span class="amt">≈ {money(cost.annual_savable)}/年</span>'
        blocks.append(
            '<div class="finding tip"><div class="ft">'
            f"<span>第 {index} 个习惯</span>{badge}</div>"
            f'<div class="fd">{_esc(cost.explain())}</div></div>'
        )

    notes = "".join(f'<div class="n" style="font-size:12px">{_esc(n)}</div>' for n in habits.notes())
    return (
        '<div class="card"><h2>照你这样点，一年大概花多少</h2>'
        '<div class="fd" style="font-size:13.5px;color:#4A4A4A;margin-bottom:12px">'
        f"{_esc(habits.headline())}</div>"
        f"{''.join(blocks)}"
        '<div style="margin-top:14px;padding-top:12px;border-top:1px dashed #F0E4CC">'
        f"{notes}</div></div>"
    )


def _coupon_honesty_card(portfolio: CouponPortfolio | None, topups: list[TopUpAdvice]) -> str:
    """把券的底细交代清楚：哪些算得出来、哪些算不出来、为什么。"""
    if portfolio is None or not portfolio.rules:
        return ""

    rows: list[str] = []
    for index, rule in enumerate(portfolio.rules, start=1):
        rows.append(
            f'<div class="rank"><div class="i">{index}</div>'
            '<div style="flex:1">'
            f'<div class="n">{_esc(rule.raw)}</div>'
            f'<div class="q" style="text-align:left">{_esc(rule.describe())}</div>'
            "</div></div>"
        )

    uncomputable = [r for r in portfolio.rules if not r.is_computable]
    tail = ""
    if uncomputable:
        tail = (
            f'<div class="warn">其中 {len(uncomputable)} 张券<b>算不出确定金额</b>，'
            "本次没有计入优惠 —— 它们的优惠额取决于具体商品或购买件数，"
            "而接口只返回券名。宁可少算，也不给你一个到店付不出来的价。</div>"
        )

    topup_html = ""
    if topups:
        tip_blocks: list[str] = []
        for advice in topups[:3]:
            level = "tip" if advice.worth_it else "notice"
            badge = '<span class="amt">值得加</span>' if advice.worth_it else ""
            tip_blocks.append(
                f'<div class="finding {level}"><div class="ft">'
                f"<span>{_esc(advice.coupon)}</span>{badge}</div>"
                f'<div class="fd">{_esc(advice.explain())}</div></div>'
            )
        topup_html = (
            '<div class="charttitle" style="margin-top:16px">凑单值不值</div>'
            + "".join(tip_blocks)
        )

    return (
        '<div class="card"><h2>你手上的券，能算清楚的有几张</h2>'
        '<div class="fd" style="font-size:13.5px;color:#4A4A4A;margin-bottom:10px">'
        f"一共 {len(portfolio.rules)} 张，其中 {len(portfolio.computable)} 张能算出确定金额。</div>"
        f"{''.join(rows)}{tail}{topup_html}</div>"
    )


def _nutrition_card(
    profile: NutritionProfile | None, fit: FitResult | None
) -> str:
    """营养画像：只讲事实，不给健康结论。"""
    if profile is None or not profile.total.known_items:
        return ""
    lines = "".join(f'<div class="n" style="font-size:12.5px">{_esc(t)}</div>' for t in profile.describe())

    cats = ""
    if profile.by_category:
        total_kcal = sum(v for _, v in profile.by_category) or 1.0
        cats = "".join(
            f'<div class="rank"><div class="n" style="flex:1">{_esc(name)}</div>'
            f'<div class="q">{kcal:g} 大卡 · {kcal / total_kcal * 100:.0f}%</div></div>'
            for name, kcal in profile.by_category[:5]
        )
        cats = f'<div class="charttitle" style="margin-top:14px">热量从哪来</div>{cats}'

    fit_html = ""
    if fit is not None and fit.has_options:
        rows = "".join(
            f'<div class="rank"><div class="i">{i}</div><div style="flex:1">'
            f'<div class="n">{_esc(o.label)}</div>'
            f'<div class="q" style="text-align:left">{_esc(o.total.describe())}</div></div>'
            f'<div class="q">{money(o.price)}</div></div>'
            for i, o in enumerate(fit.options[:5], start=1)
        )
        fit_html = (
            '<div class="charttitle" style="margin-top:16px">按营养限制，最省的几套</div>'
            f"{rows}"
        )

    return (
        '<div class="card"><h2>你实际吃成什么样</h2>'
        f"{lines}{cats}{fit_html}"
        f'<div class="warn">{_esc(DISCLAIMER)}</div></div>'
    )


def _render_extras(extras: ReportExtras | None) -> str:
    if extras is None:
        return ""
    return (
        _habits_card(extras.habits)
        + _coupon_honesty_card(extras.portfolio, extras.topups)
        + _nutrition_card(extras.nutrition, extras.meal_fit)
    )


def render_html(
    metrics: Metrics,
    personas: list[Persona],
    audit: AuditResult,
    *,
    headline: str = "",
    source: str = "你的麦当劳账户",
    warnings: list[str] | None = None,
    generated_at: datetime | None = None,
    account: AccountPoints | None = None,
    promotions: list[str] | None = None,
    extras: ReportExtras | None = None,
) -> str:
    """生成完整年报 HTML。"""
    generated_at = generated_at or datetime.now()
    warnings = warnings or []

    if metrics.total_orders == 0:
        # 订单为空时，积分层面的审计结论依然成立，不能一并丢掉
        points = _points_card(account)
        finding_html = "".join(_finding_block(f) for f in audit.findings)
        body = (
            '<div class="hero"><div class="brand">McDonald\'s Meal Ledger</div>'
            '<h1>麦门账本</h1><div class="sub">暂无可统计的订单数据</div>'
            '<div class="headline">还没有拉到订单记录 —— 但账户层面的审计照样能跑</div></div>'
        )
        if points:
            body += points
        if finding_html:
            body += f'<div class="card"><h2>你的账单，钱漏在哪</h2>{finding_html}</div>'
        body += (
            '<div class="card"><div class="empty">'
            "这次没读到你的历史订单，所以只给出了账户层面的检查结果。<br>"
            "常见原因：这个账号还没有麦当劳点单记录，"
            "或者登录的账号和你平时点单用的不是同一个。"
            "</div></div>"
        )
        return _wrap(body, generated_at, source)

    persona_html = "".join(_persona_block(p) for p in personas) or (
        '<div class="empty">订单样本偏少，暂未匹配到鲜明的人格标签</div>'
    )
    finding_html = "".join(_finding_block(f) for f in audit.findings) or (
        '<div class="empty">没有发现明显的优惠漏洞，账户状态健康</div>'
    )

    # 时段洞察文案
    if metrics.has_time_data:
        time_insight = (
            f"你的高频时段是 <b>{metrics.peak_hour}:00</b> 前后，"
            f"最爱在 <b>{WEEKDAY_NAMES[metrics.peak_weekday]}</b> 下单，"
            f"整体集中在 <b>{metrics.peak_daypart}</b>。"
        )
        weekday_html = (
            '<div class="charttitle">星期几点得多</div>' + weekday_series_svg(metrics.weekday)
        )
    else:
        time_insight = "订单数据未携带下单时间，时段洞察不可用。"
        weekday_html = ""

    missed_html = ""
    if audit.estimated_missed >= 0.5:
        missed_html = _stat(
            "本来还能再省",
            money(audit.estimated_missed),
            "按你平时的省钱水平估算",
            "accent",
        )

    body = f"""
<div class="hero">
  <div class="brand">McDonald's Meal Ledger</div>
  <h1>麦门账本</h1>
  <div class="sub">{_esc(_period_text(metrics))} · 数据来自{_esc(source)}</div>
  <div class="headline">{_esc(headline)}</div>
</div>

<div class="card">
  <h2>先看几个数</h2>
  <div class="grid g4">
    {_stat(
        "一共点了几单",
        f"{metrics.total_orders} 单",
        f"有 {metrics.active_days} 天点过，最早一单距今 {metrics.span_days} 天",
    )}
    {_stat("一共花了", money(metrics.total_paid), f"按原价算要 {money(metrics.total_gross)}")}
    {_stat(
        "省下了",
        money(metrics.total_saved),
        f"相当于每花 100 元省 {metrics.save_rate * 100:.0f} 元",
        "gold",
    )}
    {_stat(
        "平均每单",
        money(metrics.avg_order_value),
        f"平均每单点 {metrics.avg_items_per_order:.1f} 样",
    )}
    {missed_html}
  </div>
</div>

<div class="card">
  <h2>你是哪一种麦门食客</h2>
  {persona_html}
</div>

<div class="card">
  <h2>你什么时候点麦当劳</h2>
  <div class="fd" style="font-size:13.5px;color:#4A4A4A">{time_insight}</div>
  <div class="charttitle">每个月花了多少</div>
  {monthly_series_svg(metrics.monthly)}
  <div class="charttitle">一天里什么时候点</div>
  {hourly_series_svg(metrics.hourly)}
  {weekday_html}
</div>

<div class="card">
  <h2>你点得最多的是</h2>
  {_item_ranking(metrics)}
  {_component_ranking(metrics)}
</div>

{_points_card(account)}

{_promotions_card(promotions)}

{_render_extras(extras)}

<div class="card">
  <h2>你的账单，钱漏在哪</h2>
  <div class="graderow">
    <div class="grade">{_esc(audit.grade)}</div>
    <div class="gt">
      <b>有 {audit.coupon_utilization * 100:.0f}% 的订单用上了优惠</b>
      {metrics.discounted_orders} 单用上了，{metrics.free_orders} 单是原价买的
    </div>
  </div>
  {finding_html}
</div>

<footer>
  这份报告读的是你自己的麦当劳消费记录，所有统计都在你自己电脑上完成，不会外传。<br>
  本项目为麦当劳程序员创意开发大赛参赛作品，非麦当劳官方产品。<br>
  餐品信息和价格请以麦当劳门店的实时情况为准。<br>
  「估算可省回」是按你自己平时享受到的折扣水平倒推的，只是个大概，不等于一定能省到这么多。
</footer>
"""
    if warnings:
        body += (
            '<div class="warn">说明：'
            + _esc("；".join(warnings[:4]))
            + "</div>"
        )
    return _wrap(body, generated_at, source)


def _wrap(body: str, generated_at: datetime, source: str) -> str:
    stamp = generated_at.strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="generator" content="mcd-meal-ledger 1.0.0">
<title>麦门账本 · 我的麦当劳消费年报</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
{body}
<div style="text-align:center;font-size:11.5px;color:#B5A88F;padding-bottom:24px">
生成时间 {stamp} · 数据来源 {source}
</div>
</div>
</body>
</html>
"""


def render_markdown_summary(
    metrics: Metrics,
    personas: list[Persona],
    audit: AuditResult,
    *,
    headline: str = "",
    account: AccountPoints | None = None,
    extras: ReportExtras | None = None,
) -> str:
    """给 Agent / 终端用的 Markdown 摘要。"""
    if metrics.total_orders == 0:
        empty: list[str] = ["## 麦门账本", "", "暂无订单数据。"]
        if account is not None and account.expired:
            empty.append(
                f"- ⚠️ 账户已有 **{account.expired:g} 积分过期作废**（已实现损失）"
            )
        if audit.findings:
            empty += ["", "### 你的账单，钱漏在哪", ""]
            empty += [f"- **{f.title}** — {f.detail}" for f in audit.findings]
        return "\n".join(empty)

    lines: list[str] = ["## 麦门账本 · 消费简报", ""]
    if headline:
        lines += [f"> {headline}", ""]

    lines += [
        "### 先看几个数",
        f"- 订单总数：**{metrics.total_orders} 单**（覆盖 {metrics.active_days} 个下单日）",
        f"- 累计实付：**{money(metrics.total_paid)}**（原价 {money(metrics.total_gross)}）",
        f"- 累计省下：**{money(metrics.total_saved)}**，整体折扣率 {metrics.save_rate * 100:.1f}%",
        f"- 平均客单价：**{money(metrics.avg_order_value)}**",
        "",
    ]

    if personas:
        lines += ["### 你是哪一种麦门食客", ""]
        for persona in personas:
            lines.append(f"- {persona.emoji} **{persona.title}** — {persona.tagline}")
            lines.append(f"  - 依据：{persona.evidence}")
        lines.append("")

    if metrics.top_items:
        lines += ["### 你点得最多的是", ""]
        for index, item in enumerate(metrics.top_items[:5], start=1):
            lines.append(f"{index}. {item.name} —— {item.qty} 份 / {money(item.spend)}")
        lines.append("")

    if metrics.top_components:
        lines += ["### 把套餐拆开，你实际吃到的是", ""]
        for index, item in enumerate(metrics.top_components[:5], start=1):
            lines.append(f"{index}. {item.name} —— {item.qty} 份")
        lines.append("")

    if account is not None and (account.available or account.expired):
        lines += [
            "### 你的积分",
            f"- 可用积分：**{account.available:g}**",
            f"- 即将过期：{account.at_risk:g}",
            f"- 已过期作废：**{account.expired:g}**",
            "",
        ]

    if extras is not None and extras.habits is not None and extras.habits.has_data:
        lines += ["", "### 照你这样点，一年大概花多少", ""]
        lines.append(f"> {extras.habits.headline()}")
        for cost in extras.habits.costs:
            lines.append(f"- {cost.explain()}")
        for note in extras.habits.notes():
            lines.append(f"  - {note}")
        lines.append("")

    if extras is not None and extras.nutrition is not None and extras.nutrition.total.known_items:
        lines += ["### 你实际吃成什么样", ""]
        lines += [f"- {t}" for t in extras.nutrition.describe()]
        lines.append(f"- {DISCLAIMER}")
        lines.append("")

    lines += [
        f"### 你的账单，钱漏在哪"
        f"（{audit.coupon_utilization * 100:.0f}% 的订单用上了优惠，评级 {audit.grade}）",
        "",
    ]
    if audit.findings:
        for finding in audit.findings:
            amount = f"（估算 ≈ {money(finding.amount)}）" if finding.amount >= 0.5 else ""
            lines.append(f"- **{finding.title}**{amount}")
            lines.append(f"  - {finding.detail}")
            if finding.action:
                lines.append(f"  - 建议：{finding.action}")
    else:
        lines.append("- 未发现明显的优惠漏洞。")
    lines.append("")
    return "\n".join(lines)


def write_report(html_text: str, output: str | Path) -> Path:
    """写出 HTML，自动创建父目录。

    显式指定 newline="\\n"：Windows 的文本模式默认会把 \\n 转成 \\r\\n，
    那样同一份输入在 Windows 与 Linux 上产出的文件字节数不同 ——
    对一个以「确定性、可复现」为核心承诺的工具来说，这是不能接受的。
    """
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(html_text)
    return path


def write_json_snapshot(payload: object, output: str | Path) -> Path:
    """把中间结果落盘为 JSON，便于复现与写进 MCP_INTEGRATION.md。"""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
        fh.write("\n")
    return path
