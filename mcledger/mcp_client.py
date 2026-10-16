"""麦当劳 MCP 客户端 —— Streamable HTTP + JSON-RPC 2.0，纯标准库实现。

    为什么自己手写而不是用官方 SDK：
      1. 零依赖是这个项目的硬承诺（任何人 clone 下来就能跑，不需要 npm/pip install）；
      2. MCP 的 Streamable HTTP 传输本身就是「POST 一个 JSON-RPC，可能回 JSON
         也可能回 SSE 流」，协议面很窄，手写反而比引入依赖更可控；
      3. 只有把调用链写明白，MCP_INTEGRATION.md 才讲得清楚。

    协议要点（来自 M-China/mcd-mcp-server 官方文档）：
      - 接入地址：https://mcp.mcd.cn
      - 传输协议：Streamable HTTP
      - 鉴权：请求头 Authorization: Bearer <MCP_TOKEN>
      - 限流：每 Token 每分钟最多 600 次请求，超限返回 429
      - 错误：401 = Token 无效/过期/未提供
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import rows_of, unwrap

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"
TOKEN_ENV = "MCD_MCP_TOKEN"

# 服务端限流：600 次/分钟。留出余量，避免把自己撞上 429。
_MIN_REQUEST_INTERVAL = 0.12


class MCPError(RuntimeError):
    """MCP 调用失败。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class MCPAuthError(MCPError):
    """401 —— Token 无效、过期或未提供。"""


class MCPRateLimitError(MCPError):
    """429 —— 触发限流。"""


# --------------------------------------------------------------------------


@dataclass
class ToolCall:
    """一次工具调用的结果。"""

    tool: str
    arguments: dict[str, Any]
    raw: Any = None
    text: str = ""
    is_error: bool = False


class ToolCaller(Protocol):
    """取数层真正需要的最小客户端接口：只要会调工具就行。

    拆成 Protocol 的好处有两个：
      1. 测试可以传入轻量替身（无需真连网）；
      2. 取数逻辑不依赖 MCPClient 的具体实现，将来换传输层不必改这里。
    """

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCall: ...


class MCPToolClient(ToolCaller, Protocol):
    """完整取数接口：在 ToolCaller 之上多一个「列出可用工具」。

    单独拆出来是为了让轻量替身只需实现 call_tool 就能通过类型检查 ——
    替身本来就不需要列工具。
    """

    def list_tools(self) -> list[str]: ...


def _extract_payload(response: dict[str, Any]) -> tuple[Any, str, bool]:
    """从 MCP 工具响应中抽出可用的数据结构。

    MCP 的返回形态有三种，按可靠性排序：
      1. structuredContent —— 结构化结果，最好用；
      2. content[].text     —— 文本块，通常是 JSON 字符串，需要再解析一层；
      3. 原始 result        —— 兜底。
    """
    if not isinstance(response, dict):
        return response, str(response), False

    is_error = bool(response.get("isError"))

    structured = response.get("structuredContent")
    if isinstance(structured, (dict, list)):
        return structured, json.dumps(structured, ensure_ascii=False), is_error

    content = response.get("content")
    if isinstance(content, list):
        chunks = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(chunks).strip()
        if text:
            try:
                return json.loads(text), text, is_error
            except json.JSONDecodeError:
                return text, text, is_error

    return response, json.dumps(response, ensure_ascii=False), is_error


@dataclass
class MCPClient:
    """一个极简但完整的 MCP Streamable HTTP 客户端。

    trace 是可选的调用日志回调。刻意**不**在库里直接 print：
    库一旦自己写 stdout，就会污染调用方的管道输出（例如
    `run.py --json-out` 之后还要把结果喂给别的程序）。
    想要日志的调用方自己传入一个写 stderr 的回调即可。
    """

    endpoint: str = DEFAULT_ENDPOINT
    token: str = ""
    timeout: float = 30.0
    trace: Callable[[str], None] | None = None

    _session_id: str = field(default="", init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)
    _request_id: int = field(default=0, init=False, repr=False)
    _last_call_at: float = field(default=0.0, init=False, repr=False)
    _tool_names: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.token:
            self.token = os.environ.get(TOKEN_ENV, "").strip()

    def _emit(self, message: str) -> None:
        """向调用方提供的回调输出一条调用日志（未设置回调时静默）。"""
        if self.trace is not None:
            self.trace(message)

    # -- 基础 ----------------------------------------------------------

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call_at
        if gap < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - gap)
        self._last_call_at = time.monotonic()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _rpc(self, method: str, params: dict[str, Any] | None = None, retries: int = 2) -> Any:
        """发一次 JSON-RPC 请求，自动处理 SSE 响应与 429 退避。"""
        if not self.has_token:
            raise MCPAuthError(
                f"未提供 MCP Token。请设置环境变量 {TOKEN_ENV}，"
                "或前往 https://open.mcd.cn/mcp 手机号登录后领取。"
            )

        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params or {}},
            ensure_ascii=False,
        ).encode("utf-8")

        attempt = 0
        while True:
            self._throttle()
            request = urllib.request.Request(
                self.endpoint, data=body, headers=self._headers(), method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    session = response.headers.get("Mcp-Session-Id")
                    if session:
                        self._session_id = session
                    payload = self._read_body(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    raise MCPAuthError(
                        "MCP Token 无效、已过期或未提供（HTTP 401）。"
                        "请到 https://open.mcd.cn/mcp 重新获取。",
                        code=401,
                    ) from exc
                if exc.code == 429:
                    if attempt < retries:
                        attempt += 1
                        time.sleep(1.5 * attempt)
                        continue
                    raise MCPRateLimitError(
                        "触发 MCP 限流（HTTP 429，上限 600 次/分钟）。请降低调用频率。", code=429
                    ) from exc
                # 尽力读取错误响应体，读不到就只报状态码。
                # 只吞网络/解码类异常 —— 这里不该掩盖真正的逻辑错误。
                detail = ""
                with contextlib.suppress(OSError, ValueError):
                    detail = exc.read().decode("utf-8", "replace")[:300]
                raise MCPError(f"HTTP {exc.code} 调用失败：{detail}", code=exc.code) from exc
            except urllib.error.URLError as exc:
                if attempt < retries:
                    attempt += 1
                    time.sleep(1.0 * attempt)
                    continue
                raise MCPError(f"网络错误，无法连接 {self.endpoint}：{exc.reason}") from exc

            if not payload:
                return None
            if "error" in payload:
                err = payload["error"] or {}
                raise MCPError(
                    f"JSON-RPC 错误 {err.get('code')}：{err.get('message')}",
                    code=err.get("code"),
                )
            return payload.get("result")

    @staticmethod
    def _read_body(response: Any) -> dict[str, Any] | None:
        """读取响应体，兼容 application/json 与 text/event-stream 两种返回。"""
        content_type = (response.headers.get("Content-Type") or "").lower()
        raw = response.read().decode("utf-8", "replace").strip()
        if not raw:
            return None

        if "text/event-stream" in content_type:
            # 逐行取 data: 后面第一个可解析的 JSON
            for line in raw.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    continue
            return None

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPError(f"响应不是合法 JSON：{raw[:200]}") from exc

    # -- MCP 生命周期 --------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        """握手。

        注意：麦当劳 MCP 服务端**不会返回 Mcp-Session-Id 响应头**。
        如果沿用"没有 session id 就重新握手"的判断，会导致每次 call_tool
        都额外发一次 initialize —— 真实联调时发现的性能缺陷。
        因此这里用独立的 _initialized 标记，而不是拿 session id 当哨兵。
        """
        if self._initialized:
            return {}
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "mcd-meal-ledger", "version": "1.0.0"},
            },
        )
        self._initialized = True
        # 部分实现要求显式发送 initialized 通知；服务端不认识这个方法时
        # 会返回 JSON-RPC 错误，但那不影响后续 tools/call。
        with contextlib.suppress(MCPError):
            self._rpc("notifications/initialized")
        return result if isinstance(result, dict) else {}

    def list_tools(self) -> list[str]:
        """列出服务端可用工具。"""
        if self._tool_names:
            return self._tool_names
        self.initialize()
        result = self._rpc("tools/list")
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        self._tool_names = [t.get("name", "") for t in tools if isinstance(t, dict)]
        return self._tool_names

    def list_tool_schemas(self) -> list[dict[str, Any]]:
        """取回带 inputSchema 的工具定义，用于核对必填参数。"""
        self.initialize()
        result = self._rpc("tools/list")
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        return [t for t in tools if isinstance(t, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
        """调用一个工具，返回归一化后的结果。

        麦当劳 MCP 的业务错误有两种表达：JSON-RPC 层的 error，
        以及 HTTP 200 + body 内 {success:false, code, message}。
        后者必须显式识别，否则会被当成"调用成功但没数据"而静默丢掉。
        """
        arguments = arguments or {}
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            return ToolCall(tool=name, arguments=arguments, raw=result, text=str(result))
        payload, text, is_error = _extract_payload(result)

        # 识别业务层错误：{success: false, code, message}
        message = ""
        if isinstance(payload, dict) and payload.get("success") is False:
            is_error = True
            message = str(payload.get("message") or "")
            code = payload.get("code")
            message = f"code={code} {message}".strip()

        if self.trace is not None:
            flag = f" ⚠️ {message[:120]}" if is_error else ""
            self._emit(f"[mcp] {name}({json.dumps(arguments, ensure_ascii=False)}){flag}")

        return ToolCall(
            tool=name,
            arguments=arguments,
            raw=payload,
            text=message or text,
            is_error=is_error,
        )


# --------------------------------------------------------------------------
# 业务层封装 —— 把工具名和参数收敛到一处，方便审计与文档
# --------------------------------------------------------------------------

# 券池的三个可查来源（第四路是 auto-bind-coupons 之后再查一次）。
# 少查一路，用户就少省钱 —— 这是本项目最重要的一条经验。
COUPON_SOURCES: dict[str, str] = {
    "available-coupons": "available",  # 麦麦省可领取的券
    "query-my-coupons": "my",  # 我账户里已有的券
    "query-store-coupons": "store",  # 当前门店专属券（需 storeCode+orderType+beType）
}

# beType 渠道编码（来自官方 inputSchema）：1-到店取餐 2-麦乐送 5-得来速 6-团餐
DEFAULT_BE_TYPE = 1
DEFAULT_ORDER_TYPE = 1

# 逐单补全详情的上限，避免账号历史极多时把请求数放大到不可控
DEFAULT_ENRICH_LIMIT = 200


@dataclass
class MCPBundle:
    """一次完整取数的结果包。"""

    orders_raw: Any = None
    coupons_raw: list[Any] = field(default_factory=list)
    meals_raw: Any = None
    campaigns_raw: Any = None
    account_raw: Any = None
    tools_available: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    enriched: int = 0  # 实际补全详情的订单数


def _fetch_orders(client: ToolCaller, page_size: int, max_pages: int) -> tuple[list[Any], list[str]]:
    """拉取历史订单。

    真实联调结论：order-list 的 inputSchema 没有任何参数，
    传入 pageNo/pageSize 会被忽略、每页返回同样的内容。
    因此这里改成"拉一次 + 内容指纹判重"，最多再尝试 max_pages 次，
    出现重复就立即停止 —— 既不会死循环，也不会漏掉真有分页的实现。
    """
    warnings: list[str] = []
    collected: list[Any] = []
    seen_fingerprint: set[str] = set()

    for page in range(1, max_pages + 1):
        try:
            call = client.call_tool("order-list", {"pageNo": page, "pageSize": page_size})
        except MCPError as exc:
            warnings.append(f"order-list 第 {page} 页失败：{exc}")
            break
        if call.is_error:
            warnings.append(f"order-list 返回业务错误：{call.text[:160]}")
            break

        rows = rows_of(call.raw)
        if not rows:
            break

        ids = [str(r.get("orderId") or r.get("orderNo") or "") for r in rows if isinstance(r, dict)]
        fingerprint = "|".join(ids)
        if fingerprint in seen_fingerprint:
            break  # 服务端忽略了分页参数，已在重复返回
        seen_fingerprint.add(fingerprint)
        collected.extend(rows)

        # 返回条数少于请求条数 -> 已经到底，不必再请求下一页。
        # 真实联调结论：麦当劳 order-list 忽略分页参数并一次性返回全部，
        # 这个判断能让常规账号只发 1 次请求。
        if len(rows) < page_size:
            break

    # 按 orderId 去重，保持顺序
    deduped: list[Any] = []
    seen: set[str] = set()
    for row in collected:
        if not isinstance(row, dict):
            continue
        key = str(row.get("orderId") or row.get("orderNo") or id(row))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped, warnings


def _enrich_orders(
    client: ToolCaller, orders: list[Any], limit: int, warnings: list[str]
) -> int:
    """逐单补全详情。

    ⚠️ 这是本项目最关键的一步取数逻辑。

    order-list **只返回 realTotalAmount（实付）**，不返回 totalAmount（原价）
    与 totalDiscountAmount（优惠）。如果不补全，每笔订单的 gross 就等于 paid、
    省下金额恒为 0 —— **整个优惠审计引擎会拿不到任何数据、静默失效**。

    query-order 才返回 totalAmount / totalDiscountAmount / couponList /
    带单价的 orderProductList。因此必须逐单拉一次详情并合并。

    合并策略：以详情为主体，从列表项补齐详情里没有的字段
    （orderType / beType / storeCode，详情里不带渠道信息）。

    上限按**尝试次数**计，而不是成功次数 —— 否则接口持续失败时
    （例如全局限流、订单号失效），enriched 永远为 0，上限就形同虚设，
    会对成千上万笔订单逐条发起请求。
    """
    enriched = 0
    attempted = 0
    for order in orders:
        if attempted >= limit:
            remaining = len(orders) - attempted
            warnings.append(
                f"订单补全已达上限 {limit} 单，剩余 {remaining} 单未拉取详情，"
                "相关订单的原价与优惠金额缺失，优惠审计可能不完整"
            )
            break
        if not isinstance(order, dict):
            continue
        order_id = order.get("orderId") or order.get("orderNo")
        if not order_id:
            continue
        attempted += 1
        try:
            call = client.call_tool("query-order", {"orderId": str(order_id)})
        except MCPError as exc:
            warnings.append(f"query-order({str(order_id)[:16]}…) 失败：{exc}")
            continue
        detail = unwrap(call.raw)
        if not isinstance(detail, dict) or call.is_error:
            continue

        # 详情的餐品行更完整（带单价），优先用详情；列表项作为兜底
        if not detail.get("orderProductList"):
            detail["orderProductList"] = order.get("orderProductList") or []
        # 补齐详情里缺失、但列表里有的字段
        for key in ("orderType", "beType", "beCode", "storeCode", "storeName", "orderStatus"):
            if detail.get(key) in (None, "") and order.get(key) not in (None, ""):
                detail[key] = order[key]
        if not detail.get("createTime") and order.get("createTime"):
            detail["createTime"] = order["createTime"]

        order.clear()
        order.update(detail)
        enriched += 1
    return enriched


def _dominant_channel_code(orders: list[Any]) -> tuple[int, int]:
    """从历史订单里推断主要渠道编码，供 query-meals / query-store-coupons 使用。"""
    counts: dict[tuple[int, int], int] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        try:
            be_type = int(str(order.get("beType") or DEFAULT_BE_TYPE))
            order_type = int(str(order.get("orderType") or DEFAULT_ORDER_TYPE))
        except (TypeError, ValueError):
            continue
        counts[(order_type, be_type)] = counts.get((order_type, be_type), 0) + 1
    if not counts:
        return DEFAULT_ORDER_TYPE, DEFAULT_BE_TYPE
    return max(counts, key=lambda k: counts[k])


def fetch_bundle(
    client: MCPToolClient,
    *,
    order_pages: int = 3,
    page_size: int = 200,
    store_code: str | None = None,
    include_campaigns: bool = True,
    bind_coupons: bool = False,
    enrich: bool = True,
    enrich_limit: int = DEFAULT_ENRICH_LIMIT,
) -> MCPBundle:
    """按「订单 → 补全详情 → 券 → 菜单 → 积分 → 活动」的顺序拉取全部数据。

    bind_coupons 默认关闭 —— auto-bind-coupons 会真实写入用户账户，
    属于有副作用操作，必须由使用者显式开启。
    """
    bundle = MCPBundle()

    try:
        bundle.tools_available = client.list_tools()
    except MCPError as exc:
        bundle.warnings.append(f"tools/list 失败：{exc}")

    # 1) 历史订单
    orders, order_warnings = _fetch_orders(client, page_size, order_pages)
    bundle.warnings.extend(order_warnings)
    bundle.orders_raw = orders

    # 2) 逐单补全详情（拿到原价 / 优惠金额 / 券 / 单价 —— 审计引擎的数据来源）
    if orders and enrich:
        bundle.enriched = _enrich_orders(client, orders, enrich_limit, bundle.warnings)

    # 3) 券池
    if bind_coupons:
        try:
            client.call_tool("auto-bind-coupons", {})
        except MCPError as exc:
            bundle.warnings.append(f"auto-bind-coupons 失败：{exc}")

    order_type_code, be_type_code = _dominant_channel_code(orders)
    effective_store = store_code or next(
        (str(o.get("storeCode")) for o in orders if isinstance(o, dict) and o.get("storeCode")),
        None,
    )

    for tool in COUPON_SOURCES:
        args: dict[str, Any] = {}
        if tool == "query-my-coupons":
            args = {"page": "1", "pageSize": "200"}
        elif tool == "query-store-coupons":
            # 官方 schema 要求这三个参数全部必填，缺一个就返回 400
            args = {"storeCode": effective_store, "orderType": order_type_code, "beType": be_type_code}
            if not effective_store:
                bundle.warnings.append("缺少门店编码，跳过门店券查询")
                continue
        try:
            call = client.call_tool(tool, args)
            bundle.coupons_raw.append({"source": COUPON_SOURCES[tool], "payload": call.raw})
        except MCPError as exc:
            bundle.warnings.append(f"{tool} 失败：{exc}")

    # 4) 菜单（同样需要 storeCode + orderType + beType）
    if effective_store:
        try:
            bundle.meals_raw = client.call_tool(
                "query-meals",
                {"storeCode": effective_store, "orderType": order_type_code, "beType": be_type_code},
            ).raw
        except MCPError as exc:
            bundle.warnings.append(f"query-meals 失败：{exc}")
    else:
        bundle.warnings.append("缺少门店编码，跳过菜单查询")

    # 5) 积分账户
    try:
        bundle.account_raw = client.call_tool("query-my-account", {}).raw
    except MCPError as exc:
        bundle.warnings.append(f"query-my-account 失败：{exc}")

    # 6) 活动日历
    if include_campaigns:
        try:
            bundle.campaigns_raw = client.call_tool("campaign-calendar", {}).raw
        except MCPError as exc:
            bundle.warnings.append(f"campaign-calendar 失败：{exc}")

    return bundle


def _event_title(event: dict[str, Any]) -> str:
    """抽取活动标题。

    ⚠️ 真实联调结论：`activityTitle` 在真实数据里**恒为空字符串**，
    真正的标题藏在 `articleDto.title`（或 `articleDto.highlights`）里。
    只读 `activityTitle` 会让整个活动板块静默变成空白。
    """
    article = event.get("articleDto")
    if not isinstance(article, dict):
        article = {}
    for candidate in (
        event.get("activityTitle"),
        article.get("title"),
        article.get("highlights"),
        event.get("activitySubTitle"),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def parse_campaign_titles(payload: Any, limit: int = 8) -> list[str]:
    """从 campaign-calendar 里抽出活动标题。

    真实结构：data.dailyList[].events[].articleDto.title，
    每个日期节点带 `today` 标记。优先取 today 那一天的活动，
    取不到再退回全部日期。
    """
    node = unwrap(payload)
    if not isinstance(node, dict):
        return []

    days = [d for d in (node.get("dailyList") or []) if isinstance(d, dict)]
    # 有明确的 "今天" 节点时，只看今天 —— 混入「往期回顾」会误导用户
    today_days = [d for d in days if d.get("today") is True]
    ordered = today_days if today_days else days

    titles: list[str] = []
    for day in ordered:
        for event in day.get("events") or []:
            if not isinstance(event, dict):
                continue
            title = _event_title(event)
            if title and title not in titles:
                titles.append(title)
            if len(titles) >= limit:
                return titles
    return titles
