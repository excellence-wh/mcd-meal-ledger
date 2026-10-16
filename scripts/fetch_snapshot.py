#!/usr/bin/env python
"""从真实麦当劳 MCP 拉取一份数据快照，落盘到 fixtures/live/。

用途：
  1. 真实联调时确认 MCP 返回的真实字段结构，据此校准 models.py 的别名表；
  2. 留档 —— 便于在 MCP_INTEGRATION.md 里说明"实际用到哪些 Tool、返回什么"；
  3. 联调后可以把脱敏后的快照作为新的测试夹具。

安全：产出文件写进 fixtures/live/，该目录已加入 .gitignore，
      **不会**被提交进仓库（避免真实订单数据外泄）。

用法::

    export MCD_MCP_TOKEN="你的Token"
    python scripts/fetch_snapshot.py --out fixtures/live --pages 3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcledger.mcp_client import MCPAuthError, MCPClient, MCPError, fetch_bundle  # noqa: E402


def _summarize(payload: object, name: str) -> None:
    """打印返回结构的顶层形状，方便肉眼校准字段名。"""
    rows = payload
    if isinstance(rows, dict):
        for key in ("data", "list", "records", "orders"):
            if isinstance(rows.get(key), list):
                rows = rows[key]
                break
    if isinstance(rows, list) and rows:
        first = rows[0]
        keys = list(first.keys()) if isinstance(first, dict) else [f"<{type(first).__name__}>"]
        print(f"   {name}: {len(rows)} 条，字段 = {keys}")
        items = first.get("items") if isinstance(first, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            print(f"      items[0] 字段 = {list(items[0].keys())}")
    elif isinstance(rows, dict):
        print(f"   {name}: dict，字段 = {list(rows.keys())}")
    else:
        print(f"   {name}: 无数据")


def main() -> int:
    parser = argparse.ArgumentParser(description="拉取麦当劳 MCP 数据快照")
    parser.add_argument("--out", default="fixtures/live", help="快照输出目录")
    parser.add_argument("--pages", type=int, default=3, help="历史订单页数")
    parser.add_argument("--page-size", type=int, default=50, help="每页条数")
    parser.add_argument("--store", default="", help="门店编码")
    parser.add_argument("--bind-coupons", action="store_true", help="顺带调用一键领券（有副作用）")
    args = parser.parse_args()

    client = MCPClient(verbose=True)
    if not client.has_token:
        print("❌ 未检测到 MCD_MCP_TOKEN 环境变量。", file=sys.stderr)
        print("   请到 https://open.mcd.cn/mcp 手机号登录后在控制台领取。", file=sys.stderr)
        return 2

    print("→ 连接 mcp.mcd.cn ...")
    try:
        bundle = fetch_bundle(
            client,
            order_pages=args.pages,
            page_size=args.page_size,
            store_code=args.store or None,
            bind_coupons=args.bind_coupons,
        )
    except MCPAuthError as exc:
        print(f"❌ 鉴权失败：{exc}", file=sys.stderr)
        return 2
    except MCPError as exc:
        print(f"❌ 调用失败：{exc}", file=sys.stderr)
        return 3

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "tools_available": bundle.tools_available,
        "orders_raw": bundle.orders_raw,
        "coupons_raw": bundle.coupons_raw,
        "meals_raw": bundle.meals_raw,
        "account_raw": bundle.account_raw,
        "campaigns_raw": bundle.campaigns_raw,
        "warnings": bundle.warnings,
    }
    path = out_dir / "snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✅ 快照已保存：{path}")
    print(f"   可用 Tool 共 {len(bundle.tools_available)} 个")
    _summarize(bundle.orders_raw, "order-list")
    if isinstance(bundle.coupons_raw, list):
        for entry in bundle.coupons_raw:
            _summarize(entry.get("payload"), f"coupons[{entry.get('source')}]")
    _summarize(bundle.account_raw, "query-my-account")
    _summarize(bundle.campaigns_raw, "campaign-calendar")

    if bundle.warnings:
        print("\n⚠️  取数警告：")
        for warning in bundle.warnings:
            print(f"   - {warning}")

    print("\n提示：fixtures/live/ 已在 .gitignore 中，不会被提交。请勿手动提交真实订单数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
