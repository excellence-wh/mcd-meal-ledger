"""券规则解析器 —— 从券名反推出可计算的规则。

    为什么需要这个模块？

    真实联调发现：麦当劳 MCP 的 `available-coupons` 只返回
    `couponName` / `couponImage` / `couponStatus` / `label` 四个字段 ——
    **没有门槛、没有面额、没有类型**。

    接口只告诉你"有一张叫『满40减10』的券"，
    但要把券算进方案里，需要的是结构化规则：满 40 元、减 10 元、全场通用。

    所以这个模块做的是：**用确定性的正则从券名里把规则挖出来**。
    中文券名格式相当稳定，规则解析完全够用 ——
    可测试、可复现、可解释，不需要任何模型参与。

    两条安全底线：

    1. **解析不出来就老实标 unknown，绝不猜。**
       宁可少算优惠，也不给用户一个到门店用不出来的方案。
    2. **绝不把"看不懂的券"美化成一个诱人的说法。**
       比如「第2杯半价」不是"全场五折" —— 那样会算出严重偏低的假价。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 券类型
# --------------------------------------------------------------------------

KIND_THRESHOLD = "threshold"  # 满减
KIND_PERCENT = "percent"  # 整单折扣
KIND_ITEM_PRICE = "item_price"  # 单品直降
KIND_FREE_GIFT = "free_gift"  # 买 N 送 M
KIND_BUNDLE_PRICE = "bundle_price"  # 组合价
KIND_FREE_DELIVERY = "free_delivery"  # 免配送费
KIND_PRODUCT = "product"  # 商品券（券名就是个商品，优惠在商品定义里）
KIND_NTH_ITEM = "nth_item"  # 第 N 件优惠（如第 2 杯半价）
KIND_UNKNOWN = "unknown"

KIND_LABELS: dict[str, str] = {
    KIND_THRESHOLD: "满减",
    KIND_PERCENT: "整单折扣",
    KIND_ITEM_PRICE: "单品直降",
    KIND_FREE_GIFT: "买赠",
    KIND_BUNDLE_PRICE: "组合价",
    KIND_FREE_DELIVERY: "免配送费",
    KIND_PRODUCT: "商品券",
    KIND_NTH_ITEM: "第N件优惠",
    KIND_UNKNOWN: "无法解析",
}

# 能从券名里推出确定金额的类型（其余类型的优惠额取决于具体商品，不能凭空算）
COMPUTABLE_KINDS = (KIND_THRESHOLD, KIND_PERCENT, KIND_ITEM_PRICE)

# --------------------------------------------------------------------------
# 券名里的"规则噪音"。
#
# ⚠️ 顺序很重要：必须把更长、更具体的模式放在前面。
# 否则「立减4元」会先被「减\d+元」吃掉，留下一个孤零零的「立」字，
# 结果把「巨无霸立减4元」的适用餐品错解析成「巨无霸立」。
# --------------------------------------------------------------------------

_NOISE_PATTERNS: tuple[str, ...] = (
    r"立减\s*\d+(?:\.\d+)?\s*元?",
    r"直降\s*\d+(?:\.\d+)?\s*元?",
    r"省\s*\d+(?:\.\d+)?\s*元?",
    r"满\s*\d+(?:\.\d+)?\s*元?",
    r"减\s*\d+(?:\.\d+)?\s*元?",
    r"免配送费",
    r"免外送费",
    r"第\s*\d+\s*杯半价",
    r"半价",
    r"\d+(?:\.\d+)?\s*折",
    r"买一送一",
    r"买\s*\d+\s*送\s*\d+",
    r"随心配",
    r"1\s*\+\s*1",
    r"新客",
    r"新人",
    r"专属",
    r"限定",
    r"仅限",
    r"到店取餐",
    r"到店",
    r"外送",
    r"麦乐送",
    r"堂食",
    r"任意消费",
    r"任意",
    r"消费",
    r"全场",
    r"通用",
    r"优惠券",
    r"代金券",
    r"抵扣券",
    r"现金券",
    r"券",
    r"[：:].*$",  # 冒号后面的通常是门店名或补充说明
)

# 门店名的特征 —— 命中的片段是"店"不是"餐品"，要丢掉而不是当适用范围
_STORE_HINTS = ("店", "餐厅", "门店", "广场", "中心", "大厦", "路", "街")


# 一次性券的特征词。
#
# ⚠️ 这个标记很关键：如果把「新客立减8元」当成可以长期复用的券，
# 那"每次都用券能省多少"会被严重高估 —— 新客券一辈子只能用一次。
# 凡是做长期预测（比如年度账）的地方，都必须先排除一次性券。
_ONE_TIME_HINTS = ("新客", "新人", "首单", "首次", "注册", "新用户", "新人专享")


@dataclass(frozen=True)
class CouponRule:
    """一张券的可计算规则。"""

    raw: str
    kind: str = KIND_UNKNOWN
    threshold: float = 0.0  # 满 X 元可用
    value: float = 0.0  # 满减/直降是金额；折扣是折扣率（0.95 表示 95 折）
    scope: tuple[str, ...] = ()  # 适用餐品关键词；空 = 全场通用
    confidence: str = "unknown"  # high / medium / unknown
    one_time: bool = False  # 是否一次性券（新客券之类）

    @property
    def is_general(self) -> bool:
        """是否全场通用（不限具体餐品）。"""
        return not self.scope

    @property
    def is_computable(self) -> bool:
        """是否能算出确定的优惠金额。"""
        return self.kind in COMPUTABLE_KINDS

    @property
    def is_usable(self) -> bool:
        """是否可用（用于匹配餐品）。"""
        return self.kind != KIND_UNKNOWN and self.confidence != "unknown"

    def describe(self) -> str:
        """给用户看的一句话说明。"""
        target = "全场" if self.is_general else "、".join(self.scope)
        if self.kind == KIND_THRESHOLD:
            return f"满 {self.threshold:g} 元减 {self.value:g} 元（{target}）"
        if self.kind == KIND_PERCENT:
            return f"整单 {self.value * 10:g} 折（{target}）"
        if self.kind == KIND_ITEM_PRICE:
            return f"{target} 直降 {self.value:g} 元"
        if self.kind == KIND_FREE_GIFT:
            return f"{target}：买就送，具体以券面为准"
        if self.kind == KIND_BUNDLE_PRICE:
            return f"{target}：可走组合价，具体以券面为准"
        if self.kind == KIND_FREE_DELIVERY:
            if self.threshold > 0:
                return f"满 {self.threshold:g} 元免配送费"
            return "免配送费"
        if self.kind == KIND_PRODUCT:
            return f"「{self.raw}」是一张商品券，优惠金额写在商品定义里"
        if self.kind == KIND_NTH_ITEM:
            return f"「{self.raw}」是第 N 件优惠，需要知道买几件才算得出来"
        return f"「{self.raw}」的规则看不出来，本次不参与计算"

    def discount_for(self, subtotal: float) -> float:
        """在给定小计下，这张券能减多少钱。

        只对能确定金额的三种券（满减 / 整单折扣 / 单品直降）返回数字，
        其余一律返回 0 —— 宁可少算，绝不猜。
        """
        if not self.is_computable or subtotal <= 0:
            return 0.0
        if self.kind == KIND_THRESHOLD:
            return round(self.value, 2) if subtotal >= self.threshold else 0.0
        if self.kind == KIND_PERCENT:
            return round(subtotal * (1 - self.value), 2)
        if self.kind == KIND_ITEM_PRICE:
            return round(min(self.value, subtotal), 2)
        return 0.0

    def shortfall(self, subtotal: float) -> float:
        """还差多少钱才能用上这张满减券（0 表示已经够）。"""
        if self.kind != KIND_THRESHOLD or subtotal >= self.threshold:
            return 0.0
        return round(self.threshold - subtotal, 2)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

_RE_THRESHOLD = re.compile(r"满\s*(\d+(?:\.\d+)?)\s*元?\s*减\s*(\d+(?:\.\d+)?)")
_RE_FREE_DELIVERY = re.compile(r"满\s*(\d+(?:\.\d+)?)\s*元?\s*免(?:配送费|外送费)")
_RE_FREE_DELIVERY_PLAIN = re.compile(r"免(?:配送费|外送费)")
_RE_DISCOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*折")
_RE_ITEM_CUT = re.compile(r"(?:立减|直降|省)\s*(\d+(?:\.\d+)?)\s*元?")
_RE_BUY_GET = re.compile(r"买\s*\d+\s*送\s*\d+")
_RE_NTH_HALF = re.compile(r"第\s*\d+\s*杯半价")
_RE_BUNDLE = re.compile(r"随心配|1\s*\+\s*1")


def _extract_scope(name: str) -> tuple[str, ...]:
    """从券名里剥掉规则噪音，剩下的就是适用餐品。

    会把门店名（如「望京SOHO店」）剔掉 —— 门店名限定的是"哪家店能用"，
    不是"买哪个餐品能用"，把它当成适用范围会导致券永远匹配不上。
    """
    text = name
    for pattern in _NOISE_PATTERNS:
        text = re.sub(pattern, " ", text)

    parts = re.split(r"[、,，/／+＋\s]+", text)
    keywords: list[str] = []
    for part in parts:
        part = part.strip(" -—·*（）()[]【】")
        if len(part) < 2:
            continue
        if part in ("套餐", "商品", "餐品", "单品", "指定"):
            continue
        if any(hint in part for hint in _STORE_HINTS):
            continue  # 门店名，不是餐品
        keywords.append(part)
    return tuple(dict.fromkeys(keywords))  # 去重且保序


def parse_coupon_rule(name: str) -> CouponRule:
    """把一条券名解析成规则。解析不出来就老实说解析不出来。"""
    raw = str(name or "").strip()
    if not raw:
        return CouponRule(raw=raw, kind=KIND_UNKNOWN, confidence="unknown")

    scope = _extract_scope(raw)
    one_time = any(hint in raw for hint in _ONE_TIME_HINTS)

    def make(
        kind: str,
        confidence: str,
        *,
        threshold: float = 0.0,
        value: float = 0.0,
    ) -> CouponRule:
        """统一收口构造，避免每个分支都要记得带上 one_time。"""
        return CouponRule(
            raw=raw,
            kind=kind,
            threshold=threshold,
            value=value,
            scope=scope,
            confidence=confidence,
            one_time=one_time,
        )

    # ① 满 X 减 Y（最明确，优先）
    m = _RE_THRESHOLD.search(raw)
    if m:
        return make(KIND_THRESHOLD, "high", threshold=float(m.group(1)), value=float(m.group(2)))

    # ② 满 X 免配送费
    m = _RE_FREE_DELIVERY.search(raw)
    if m:
        return make(KIND_FREE_DELIVERY, "high", threshold=float(m.group(1)))
    if _RE_FREE_DELIVERY_PLAIN.search(raw):
        return make(KIND_FREE_DELIVERY, "high")

    # ③ 第 N 杯半价
    #    ⚠️ 这**不是**整单五折！它只对第 N 件生效，而券名里没有件数信息，
    #    所以标成不可计算。曾经把它当成整单半价，会算出严重偏低的假价。
    if _RE_NTH_HALF.search(raw):
        return make(KIND_NTH_ITEM, "medium", value=0.5)

    # ④ 整单折扣：95 折与 9.5 折都表示"付 95%"。
    #    判定规则：数字 >= 10 视为百分数，< 10 视为几折。
    m = _RE_DISCOUNT.search(raw)
    if m:
        number = float(m.group(1))
        rate = number / 100 if number >= 10 else number / 10
        if 0 < rate < 1:
            return make(KIND_PERCENT, "high", value=round(rate, 4))

    # ⑤ 买 N 送 M
    if _RE_BUY_GET.search(raw) or "买一送一" in raw:
        return make(KIND_FREE_GIFT, "high")

    # ⑥ 组合价（如「1+1 随心配」）
    if _RE_BUNDLE.search(raw):
        return make(KIND_BUNDLE_PRICE, "medium")

    # ⑦ 单品直降
    m = _RE_ITEM_CUT.search(raw)
    if m:
        return make(KIND_ITEM_PRICE, "high", value=float(m.group(1)))

    # ⑧ 券名本身就是个商品（如「王牌小食拼」「猪柳蛋套餐」）。
    #    这类券的优惠额写在商品定义里，从券名推不出来 ——
    #    如实标成「商品券」，绝不能包装成"买一送一"这种诱人但没依据的说法。
    if scope:
        return make(KIND_PRODUCT, "medium")

    return make(KIND_UNKNOWN, "unknown")


def parse_coupon_rules(names: list[str]) -> list[CouponRule]:
    """批量解析，并按"可计算程度"排序（越明确越靠前）。"""
    order = {"high": 0, "medium": 1, "unknown": 2}
    rules = [parse_coupon_rule(name) for name in names]
    return sorted(rules, key=lambda r: (order.get(r.confidence, 9), r.raw))


@dataclass
class CouponPortfolio:
    """手上的一把券，以及它们的解析情况。"""

    rules: list[CouponRule] = field(default_factory=list)

    @classmethod
    def from_names(cls, names: list[str]) -> CouponPortfolio:
        return cls(rules=parse_coupon_rules(names))

    @property
    def computable(self) -> list[CouponRule]:
        return [r for r in self.rules if r.is_computable]

    @property
    def unparsed(self) -> list[CouponRule]:
        return [r for r in self.rules if not r.is_usable]

    @property
    def reusable(self) -> CouponPortfolio:
        """排除一次性券之后剩下能长期复用的部分。

        做年度预测时必须用这个，而不是用全部券 ——
        「新客立减8元」一辈子只能领一次，拿它乘以一年的次数是虚高的。
        """
        return CouponPortfolio(rules=[r for r in self.rules if not r.one_time])

    @property
    def one_time(self) -> list[CouponRule]:
        return [r for r in self.rules if r.one_time]

    def match(self, item_names: list[str]) -> list[CouponRule]:
        """挑出能用在这批餐品上的券（全场券 + 关键词命中的券）。"""
        hits: list[CouponRule] = []
        for rule in self.rules:
            if not rule.is_usable:
                continue
            if rule.is_general:
                hits.append(rule)
                continue
            if any(keyword in name for keyword in rule.scope for name in item_names):
                hits.append(rule)
        return hits

    def best_single(self, item_names: list[str], subtotal: float) -> float:
        """挑出单张能减最多的金额。

        保守原则：**默认不叠加，只取最优的单张** ——
        叠加规则各券不同且接口不返回，宁可少报优惠，
        也绝不给用户一个到店付不出来的价格。
        """
        return max((r.discount_for(subtotal) for r in self.match(item_names)), default=0.0)
