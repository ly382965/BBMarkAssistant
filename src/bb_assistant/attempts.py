"""Select the latest submission without depending on incoming list order."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any


_USTC_TIMEZONE = timezone(timedelta(hours=8))
_DATE = re.compile(r"(\d{4}|\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(.*)\Z")
_BB_ID = re.compile(r"(?:_(\d+)_\d+|(\d+))\Z", re.ASCII)


def _submitted_time(value: Any) -> datetime | None:
    """BB's two-digit year is a 2000s year; undated timezone means USTC time."""
    if not isinstance(value, str):
        return None
    match = _DATE.fullmatch(value.strip())
    if not match:
        return None
    year, month, day, tail = match.groups()
    year_number = int(year) + (2000 if len(year) == 2 else 0)
    tail = tail.strip().replace("时", ":").replace("分", ":").removesuffix("秒").rstrip(":")
    if tail:
        tail = "T" + tail.removeprefix("T").strip()
        # Blackboard may omit the leading zero on clock components, too.
        tail = re.sub(r"^T(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?",
                      lambda m: f"T{int(m[1]):02}:{int(m[2]):02}" + (f":{int(m[3]):02}" if m[3] else ""),
                      tail)
    try:
        parsed = datetime.fromisoformat(f"{year_number:04}-{int(month):02}-{int(day):02}{tail}")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_USTC_TIMEZONE)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _numeric_id(value: Any) -> int | None:
    match = _BB_ID.fullmatch(str(value).strip())
    if not match:
        return None
    try:
        return int(match[1] or match[2])
    except ValueError:
        return None


def latest_attempts(rows: list[dict]) -> list[dict]:
    """Return one original row per (assignment, student), preserving all statuses.

    Parsed times take priority when every record has a usable timestamp. Equal
    timestamps, or any missing timestamp, use Blackboard's increasing numeric
    attempt IDs. Opaque IDs cannot resolve a tie: reject that ambiguous group
    rather than silently choose an older attempt. Input rows are never changed.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = str(row["assignment_id"]), str(row["student_id"]).strip().upper()
        groups.setdefault(key, []).append(row)
    selected = []
    for key, group in sorted(groups.items()):
        candidates = group
        if len(group) > 1:
            times = [_submitted_time(row.get("submitted_at")) for row in group]
            if all(value is not None for value in times):
                latest = max(times)
                candidates = [row for row, value in zip(group, times) if value == latest]
        if len(candidates) > 1:
            numbers = [_numeric_id(row.get("id")) for row in candidates]
            if all(value is not None for value in numbers):
                newest = max(numbers)
                candidates = [row for row, value in zip(candidates, numbers) if value == newest]
        if len({str(row["id"]) for row in candidates}) != 1:
            raise ValueError(f"{key[1]}：无法确定最新提交；请先重新同步 BB 的提交时间，再处理或上传。")
        selected.append(candidates[0])
    return selected
