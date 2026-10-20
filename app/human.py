"""Small formatters for logs and the status page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def format_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


def ago(when: datetime, now: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = now - when
    if delta < timedelta(0):
        return "just now"
    if delta.days >= 1:
        days = delta.days
        unit = "day" if days == 1 else "days"
        return f"{days} {unit} ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    minutes = max(delta.seconds // 60, 1)
    unit = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {unit} ago"


def season_list(numbers: list[int] | tuple[int, ...]) -> str:
    if not numbers:
        return "none"
    return ", ".join(str(number) for number in sorted(numbers))
