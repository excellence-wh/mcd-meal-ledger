#!/usr/bin/env python
"""麦门账本 —— 一键生成你的麦当劳消费年报。

用法::

    # 离线模式（不需要 Token，用内置脱敏样例，适合先看效果）
    python scripts/run.py --offline

    # 在线模式（真实调用麦当劳 MCP）
    export MCD_MCP_TOKEN="你的Token"
    python scripts/run.py --out docs/mine.html --markdown

Windows PowerShell::

    $env:MCD_MCP_TOKEN="你的Token"
    python scripts/run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcledger.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
