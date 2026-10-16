"""数据模型 —— 把麦当劳 MCP 返回的异构 JSON 归一化成稳定的内部结构。

设计原则（这是整个项目的基石）：

    MCP 各 Tool 的字段名会随版本演进（1.0.0 -> 1.0.9 已经改过多轮），
    所以这里统一采用「宽容取值」（tolerant access）：
    按别名列表依次尝试，取不到就降级为 None / 0，绝不让一个缺字段
    导致整份年报生成失败。

    所有金额统一为 float（元），所有时间统一为 naive datetime（本地时区）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------
# 缺省值
# --------------------------------------------------------------------------

_MISSING = object()


# --------------------------------------------------------------------------
# 底层取值工具
# --------------------------------------------------------------------------


def pick(data: Mapping[str, Any] | None, *keys: str, default: Any = None) -> Any:
    """按别名顺序从 dict 中取第一个「存在且非空」的值。

    支持 "a.b.c" 形式的浅层路径（本项目够用，不引入完整 JSONPath）。
    """
    if not isinstance(data, Mapping):
        return default
    for key in keys:
        if "." in key:
            node: Any = data
            ok = True
            for part in key.split("."):
                if isinstance(node, Mapping) and part in node:
                    node = node[part]
                else:
                    ok = False
                    break
            if ok and node is not None and node != "":
                return node
            continue
        value = data.get(key, _MISSING)
        if value is not _MISSING and value is not None and value != "":
            return value
    return default


def to_float(value: Any, default: float = 0.0) -> float:
    """把 "¥12.5" / "12.5元" / 12.5 / None 统一转成 float。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def to_int(value: Any, default: int = 0) -> int:
    """取整数，带四舍五入。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return default
    try:
        return int(round(float(match.group(0))))
    except ValueError:
        return default


_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d%H%M%S",
)


def parse_datetime(value: Any) -> datetime | None:
    """解析时间。兼容 ISO8601、常见中文格式、以及毫秒/秒级时间戳。"""
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value.replace(tzinfo=None)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = float(value)
        if stamp > 1e11:  # 毫秒
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit() and len(text) == 13:
        return parse_datetime(int(text))

    normalized = text.replace("Z", "").split("+")[0].strip()
    normalized = normalized.replace("T", " ")
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# 渠道归一化
# --------------------------------------------------------------------------

CHANNEL_LABELS: dict[str, str] = {
    "dine_in": "到店取餐",
    "delivery": "麦乐送",
    "drive_thru": "得来速",
    "group": "团餐",
    "unknown": "其他",
}

_CHANNEL_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("外送", "外卖", "麦乐送", "delivery", "配送"), "delivery"),
    (("得来速", "车道", "drivethru", "drive_thru", "drive-thru"), "drive_thru"),
    (("团餐", "企业", "助餐", "group"), "group"),
    (("到店", "堂食", "自取", "取餐", "柜台", "dinein", "dine_in", "takeout"), "dine_in"),
)


def normalize_channel(raw: Any) -> str:
    """把五花八门的渠道文案映射到 5 个规范值。"""
    text = str(raw or "").strip().lower()
    if not text:
        return "unknown"
    for keywords, channel in _CHANNEL_RULES:
        for keyword in keywords:
            if keyword in text:
                return channel
    return "unknown"


# --------------------------------------------------------------------------
# 领域模型
# --------------------------------------------------------------------------


@dataclass
class OrderItem:
    """订单中的一个餐品行。"""

    name: str
    qty: int = 1
    unit_price: float = 0.0  # 原价单价
    paid_amount: float = 0.0  # 该行实付合计（折扣已分摊）
    code: str | None = None
    category: str | None = None
    calories: float | None = None

    @property
    def gross(self) -> float:
        """原价合计。"""
        return round(self.unit_price * self.qty, 2)

    @property
    def saved(self) -> float:
        """该行省下的金额。"""
        return round(max(self.gross - self.paid_amount, 0.0), 2)

    @property
    def unit_paid(self) -> float:
        """折后单价（用于比价，避免只看总价被份数误导）。"""
        return round(self.paid_amount / self.qty, 2) if self.qty else 0.0


@dataclass
class Order:
    """一笔订单。"""

    order_id: str
    created_at: datetime | None = None
    channel: str = "unknown"
    store: str | None = None
    items: list[OrderItem] = field(default_factory=list)
    gross: float = 0.0
    paid: float = 0.0
    discount: float = 0.0
    coupons: list[str] = field(default_factory=list)
    delivery_fee: float = 0.0

    # -- 派生属性 ---------------------------------------------------------

    @property
    def saved(self) -> float:
        return round(max(self.gross - self.paid, 0.0), 2)

    @property
    def discount_rate(self) -> float:
        """折扣率：0.25 表示省了两成五。"""
        if self.gross <= 0:
            return 0.0
        return round(self.saved / self.gross, 4)

    @property
    def had_discount(self) -> bool:
        return self.saved > 0.005 or bool(self.coupons)

    @property
    def total_qty(self) -> int:
        return sum(item.qty for item in self.items)

    @property
    def calories(self) -> float:
        return round(sum((item.calories or 0.0) * item.qty for item in self.items), 1)

    @property
    def weekday(self) -> int:
        """0=周一 ... 6=周日"""
        return self.created_at.weekday() if self.created_at else -1

    @property
    def hour(self) -> int:
        return self.created_at.hour if self.created_at else -1

    @property
    def month_key(self) -> str:
        """YYYY-MM，无时间时归入 '未知'。"""
        return self.created_at.strftime("%Y-%m") if self.created_at else "未知"

    @property
    def day_key(self) -> str:
        return self.created_at.strftime("%Y-%m-%d") if self.created_at else "未知"


@dataclass
class Coupon:
    """一张优惠券（仅保留做审计需要的字段）。"""

    coupon_id: str
    name: str
    kind: str = "unknown"  # item_price / percent / threshold / bundle_price / free_gift
    threshold: float = 0.0
    value: float = 0.0
    scope: str | None = None
    source: str = "unknown"  # my / available / store / bound
    expires_at: datetime | None = None

    @property
    def is_expiring_soon(self) -> bool:
        """是否在 7 天内过期（用于「券即将失效」提醒）。"""
        if self.expires_at is None:
            return False
        delta = self.expires_at - datetime.now()
        return timedelta(0) <= delta <= timedelta(days=7)


# --------------------------------------------------------------------------
# 解析入口
# --------------------------------------------------------------------------

_CHANNEL_KEYS = ("channel", "orderChannel", "orderType", "mealType", "deliveryType", "type", "scene")
_TIME_KEYS = ("createdAt", "createTime", "orderTime", "orderDate", "businessTime", "payTime", "time")
_STORE_KEYS = ("storeName", "store.name", "store", "shopName", "restaurantName", "storeCode")
_ID_KEYS = ("orderId", "orderNo", "id", "orderNumber", "tradeNo")
_ITEM_LIST_KEYS = ("items", "products", "productList", "goodsList", "itemList", "orderItems", "details", "meals")


def _parse_item(raw: Mapping[str, Any]) -> OrderItem | None:
    """解析单个餐品行；连名字都取不到就丢弃（脏数据防御）。"""
    if not isinstance(raw, Mapping):
        return None

    name = pick(raw, "name", "productName", "mealName", "itemName", "goodsName", "title")
    if not name:
        return None

    qty = to_int(pick(raw, "qty", "quantity", "num", "count", "amount"), default=1)
    if qty <= 0:
        qty = 1

    unit_price = to_float(
        pick(raw, "unitPrice", "price", "originalPrice", "originPrice", "listPrice", "salePrice")
    )

    paid_total = pick(
        raw,
        "paidAmount",
        "actualAmount",
        "payAmount",
        "discountPrice",
        "finalPrice",
        "totalPrice",
        "subtotal",
        "amount",
    )
    if paid_total is None:
        paid_amount = round(unit_price * qty, 2)
    else:
        paid_amount = to_float(paid_total)

    calories_raw = pick(raw, "calories", "energy", "kcal", "calorie", "energyKcal")
    calories = to_float(calories_raw) if calories_raw is not None else None

    return OrderItem(
        name=str(name).strip(),
        qty=qty,
        unit_price=unit_price,
        paid_amount=paid_amount,
        code=str(pick(raw, "code", "productCode", "mealCode", "sku", "itemCode") or "") or None,
        category=str(pick(raw, "category", "categoryName", "typeName", "groupName") or "") or None,
        calories=calories,
    )


def _collect_coupon_names(raw: Mapping[str, Any]) -> list[str]:
    """从订单里捞出用过的券名（兼容字符串数组 / 对象数组两种形态）。"""
    for key in ("coupons", "couponList", "couponNames", "usedCoupons", "discountList", "promotions"):
        value = raw.get(key)
        if not value:
            continue
        names: list[str] = []
        if isinstance(value, str):
            names.append(value)
        elif isinstance(value, Sequence):
            for entry in value:
                if isinstance(entry, str):
                    names.append(entry)
                elif isinstance(entry, Mapping):
                    label = pick(entry, "name", "couponName", "title", "desc", "description")
                    if label:
                        names.append(str(label))
        if names:
            return names
    return []


def parse_order(raw: Mapping[str, Any], index: int = 0) -> Order | None:
    """把一个原始订单 dict 解析成 Order。"""
    if not isinstance(raw, Mapping):
        return None

    items: list[OrderItem] = []
    for key in _ITEM_LIST_KEYS:
        candidate = raw.get(key)
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            for entry in candidate:
                parsed = _parse_item(entry) if isinstance(entry, Mapping) else None
                if parsed:
                    items.append(parsed)
            if items:
                break

    # ---- 金额归属：原价（gross）与实付（paid）----------------------------
    #
    # 这里有一条经过权衡的规则：gross 取「明细求和」与「服务端原价字段」中的较大者。
    #
    #   情况 A：明细里是原价，服务端的 totalAmount 其实是应付价（较低）
    #           -> max 取明细求和，原价正确。
    #   情况 B：明细被截断/不完整，服务端的 totalAmount 才是完整原价（较高）
    #           -> max 取服务端字段，避免低估原价、漏报优惠。
    #
    # 两种常见的数据形态都能得到正确结果，且偏向"宁可高估原价"——
    # 因为高估原价最多让"省了多少"偏乐观，而低估原价会让用户以为自己没省到钱。
    items_gross = round(sum(item.gross for item in items), 2)
    provided_gross = to_float(
        pick(
            raw,
            "originalAmount",
            "originAmount",
            "grossAmount",
            "originalTotalAmount",
            "totalAmount",
            "orderAmount",
        )
    )
    gross = max(items_gross, provided_gross)

    # 实付只认明确的"应付/实付"字段，绝不把订单总额误当实付。
    provided_paid = pick(
        raw,
        "payAmount",
        "paidAmount",
        "actualAmount",
        "finalAmount",
        "realAmount",
        "paymentAmount",
    )
    paid = to_float(provided_paid) if provided_paid is not None else 0.0

    discount_raw = pick(raw, "discountAmount", "totalDiscount", "savedAmount", "preferentialAmount", "couponAmount")
    discount = to_float(discount_raw) if discount_raw is not None else 0.0

    # 若只有原价、没有实付：用 实付 = 原价 - 优惠 反推。
    if paid <= 0 and gross > 0:
        paid = round(max(gross - discount, 0.0), 2)

    # 若只有实付、没有原价：用 原价 = 实付 + 优惠 反推，保证 saved 可算。
    if gross <= 0 and paid > 0:
        gross = round(paid + discount, 2)

    # 仍未取到优惠金额时，用差额推断。
    if discount_raw is None:
        discount = round(max(gross - paid, 0.0), 2)

    order_id = str(pick(raw, *_ID_KEYS) or f"ORD-{index:05d}")
    store = pick(raw, *_STORE_KEYS)
    store_name = str(store).strip() if isinstance(store, (str, int)) else None

    return Order(
        order_id=order_id,
        created_at=parse_datetime(pick(raw, *_TIME_KEYS)),
        channel=normalize_channel(pick(raw, *_CHANNEL_KEYS)),
        store=store_name,
        items=items,
        gross=gross,
        paid=paid,
        discount=discount,
        coupons=_collect_coupon_names(raw),
        delivery_fee=to_float(pick(raw, "deliveryFee", "shippingFee", "packingFee")),
    )


def parse_orders(payload: Any) -> list[Order]:
    """把 MCP 的分页响应解析成订单列表。

    自动识别三种常见外壳：裸数组、{data: [...]}、{data: {list: [...]}}。
    """
    rows: Any = payload
    if isinstance(payload, Mapping):
        for key in ("data", "result", "orders", "list", "records", "items"):
            candidate = payload.get(key)
            if candidate is None:
                continue
            rows = candidate
            if isinstance(candidate, Mapping):
                for inner in ("list", "records", "items", "orders"):
                    if isinstance(candidate.get(inner), Sequence):
                        rows = candidate[inner]
                        break
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                break

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []

    orders: list[Order] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        parsed = parse_order(row, index)
        if parsed is not None:
            orders.append(parsed)
    return orders


def parse_coupons(payload: Any, source: str = "unknown") -> list[Coupon]:
    """解析券列表响应。"""
    rows: Any = payload
    if isinstance(payload, Mapping):
        for key in ("data", "result", "coupons", "list", "records", "items"):
            candidate = payload.get(key)
            if candidate is None:
                continue
            rows = candidate
            if isinstance(candidate, Mapping):
                for inner in ("list", "records", "items", "coupons"):
                    if isinstance(candidate.get(inner), Sequence):
                        rows = candidate[inner]
                        break
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                break

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []

    coupons: list[Coupon] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        name = pick(row, "name", "couponName", "title", "desc")
        if not name:
            continue
        coupons.append(
            Coupon(
                coupon_id=str(pick(row, "couponId", "id", "code") or f"CPN-{index:04d}"),
                name=str(name).strip(),
                kind=str(pick(row, "type", "couponType", "kind") or "unknown"),
                threshold=to_float(pick(row, "threshold", "minAmount", "condition")),
                value=to_float(pick(row, "value", "discount", "amount", "discountAmount")),
                scope=str(pick(row, "scope", "applicableScope") or "") or None,
                source=source,
                expires_at=parse_datetime(pick(row, "expireTime", "expiresAt", "endTime", "validEnd")),
            )
        )
    return coupons


def dedupe_orders(orders: Iterable[Order]) -> list[Order]:
    """按 order_id 去重（多页拉取时可能重叠），并按时间正序排列。"""
    seen: dict[str, Order] = {}
    for order in orders:
        if order.order_id not in seen:
            seen[order.order_id] = order
    return sorted(seen.values(), key=lambda o: (o.created_at or datetime.min, o.order_id))
