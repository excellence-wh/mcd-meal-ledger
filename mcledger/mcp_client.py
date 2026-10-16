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

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

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
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text", "")))
        text = "\n".join(chunks).strip()
        if text:
            try:
                return json.loads(text), text, is_error
            except json.JSONDecodeError:
                return text, text, is_error

    return response, json.dumps(response, ensure_ascii=False), is_error


@dataclass
class MCPClient:
    """一个极简但完整的 MCP Streamable HTTP 客户端。"""

    endpoint: str = DEFAULT_ENDPOINT
    token: str = ""
    timeout: float = 30.0
    verbose: bool = False

    _session_id: str = field(default="", init=False, repr=False)
    _request_id: int = field(default=0, init=False, repr=False)
    _last_call_at: float = field(default=0.0, init=False, repr=False)
    _tool_names: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.token:
            self.token = os.environ.get(TOKEN_ENV, "").strip()

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
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:  # pragma: no cover - 尽力而为
                    pass
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
        """握手。Streamable HTTP 要求先 initialize，服务端会返回 session id。"""
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "mcd-meal-ledger", "version": "1.0.0"},
            },
        )
        # 部分实现要求显式发送 initialized 通知，失败不影响后续调用。
        try:
            self._rpc("notifications/initialized")
        except MCPError:
            pass
        return result if isinstance(result, dict) else {}

    def list_tools(self) -> list[str]:
        """列出服务端可用工具（首次调用会先握手）。"""
        if self._tool_names:
            return self._tool_names
        if not self._session_id:
            self.initialize()
        result = self._rpc("tools/list")
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        self._tool_names = [t.get("name", "") for t in tools if isinstance(t, dict)]
        return self._tool_names

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
        """调用一个工具，返回归一化后的结果。"""
        arguments = arguments or {}
        if not self._session_id:
            self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            return ToolCall(tool=name, arguments=arguments, raw=result, text=str(result))
        payload, text, is_error = _extract_payload(result)
        if self.verbose:
            print(f"[mcp] {name}({json.dumps(arguments, ensure_ascii=False)}) -> {len(text)} chars")
        return ToolCall(
            tool=name, arguments=arguments, raw=payload, text=text, is_error=is_error
        )


# --------------------------------------------------------------------------
# 业务层封装 —— 把工具名和参数收敛到一处，方便审计与文档
# --------------------------------------------------------------------------

# 券池的四个来源。少查一路，用户就少省钱 —— 这是本项目最重要的一条经验。
COUPON_SOURCES: dict[str, str] = {
    "available-coupons": "available",  # 麦麦省可领取的券
    "query-my-coupons": "my",  # 我账户里已有的券
    "query-store-coupons": "store",  # 当前门店专属券
}


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


def fetch_bundle(
    client: MCPClient,
    *,
    order_pages: int = 3,
    page_size: int = 50,
    store_code: str | None = None,
    include_campaigns: bool = True,
    bind_coupons: bool = False,
) -> MCPBundle:
    """按「定位 → 取菜单 → 取券 → 取订单 → 取活动」的顺序拉取全部数据。

    bind_coupons 默认关闭 —— auto-bind-coupons 会真实写入用户账户，
    属于有副作用操作，必须由使用者显式开启。
    """
    bundle = MCPBundle()

    try:
        bundle.tools_available = client.list_tools()
    except MCPError as exc:
        bundle.warnings.append(f"tools/list 失败：{exc}")

    # 1) 历史订单（多页拉取后由调用方去重）
    collected: list[Any] = []
    for page in range(1, order_pages + 1):
        try:
            call = client.call_tool("order-list", {"pageNo": page, "pageSize": page_size})
        except MCPError as exc:
            bundle.warnings.append(f"order-list 第 {page} 页失败：{exc}")
            break
        rows = call.raw
        if isinstance(rows, dict):
            for key in ("data", "list", "records", "orders"):
                if isinstance(rows.get(key), list):
                    rows = rows[key]
                    break
        if not isinstance(rows, list) or not rows:
            break
        collected.extend(rows)
        if len(rows) < page_size:
            break
    bundle.orders_raw = collected

    # 2) 券池：四路合一
    if bind_coupons:
        try:
            client.call_tool("auto-bind-coupons", {})
        except MCPError as exc:
            bundle.warnings.append(f"auto-bind-coupons 失败：{exc}")

    for tool in COUPON_SOURCES:
        args: dict[str, Any] = {}
        if tool == "query-store-coupons" and store_code:
            args["storeCode"] = store_code
        try:
            call = client.call_tool(tool, args)
            bundle.coupons_raw.append({"source": COUPON_SOURCES[tool], "payload": call.raw})
        except MCPError as exc:
            bundle.warnings.append(f"{tool} 失败：{exc}")

    # 3) 菜单
    try:
        bundle.meals_raw = client.call_tool(
            "query-meals", {"storeCode": store_code} if store_code else {}
        ).raw
    except MCPError as exc:
        bundle.warnings.append(f"query-meals 失败：{exc}")

    # 4) 积分账户
    try:
        bundle.account_raw = client.call_tool("query-my-account", {}).raw
    except MCPError as exc:
        bundle.warnings.append(f"query-my-account 失败：{exc}")

    # 5) 活动日历
    if include_campaigns:
        try:
            bundle.campaigns_raw = client.call_tool("campaign-calendar", {}).raw
        except MCPError as exc:
            bundle.warnings.append(f"campaign-calendar 失败：{exc}")

    return bundle
