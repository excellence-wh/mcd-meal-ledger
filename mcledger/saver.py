"""省钱助手 —— 算清楚这一单怎么点最省，以及"为了省钱而多买"到底划不划算。

    这个模块只做两件事，但两件都要求算得准：

    1. **比价**：同一批想吃的，不用券 / 用券 / 走组合价，各要多少钱。
    2. **凑单判断**：为了够上满减门槛而加购，到底是赚还是亏。

    第 2 件事是重点。它看起来简单，但**大模型经常算错**，
    而且是往"划算"的方向错 —— 因为它只看到"减 7 元"，
    看不到"为了减 7 元你先多花了 10 元"。

    设计底线：

    - 只对能从券名推出确定金额的券出方案（满减 / 整单折扣 / 单品直降）；
      其余类型的优惠额取决于具体商品，一律不参与计算，也不假装知道。
    - **默认不叠加券**。叠加规则接口不返回，宁可少报优惠，
      也不给用户一个到店付不出来的价格。
    - 拿不到菜单时不给凑单建议，并说明原因，而不是编一个商品出来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analytics import money
from .coupons import KIND_THRESHOLD, CouponPortfolio
from .models import to_float, unwrap

# --------------------------------------------------------------------------
# 菜单
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MenuItem:
    """菜单上的一件商品。

    门店实时菜单除了现价，还会给一个「标称原价」。两者之差就是官方
    自己承认的折扣幅度 —— 这是判断「我这一单到底买贵没有」的基准，
    所以单独留一个字段，不要和现价混在一起。
    """

    name: str
    price: float
    code: str | None = None
    original_price: float | None = None  # 菜单标称原价；无或不高于现价时为 None
    category: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", round(float(self.price), 2))
        if self.original_price is not None:
            original = round(float(self.original_price), 2)
            # 原价不高于现价时视为无效 —— 有些门店会把两者填成同一个数
            object.__setattr__(self, "original_price", original if original > self.price else None)

    @property
    def menu_discount(self) -> float:
        """官方标称的折扣金额（现价与原价之差）。"""
        if self.original_price is None:
            return 0.0
        return round(self.original_price - self.price, 2)


@dataclass
class Menu:
    """一份可用菜单。

    菜单可能来自三个地方，可信度不同，所以要记下来源：
      - ``mcp``    ：门店实时菜单（最准）
      - ``history``：从你自己的历史订单里提取的价格（是过去的价，仅供参考）
      - ``fixture``：内置演示数据
    """

    items: list[MenuItem] = field(default_factory=list)
    source: str = "unknown"

    def __bool__(self) -> bool:
        return bool(self.items)

    @property
    def is_live(self) -> bool:
        """是否是门店实时菜单。凑单建议依赖当前价格，非实时时会提醒用户。"""
        return self.source == "mcp"

    def cheapest_at_least(self, amount: float) -> MenuItem | None:
        """找出价格 >= amount 的最便宜商品 —— 凑单时最该加的那一件。"""
        candidates = [i for i in self.items if i.price >= amount]
        if not candidates:
            return None
        return min(candidates, key=lambda i: (i.price, i.name))

    def find(self, keyword: str) -> MenuItem | None:
        """按名字里的关键词找商品（取最便宜的匹配）。"""
        hits = [i for i in self.items if keyword and keyword in i.name]
        return min(hits, key=lambda i: (i.price, i.name)) if hits else None


def menu_from_mcp(payload: object) -> Menu:
    """把门店实时菜单解析成 Menu。

    真实接口是**两层结构**（2026-10-16 联调确认）：

    - ``data.categories[]`` —— 每个品类是 ``{name, meals: [{code, tags}]}``，
      这里只有编码和标签；
    - ``data.meals{code}`` —— 编码 → ``{name, currentPrice, originalPrice, image}``，
      名字和价格都在这一层。

    必须把第二层的价格 join 回第一层的品类顺序，才拿得到「名字 + 价格 + 品类」。
    只按 ``data.list`` 之类的键名取会**一行都取不到，而且不报错** ——
    菜单会静默变空，下游的比价与凑单全部退化成「没有数据」。
    这个 bug 在本项目里真实存在过，所以专门补了契约测试锁住结构。
    """
    node = unwrap(payload)
    if not isinstance(node, dict):
        return Menu(items=[], source="mcp")

    catalog = node.get("meals")
    if not isinstance(catalog, dict):
        catalog = {}

    items: list[MenuItem] = []
    seen: set[str] = set()

    def _add(code: str, category: str | None) -> bool:
        detail = catalog.get(code)
        if not isinstance(detail, dict):
            return False
        name = str(detail.get("name") or "").strip()
        if not name or code in seen:
            return False
        seen.add(code)
        original = to_float(detail.get("originalPrice"))
        items.append(
            MenuItem(
                name=name,
                price=to_float(detail.get("currentPrice")),
                code=code,
                original_price=original if original > 0 else None,
                category=category,
            )
        )
        return True

    for category in node.get("categories") or []:
        if not isinstance(category, dict):
            continue
        cat_name = re.sub(r"\s+", " ", str(category.get("name") or "")).strip() or None
        for entry in category.get("meals") or []:
            if isinstance(entry, dict):
                _add(str(entry.get("code") or "").strip(), cat_name)

    # 兜底：菜单里存在、但没有任何品类引用的餐品也要收进来，
    # 否则它们会凭空消失（用户会觉得「这家店怎么没有巨无霸」）。
    for code in catalog:
        _add(str(code), None)

    return Menu(items=items, source="mcp")


# --------------------------------------------------------------------------
# 想吃的
# --------------------------------------------------------------------------


@dataclass
class WishItem:
    """想点的一件餐品。"""

    name: str
    qty: int = 1
    unit_price: float = 0.0

    @property
    def subtotal(self) -> float:
        return round(self.unit_price * self.qty, 2)


@dataclass
class Wish:
    """一次点单的诉求。"""

    items: list[WishItem] = field(default_factory=list)
    budget: float | None = None  # 预算上限（可选）
    max_calories: float | None = None  # 热量上限（可选）

    @property
    def item_names(self) -> list[str]:
        return [i.name for i in self.items]

    @property
    def gross(self) -> float:
        return round(sum(i.subtotal for i in self.items), 2)

    @property
    def total_qty(self) -> int:
        return sum(i.qty for i in self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


# --------------------------------------------------------------------------
# 方案
# --------------------------------------------------------------------------


@dataclass
class Plan:
    """一个可执行的省钱方案。"""

    label: str
    payable: float
    discount: float = 0.0
    coupon: str | None = None
    steps: list[str] = field(default_factory=list)

    @property
    def saved(self) -> float:
        return round(self.discount, 2)

    def explain(self) -> str:
        """一句话讲清楚这个方案。"""
        head = f"{self.label}：实付 {money(self.payable)}"
        if self.discount > 0:
            head += f"，省 {money(self.discount)}"
        return head


# --------------------------------------------------------------------------
# 比价
# --------------------------------------------------------------------------


def build_plans(wish: Wish, portfolio: CouponPortfolio) -> list[Plan]:
    """列出所有能算清楚的方案，按实付从低到高排序。

    只生成"确定能算"的方案：不用券、以及每张能确定金额的券各一个方案。
    看不懂的券不会变成方案 —— 那样等于凭空造优惠。
    """
    if not wish:
        return []

    gross = wish.gross
    names = wish.item_names
    plans: list[Plan] = [
        Plan(
            label="原价直接点",
            payable=gross,
            discount=0.0,
            steps=[f"{i.name} × {i.qty}，{money(i.unit_price)}/份" for i in wish.items],
        )
    ]

    for rule in portfolio.match(names):
        if not rule.is_computable:
            continue
        discount = rule.discount_for(gross)
        if discount <= 0:
            continue
        plans.append(
            Plan(
                label=f"用券：{rule.raw}",
                payable=round(gross - discount, 2),
                discount=discount,
                coupon=rule.raw,
                steps=[
                    f"先领券「{rule.raw}」（{rule.describe()}）",
                    f"再点：{'、'.join(f'{i.name}×{i.qty}' for i in wish.items)}",
                ],
            )
        )

    plans.sort(key=lambda p: (p.payable, -p.discount, p.label))
    return plans


def best_plan(wish: Wish, portfolio: CouponPortfolio) -> Plan | None:
    """最省的那个方案。"""
    plans = build_plans(wish, portfolio)
    return plans[0] if plans else None


# --------------------------------------------------------------------------
# 凑单判断
# --------------------------------------------------------------------------


@dataclass
class TopUpAdvice:
    """为够上满减门槛而加购，值不值得。"""

    coupon: str
    threshold: float
    shortfall: float  # 还差多少
    add_on: MenuItem | None  # 最便宜的补位商品
    net_change: float  # 加购后实付的变化（负数 = 更省）
    worth_it: bool
    reason: str

    def explain(self) -> str:
        return self.reason


def advise_topup(
    wish: Wish, portfolio: CouponPortfolio, menu: Menu
) -> list[TopUpAdvice]:
    """对每张"差一点就能用"的满减券，判断加购划不划算。

    判断标准只有一条：**加购花掉的钱 是否小于 券减掉的钱**。

        划算  <=>  补位商品价格 < 券面额

    这条看起来像废话，但它正是"凑单陷阱"的核心 ——
    为了减 7 元先多花 13 元，是净亏 6 元。
    """
    if not wish:
        return []

    gross = wish.gross
    names = wish.item_names
    advices: list[TopUpAdvice] = []

    for rule in portfolio.match(names):
        if rule.kind != KIND_THRESHOLD:
            continue
        shortfall = rule.shortfall(gross)
        if shortfall <= 0:
            continue  # 已经够了，不需要凑单

        if not menu:
            advices.append(
                TopUpAdvice(
                    coupon=rule.raw,
                    threshold=rule.threshold,
                    shortfall=shortfall,
                    add_on=None,
                    net_change=0.0,
                    worth_it=False,
                    reason=(
                        f"「{rule.raw}」还差 {money(shortfall)} 才能用。"
                        "这次没拿到门店菜单，算不出加购哪件最便宜，先不给你建议。"
                    ),
                )
            )
            continue

        add_on = menu.cheapest_at_least(shortfall)
        if add_on is None:
            advices.append(
                TopUpAdvice(
                    coupon=rule.raw,
                    threshold=rule.threshold,
                    shortfall=shortfall,
                    add_on=None,
                    net_change=0.0,
                    worth_it=False,
                    reason=(
                        f"还差 {money(shortfall)} 才能用「{rule.raw}」，"
                        "但菜单上没有够得着的商品，这张券这次用不上了。"
                    ),
                )
            )
            continue

        net = round(add_on.price - rule.value, 2)
        worth = net < 0
        if worth:
            reason = (
                f"还差 {money(shortfall)} 才能用「{rule.raw}」。"
                f"加一件 {add_on.name}（{money(add_on.price)}）就够，"
                f"减 {money(rule.value)} —— 净省 {money(-net)}，值得加。"
            )
        else:
            reason = (
                f"还差 {money(shortfall)} 才能用「{rule.raw}」。"
                f"最便宜的补位是 {add_on.name}（{money(add_on.price)}），"
                f"但券只减 {money(rule.value)} —— 加完反而多花 {money(net)}，"
                "不划算，别凑。"
            )

        advices.append(
            TopUpAdvice(
                coupon=rule.raw,
                threshold=rule.threshold,
                shortfall=shortfall,
                add_on=add_on,
                net_change=net,
                worth_it=worth,
                reason=reason,
            )
        )

    advices.sort(key=lambda a: (not a.worth_it, a.net_change))
    return advices


# --------------------------------------------------------------------------
# 组合价评估
# --------------------------------------------------------------------------


@dataclass
class BundleCheck:
    """组合价与单点价的对比。"""

    label: str
    single_total: float
    bundle_price: float
    difference: float  # 正数 = 组合价更贵

    @property
    def bundle_is_cheaper(self) -> bool:
        return self.difference < 0

    def explain(self) -> str:
        if self.bundle_is_cheaper:
            return (
                f"「{self.label}」组合价 {money(self.bundle_price)}，"
                f"单点要 {money(self.single_total)}，便宜 {money(-self.difference)}。"
            )
        return (
            f"「{self.label}」组合价 {money(self.bundle_price)}，"
            f"单点只要 {money(self.single_total)}，反而贵 {money(self.difference)}，别选套餐。"
        )


def compare_bundle(items: list[MenuItem], bundle_price: float, label: str) -> BundleCheck:
    """算一下套餐是不是真比单点便宜。

    套餐经常被默认为"更划算"，但并不总是 —— 尤其是单点正好有券的时候。
    """
    single = round(sum(i.price for i in items), 2)
    return BundleCheck(
        label=label,
        single_total=single,
        bundle_price=round(bundle_price, 2),
        difference=round(bundle_price - single, 2),
    )


# --------------------------------------------------------------------------
# 券的使用建议总览
# --------------------------------------------------------------------------


def coupon_brief(portfolio: CouponPortfolio) -> list[str]:
    """把手上这些券说清楚：哪些能算、哪些算不了、为什么。

    诚实是这个函数的全部意义 —— 用户应该知道"没算进去"的券有哪些，
    而不是以为工具已经把所有优惠都用上了。
    """
    lines: list[str] = []
    computable = [r for r in portfolio.rules if r.is_computable]
    others = [r for r in portfolio.rules if not r.is_computable]

    if computable:
        lines.append(f"能算出金额的有 {len(computable)} 张：")
        lines += [f"  · {r.raw} —— {r.describe()}" for r in computable]

    if others:
        lines.append(f"\n有 {len(others)} 张券算不出确定金额，本次没有计入优惠：")
        lines += [f"  · {r.describe()}" for r in others]
        lines.append("\n原因是：这些券的优惠额取决于具体商品或购买件数，")
        lines.append("接口只返回券名，推不出来。宁可少算，也不给你一个到店付不出来的价。")

    return lines
