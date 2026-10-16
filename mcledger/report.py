"""年报生成器 —— 自包含 HTML + 内联 SVG，零外部资源。

    产出物是一张「可以直接双击打开、也可以直接截图分享」的麦门账本。
    没有 CDN、没有外部字体、没有 JS 依赖 —— 因为分享卡片一旦依赖网络，
    十有八九会在别人手机上碎掉。

    同时提供一份 Markdown 摘要，方便 Agent 在对话里直接念给用户听。
"""

from __future__ import annotations

import html
import json
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
from .audit import LEVEL_ALERT, LEVEL_NOTICE, LEVEL_TIP, AuditResult
from .personas import Persona

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


def _period_text(metrics: Metrics) -> str:
    if not metrics.has_time_data or metrics.first_order is None:
        return "时间范围未知"
    first = metrics.first_order.strftime("%Y-%m-%d")
    last = metrics.last_order.strftime("%Y-%m-%d") if metrics.last_order else first
    return f"{first} ~ {last}" if first != last else first


def render_html(
    metrics: Metrics,
    personas: list[Persona],
    audit: AuditResult,
    *,
    headline: str = "",
    source: str = "麦当劳 MCP",
    warnings: list[str] | None = None,
    generated_at: datetime | None = None,
) -> str:
    """生成完整年报 HTML。"""
    generated_at = generated_at or datetime.now()
    warnings = warnings or []

    if metrics.total_orders == 0:
        body = '<div class="card"><div class="empty">还没有拉到订单数据。<br>请检查 MCP Token 或改用 --offline 查看示例。</div></div>'
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
            '<div class="charttitle">星期分布</div>' + weekday_series_svg(metrics.weekday)
        )
    else:
        time_insight = "订单数据未携带下单时间，时段洞察不可用。"
        weekday_html = ""

    missed_html = ""
    if audit.estimated_missed >= 0.5:
        missed_html = _stat(
            "估算可省回",
            money(audit.estimated_missed),
            "按你自己的平均折扣率估算",
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
  <h2>核心数字</h2>
  <div class="grid g4">
    {_stat("订单总数", f"{metrics.total_orders} 单", f"覆盖 {metrics.active_days} 个下单日，首单至今 {metrics.span_days} 天")}
    {_stat("累计实付", money(metrics.total_paid), f"原价合计 {money(metrics.total_gross)}")}
    {_stat("累计省下", money(metrics.total_saved), f"整体折扣率 {metrics.save_rate * 100:.1f}%", "gold")}
    {_stat("平均客单价", money(metrics.avg_order_value), f"平均每单 {metrics.avg_items_per_order:.1f} 份餐品")}
    {missed_html}
  </div>
</div>

<div class="card">
  <h2>你的麦门人格</h2>
  {persona_html}
</div>

<div class="card">
  <h2>消费节奏</h2>
  <div class="fd" style="font-size:13.5px;color:#4A4A4A">{time_insight}</div>
  <div class="charttitle">月度消费趋势</div>
  {monthly_series_svg(metrics.monthly)}
  <div class="charttitle">24 小时下单分布</div>
  {hourly_series_svg(metrics.hourly)}
  {weekday_html}
</div>

<div class="card">
  <h2>常点榜单</h2>
  {_item_ranking(metrics)}
</div>

<div class="card">
  <h2>账单审计</h2>
  <div class="graderow">
    <div class="grade">{_esc(audit.grade)}</div>
    <div class="gt">
      <b>优惠利用率 {audit.coupon_utilization * 100:.0f}%</b>
      {metrics.discounted_orders}/{metrics.total_orders} 单享受到了优惠
    </div>
  </div>
  {finding_html}
</div>

<footer>
  本报告由「麦门账本」基于麦当劳 MCP 返回的数据在本地计算生成，全部统计过程可复现。<br>
  数据来源：麦当劳 MCP 服务（order-list / query-my-coupons 等）。<br>
  本项目为麦当劳程序员创意开发大赛参赛作品，非麦当劳官方产品。<br>
  餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准；「估算可省回」为基于你自身历史折扣水平的推算，仅供参考。
</footer>
"""
    if warnings:
        body += (
            '<div class="warn">取数提示：'
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
) -> str:
    """给 Agent / 终端用的 Markdown 摘要。"""
    if metrics.total_orders == 0:
        return "## 麦门账本\n\n暂无订单数据。"

    lines: list[str] = ["## 麦门账本 · 消费简报", ""]
    if headline:
        lines += [f"> {headline}", ""]

    lines += [
        "### 核心数字",
        f"- 订单总数：**{metrics.total_orders} 单**（覆盖 {metrics.active_days} 天）",
        f"- 累计实付：**{money(metrics.total_paid)}**（原价 {money(metrics.total_gross)}）",
        f"- 累计省下：**{money(metrics.total_saved)}**，整体折扣率 {metrics.save_rate * 100:.1f}%",
        f"- 平均客单价：**{money(metrics.avg_order_value)}**",
        "",
    ]

    if personas:
        lines += ["### 麦门人格", ""]
        for persona in personas:
            lines.append(f"- {persona.emoji} **{persona.title}** — {persona.tagline}")
            lines.append(f"  - 依据：{persona.evidence}")
        lines.append("")

    if metrics.top_items:
        lines += ["### 常点榜单", ""]
        for index, item in enumerate(metrics.top_items[:5], start=1):
            lines.append(f"{index}. {item.name} —— {item.qty} 份 / {money(item.spend)}")
        lines.append("")

    lines += [f"### 账单审计（优惠利用率 {audit.coupon_utilization * 100:.0f}%，等级 {audit.grade}）", ""]
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
    """写出 HTML，自动创建父目录。"""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_text, encoding="utf-8")
    return path


def write_json_snapshot(payload: object, output: str | Path) -> Path:
    """把中间结果落盘为 JSON，便于复现与写进 MCP_INTEGRATION.md。"""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
