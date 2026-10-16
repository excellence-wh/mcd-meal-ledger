"""营养模块 —— 把你吃掉的东西翻译成数字，并在约束下找最省的搭配。

    两条必须守住的线：

    **一、合规。** 赛事规则明确禁止「宣扬不健康饮食方式」。所以本模块
    只做两件事：陈述事实（你吃了多少热量、多少蛋白质）、按用户自己
    设定的约束筛选组合。**不下健康结论、不给医疗或营养建议。**
    用户问「这样吃健康吗」，正确的回答是引导他去问专业人士。

    **二、覆盖率要说清楚。** 麦当劳官方营养表覆盖的是标准餐品，
    套餐、特制、限量品经常对不上。所以画像里会明确写出
    「多少单能对上营养表、多少单对不上」，而不是假装算全了。

    营养数据来自麦当劳 MCP 的 `list-nutrition-foods`，
    离线时用内置样例数据，两者结构由 `parse_nutrition_foods` 统一归一化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations, product

from .analytics import money
from .models import Order, classify_item, pick, rows_of, to_float, unwrap
from .saver import Menu

# 分类里算"主食"的品类 —— 用来避免推荐出"三杯饮料"这种荒唐组合
MAIN_CATEGORIES = ("汉堡", "早餐")

# 营养字段别名表（真实字段名待联调确认，这里做宽容匹配）
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "calories": ("calories", "energy", "kcal", "calorie", "energyKcal", "heat"),
    "protein": ("protein", "proteins", "proteinG", "protein_g"),
    "fat": ("fat", "fats", "fatG", "fat_g"),
    "carbs": ("carbohydrate", "carbohydrates", "carbs", "carbohydrateG", "sugar"),
    "sodium": ("sodium", "sodiumMg", "na"),
    "calcium": ("calcium", "calciumMg", "ca"),
}


@dataclass(frozen=True)
class NutritionFact:
    """一件餐品的营养成分。单位：热量 kcal，蛋白质/脂肪/碳水 g，钠/钙 mg。"""

    name: str
    calories: float = 0.0
    protein: float = 0.0
    fat: float = 0.0
    carbs: float = 0.0
    sodium: float = 0.0
    calcium: float = 0.0

    @property
    def has_data(self) -> bool:
        return self.calories > 0

    def scaled(self, qty: int) -> NutritionFact:
        return NutritionFact(
            name=self.name,
            calories=round(self.calories * qty, 1),
            protein=round(self.protein * qty, 1),
            fat=round(self.fat * qty, 1),
            carbs=round(self.carbs * qty, 1),
            sodium=round(self.sodium * qty, 1),
            calcium=round(self.calcium * qty, 1),
        )


@dataclass
class NutritionTotal:
    """一批餐品的营养合计。"""

    calories: float = 0.0
    protein: float = 0.0
    fat: float = 0.0
    carbs: float = 0.0
    sodium: float = 0.0
    calcium: float = 0.0
    known_items: int = 0  # 能对上营养表的件数
    unknown_items: int = 0  # 对不上的件数

    @property
    def coverage(self) -> float:
        total = self.known_items + self.unknown_items
        return round(self.known_items / total, 4) if total else 0.0

    @property
    def macro_ratio(self) -> dict[str, float]:
        """三大产能营养素的热量占比（蛋白/脂肪各 4/9 kcal per g，碳水 4）。"""
        p = self.protein * 4
        f = self.fat * 9
        c = self.carbs * 4
        total = p + f + c
        if total <= 0:
            return {"protein": 0.0, "fat": 0.0, "carbs": 0.0}
        return {
            "protein": round(p / total, 4),
            "fat": round(f / total, 4),
            "carbs": round(c / total, 4),
        }

    def describe(self) -> str:
        parts = [f"{self.calories:g} 大卡"]
        if self.protein:
            parts.append(f"蛋白质 {self.protein:g}g")
        if self.fat:
            parts.append(f"脂肪 {self.fat:g}g")
        if self.carbs:
            parts.append(f"碳水 {self.carbs:g}g")
        return " · ".join(parts)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def _rows(payload: object) -> list[dict]:
    """从可能嵌套的响应里取出营养数据行。"""
    rows = rows_of(payload, "list", "records", "foods", "items", "nutritionList", "data")
    if rows:
        return [r for r in rows if isinstance(r, dict)]
    node = unwrap(payload)
    if isinstance(node, dict) and node and all(isinstance(v, dict) for v in node.values()):
        # 也可能是 { 餐品名: {营养...} } 的字典形态
        return [{"name": k, **v} for k, v in node.items() if isinstance(v, dict)]
    return []


def parse_nutrition_foods(payload: object) -> dict[str, NutritionFact]:
    """把营养接口的返回归一化成 {餐品名: NutritionFact}。

    真实字段名待联调确认，因此这里用别名表做宽容匹配 ——
    和订单解析一个思路：不靠猜，靠多路匹配 + 缺字段降级。
    """
    table: dict[str, NutritionFact] = {}
    for row in _rows(payload):
        name = pick(row, "name", "foodName", "productName", "mealName", "itemName", "title")
        if not name:
            continue
        values: dict[str, float] = {}
        for field_name, aliases in _FIELD_ALIASES.items():
            raw = pick(row, *aliases)
            values[field_name] = to_float(raw) if raw is not None else 0.0
        table[str(name).strip()] = NutritionFact(name=str(name).strip(), **values)
    return table


# --------------------------------------------------------------------------
# 合计
# --------------------------------------------------------------------------


def total_nutrition(
    entries: list[tuple[str, int]], table: dict[str, NutritionFact]
) -> NutritionTotal:
    """按（餐品名, 份数）合计营养。对不上营养表的会被计入 unknown_items。"""
    total = NutritionTotal()
    for name, qty in entries:
        fact = table.get(name)
        if fact is None or not fact.has_data:
            total.unknown_items += qty
            continue
        scaled = fact.scaled(qty)
        total.calories += scaled.calories
        total.protein += scaled.protein
        total.fat += scaled.fat
        total.carbs += scaled.carbs
        total.sodium += scaled.sodium
        total.calcium += scaled.calcium
        total.known_items += qty

    total.calories = round(total.calories, 1)
    total.protein = round(total.protein, 1)
    total.fat = round(total.fat, 1)
    total.carbs = round(total.carbs, 1)
    total.sodium = round(total.sodium, 1)
    total.calcium = round(total.calcium, 1)
    return total


def order_nutrition(order: Order, table: dict[str, NutritionFact]) -> NutritionTotal:
    """算一笔订单的营养。

    套餐会被拆开算 —— 因为营养表是按单品给的，
    而"精选单人餐三件套"这种名字在表里根本查不到。
    """
    entries: list[tuple[str, int]] = []
    for item in order.items:
        if item.components:
            for comp_name, comp_qty in item.components:
                entries.append((comp_name, comp_qty * item.qty))
        else:
            entries.append((item.name, item.qty))
    return total_nutrition(entries, table)


# --------------------------------------------------------------------------
# 约束筛选：在营养限制下找最省的搭配
# --------------------------------------------------------------------------


@dataclass
class MealOption:
    """一个满足约束的搭配方案。"""

    items: list[tuple[str, int]]
    total: NutritionTotal
    price: float

    @property
    def label(self) -> str:
        return " + ".join(f"{n}×{q}" if q > 1 else n for n, q in self.items)

    def explain(self) -> str:
        return f"{self.label} —— {self.total.describe()}，{money(self.price)}"


@dataclass
class FitRequest:
    """一次搭配筛选的条件。全部可选，但至少要给一个。"""

    max_calories: float | None = None
    min_protein: float | None = None
    max_price: float | None = None
    max_items: int = 3  # 最多几样（不含加量）


@dataclass
class FitResult:
    """筛选结果。"""

    options: list[MealOption] = field(default_factory=list)
    candidates_scanned: int = 0
    menu_coverage: float = 0.0  # 菜单里有多少比例能对上营养表

    @property
    def has_options(self) -> bool:
        return bool(self.options)

    def headline(self) -> str:
        if not self.options:
            return "在给定的限制下没找到合适的搭配 —— 要么把限制放宽一点，要么这家店对得上营养表的餐品太少"
        best = self.options[0]
        return f"按你的限制，最省的一套是 {money(best.price)}：{best.explain()}"


def fit_meals(
    menu: Menu,
    table: dict[str, NutritionFact],
    request: FitRequest,
    top_n: int = 5,
) -> FitResult:
    """在营养与预算约束下，穷举出最省的若干搭配。

    穷举而不是启发式：菜单规模很小（几十项），
    上界是 C(菜单, 最多几样) × 加量组合，完全是可算的规模。
    **能穷举就不要用近似** —— 确定性是这个项目的立身之本。
    """
    # 只考虑既能对上营养表、又能拿到价格的餐品
    priced: list[tuple[str, float, NutritionFact, str]] = []
    for item in menu.items:
        fact = table.get(item.name)
        if fact is None or not fact.has_data or item.price <= 0:
            continue
        priced.append((item.name, item.price, fact, classify_item(item.name)))

    coverage = round(len(priced) / len(menu.items), 4) if menu.items else 0.0

    options: list[MealOption] = []
    scanned = 0
    kinds = min(request.max_items, len(priced))
    for size in range(1, kinds + 1):
        for combo in combinations(priced, size):
            # 至少要有一样主食，避免出现"三杯饮料"这种荒唐组合
            if not any(cat in MAIN_CATEGORIES for _, _, _, cat in combo):
                continue
            # 每样最多加到 2 份，避免搜索空间爆炸
            for qtys in product((1, 2), repeat=size):
                scanned += 1
                entries = [(name, q) for (name, _, _, _), q in zip(combo, qtys, strict=False)]
                price = round(sum(p * q for (_, p, _, _), q in zip(combo, qtys, strict=False)), 2)
                if request.max_price is not None and price > request.max_price:
                    continue
                total = total_nutrition(entries, table)
                if request.max_calories is not None and total.calories > request.max_calories:
                    continue
                if request.min_protein is not None and total.protein < request.min_protein:
                    continue
                options.append(MealOption(items=entries, total=total, price=price))

    # 去掉完全重复的组合，按价格从低到高排（同样便宜时热量低的优先）
    seen: set[tuple] = set()
    unique: list[MealOption] = []
    for opt in sorted(options, key=lambda o: (o.price, o.total.calories, o.label)):
        key = tuple(sorted(opt.items))
        if key in seen:
            continue
        seen.add(key)
        unique.append(opt)
        if len(unique) >= top_n:
            break

    return FitResult(options=unique, candidates_scanned=scanned, menu_coverage=coverage)


# --------------------------------------------------------------------------
# 个人画像
# --------------------------------------------------------------------------


@dataclass
class NutritionProfile:
    """你实际吃成什么样 —— 只陈述事实，不下健康结论。"""

    orders_total: int = 0
    orders_covered: int = 0
    total: NutritionTotal = field(default_factory=NutritionTotal)
    by_category: list[tuple[str, float]] = field(default_factory=list)  # (品类, 热量)
    by_daypart: list[tuple[str, float]] = field(default_factory=list)
    avg_calories_per_order: float = 0.0

    @property
    def coverage(self) -> float:
        return round(self.orders_covered / self.orders_total, 4) if self.orders_total else 0.0

    def describe(self) -> list[str]:
        lines: list[str] = []
        if not self.total.known_items:
            return ["没有餐品能对上官方营养表，算不出营养画像"]
        lines.append(
            f"{self.orders_total} 单里有 {self.orders_covered} 单的餐品能对上官方营养表"
            f"（覆盖 {self.coverage * 100:.0f}%）。"
        )
        lines.append(
            f"这些单合计 {self.total.calories:g} 大卡，平均每单 {self.avg_calories_per_order:g} 大卡。"
        )
        ratio = self.total.macro_ratio
        if any(ratio.values()):
            lines.append(
                f"三大产能营养素的热量占比：蛋白质 {ratio['protein'] * 100:.0f}%、"
                f"脂肪 {ratio['fat'] * 100:.0f}%、碳水 {ratio['carbs'] * 100:.0f}%。"
            )
        if self.by_category:
            top = self.by_category[0]
            lines.append(f"热量来源里占比最高的是「{top[0]}」，约 {top[1]:g} 大卡。")
        if self.coverage < 1:
            lines.append(
                "对不上的部分（套餐、特制、限量品）没有被计入 —— "
                "所以这些数字是下限，不是全部。"
            )
        return lines


def build_nutrition_profile(
    orders: list[Order], table: dict[str, NutritionFact]
) -> NutritionProfile:
    """从真实历史订单算出营养画像。"""
    profile = NutritionProfile(orders_total=len(orders))
    if not orders or not table:
        return profile

    from .analytics import DAY_PARTS

    cat_cal: dict[str, float] = {}
    part_cal: dict[str, float] = {}
    grand = NutritionTotal()

    for order in orders:
        total = order_nutrition(order, table)
        if total.known_items > 0:
            profile.orders_covered += 1
        grand.calories += total.calories
        grand.protein += total.protein
        grand.fat += total.fat
        grand.carbs += total.carbs
        grand.sodium += total.sodium
        grand.calcium += total.calcium
        grand.known_items += total.known_items
        grand.unknown_items += total.unknown_items

        # 品类与时段的热量分布
        part = "未知"
        if order.created_at is not None:
            hour = order.created_at.hour
            for name, start, end in DAY_PARTS:
                if start <= hour < end:
                    part = name
                    break
        for item in order.items:
            entries = (
                [(n, q * item.qty) for n, q in item.components]
                if item.components
                else [(item.name, item.qty)]
            )
            for name, qty in entries:
                fact = table.get(name)
                if fact is None or not fact.has_data:
                    continue
                kcal = fact.calories * qty
                cat = classify_item(name)
                cat_cal[cat] = cat_cal.get(cat, 0.0) + kcal
                part_cal[part] = part_cal.get(part, 0.0) + kcal

    profile.total = grand
    profile.avg_calories_per_order = (
        round(grand.calories / profile.orders_covered, 1) if profile.orders_covered else 0.0
    )
    profile.by_category = sorted(
        ((k, round(v, 1)) for k, v in cat_cal.items()), key=lambda kv: -kv[1]
    )
    profile.by_daypart = sorted(
        ((k, round(v, 1)) for k, v in part_cal.items()), key=lambda kv: -kv[1]
    )
    return profile


DISCLAIMER = (
    "以上只是把你吃过的餐品换算成公开的营养数字，是事实陈述，"
    "不构成营养或医疗建议。身体相关的事情请咨询专业人士。"
)
