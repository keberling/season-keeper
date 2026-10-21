"""Priority, delete rate limits, and the backlog estimate.

Restores, watchlist adds, and searches are priority 0. Ordinary tasks are 1.
Deletes are 2, so a new restore is claimed before every pending delete.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.human import format_bytes

PRIORITY_SOON = 0
PRIORITY_NORMAL = 1
PRIORITY_DELETE = 2

JOB_TIMEOUT_SECONDS = 300
JOB_MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 15

RESTORE_CANCEL = "A restore for this show was queued, so the delete was skipped."

GIB = 1024**3
TIB = 1024**4


def deletion_allowed(usage: dict, limits: dict, now: datetime) -> tuple[bool, str]:
    """True when another series delete may start."""
    last = _as_datetime(usage.get("last_at"))
    gap = int(limits["min_interval_seconds"])
    if last is not None and gap > 0:
        elapsed = (now - last).total_seconds()
        if elapsed < gap:
            return False, "Waiting for the pause between deletes."
    if int(usage.get("hour_series") or 0) >= int(limits["hourly_series"]):
        return False, "The hourly show limit has been reached."
    if int(usage.get("hour_bytes") or 0) >= int(limits["hourly_bytes"]):
        return False, "The hourly data limit has been reached."
    if int(usage.get("day_bytes") or 0) >= int(limits["daily_bytes"]):
        return False, "The daily data limit has been reached."
    return True, ""


def delete_eta(count: int, nbytes: int, usage: dict, limits: dict, now: datetime) -> str | None:
    """Plain-English estimate for the deletes still waiting."""
    count = int(count)
    nbytes = int(nbytes)
    if count <= 0:
        return None
    gap = int(limits["min_interval_seconds"])
    last = _as_datetime(usage.get("last_at"))
    wait = 0.0
    if last is not None and gap > 0:
        wait = max(0.0, gap - (now - last).total_seconds())
    spacing = wait + max(0, count - 1) * gap
    hourly_series = int(limits["hourly_series"]) or 1
    hourly_bytes = int(limits["hourly_bytes"]) or 1
    daily_bytes = int(limits["daily_bytes"]) or 1
    seconds = max(
        spacing,
        (count / hourly_series) * 3600,
        (nbytes / hourly_bytes) * 3600,
        (nbytes / daily_bytes) * 86400,
    )
    if int(usage.get("hour_series") or 0) >= hourly_series or int(usage.get("hour_bytes") or 0) >= hourly_bytes:
        seconds += 3600
    if int(usage.get("day_bytes") or 0) >= daily_bytes:
        seconds += 86400
    noun = "delete" if count == 1 else "deletes"
    return f"{_span(seconds)} left for {count} {noun} ({format_bytes(nbytes)})."


def _span(seconds: float) -> str:
    if seconds < 50:
        return "Less than a minute"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"About {minutes} {unit}"
    hours = int(round(seconds / 3600))
    if hours < 36:
        unit = "hour" if hours == 1 else "hours"
        return f"About {hours} {unit}"
    days = max(1, int(round(seconds / 86400)))
    unit = "day" if days == 1 else "days"
    return f"About {days} {unit}"


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
