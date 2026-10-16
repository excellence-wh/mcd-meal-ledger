"""活动日历导出 —— 把「最近有哪些活动」变成可以导入手机日历的 .ics 文件。

一份标题列表看完就忘了。导出成日历之后，活动会在该提醒的时候提醒你 ——
这是「看完就忘」和「真的会用上」的区别。

实现上严格按 RFC 5545：

- 特殊字符要转义（``\\`` ``;`` ``,`` 换行）；
- 一行超过 75 个**字节**就要折行（中文一个字 3 字节，按字符数折会超限）；
- 需要 ``UID``，用日期 + 标题的哈希生成，保证同一活动反复导出是同一个 UID
  （重复导入时日历软件会合并，而不是堆出一堆重复项）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

# 一行最多 75 个八位组（字节），超出用 CRLF + 空格折行
_MAX_OCTETS = 75


def _escape(text: str) -> str:
    """转义 iCalendar 的文本值。"""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """按字节折行。中文字符占 3 字节，必须按字节算，不能按字符数算。"""
    encoded = line.encode("utf-8")
    if len(encoded) <= _MAX_OCTETS:
        return line

    chunks: list[bytes] = []
    current = b""
    for char in line:
        piece = char.encode("utf-8")
        # 折行后每行前面要多一个空格，所以续行的可用空间少 1 字节
        limit = _MAX_OCTETS if not chunks else _MAX_OCTETS - 1
        if len(current) + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


@dataclass(frozen=True)
class CalendarEvent:
    """要写进日历的一条活动。"""

    date: str  # YYYY-MM-DD
    title: str
    note: str = ""


def _uid(event: CalendarEvent, domain: str) -> str:
    """生成稳定的 UID。

    用日期 + 标题的摘要，保证同一活动反复导出得到同一个 UID ——
    这样重复导入日历时，日历软件会合并而不是堆出一堆重复项。
    这里只求唯一与稳定，不涉及任何安全用途，所以用 sha1 足够。
    """
    digest = hashlib.sha1(f"{event.date}|{event.title}".encode()).hexdigest()[:16]
    return f"{digest}@{domain}"


def build_ics(
    events: Iterable[CalendarEvent],
    *,
    calendar_name: str = "麦麦活动",
    stamp: datetime | None = None,
    domain: str = "mcd-meal-ledger",
) -> str:
    """生成一份可以在手机/电脑日历里直接导入的 .ics 文本。

    日期用 ``DTSTART;VALUE=DATE``（整天事件）—— 活动是「这一天上架」，
    不是「这一天的某个时刻」，给错误的开始时间反而会让用户误事。
    """
    stamp_time = (stamp or datetime.now()).strftime("%Y%m%dT%H%M%SZ")
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//mcd-meal-ledger//McDonalds Meal Ledger//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]

    for event in events:
        compact_date = event.date.replace("-", "")
        if len(compact_date) != 8 or not compact_date.isdigit():
            continue  # 日期不合法就跳过，不塞一条错的进用户日历
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_uid(event, domain)}",
            f"DTSTAMP:{stamp_time}",
            f"DTSTART;VALUE=DATE:{compact_date}",
            f"SUMMARY:{_escape(event.title)}",
        ]
        if event.note:
            lines.append(f"DESCRIPTION:{_escape(event.note)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    # RFC 5545 要求 CRLF 行尾，且最后一行后面也要有
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def write_ics(text: str, output: str) -> str:
    """写盘。显式指定 newline=""，否则 Windows 会把 CRLF 又转一遍。"""
    with open(output, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return output
