"""Priority, delete rate limits, and the backlog estimate.

Restores, watchlist adds, and searches are priority 0. Ordinary tasks are 1.
Deletes are 2, so a new restore is claimed before every pending delete.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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

BLOCK_NONE = "none"
BLOCK_HOURLY_SHOWS = "hourly show cap"
BLOCK_HOURLY_GB = "hourly GB cap"
BLOCK_DAILY_TB = "daily TB cap"
BLOCK_COOLDOWN = "cooldown between deletes"
BLOCK_PAUSED = "deletes paused"
BLOCK_RESTORES = "waiting on re-adds/searches"
BLOCK_RETRY = "Sonarr/Radarr retrying"
BLOCK_APPROVAL = "awaiting approval"
CAP_BLOCKS = frozenset(
    {BLOCK_HOURLY_SHOWS, BLOCK_HOURLY_GB, BLOCK_DAILY_TB, BLOCK_COOLDOWN}
)
BLOCK_LOG_INTERVAL_SECONDS = 180
_BLOCK_RANK = {
    BLOCK_COOLDOWN: 0,
    BLOCK_DAILY_TB: 1,
    BLOCK_HOURLY_SHOWS: 2,
    BLOCK_HOURLY_GB: 3,
}


def deletion_allowed(
    usage: dict,
    limits: dict,
    now: datetime,
    pending_bytes: int = 0,
) -> tuple[bool, str]:
    """True when another series delete may start.

    pending_bytes is the next job. A job that would push a window over its
    cap waits, unless that window is empty and this one job is larger than
    the cap by itself.
    """
    last = _as_datetime(usage.get("last_at"))
    gap = int(limits["min_interval_seconds"])
    if last is not None and gap > 0:
        elapsed = (now - last).total_seconds()
        if elapsed < gap:
            return False, "Waiting for the pause between deletes."
    if int(usage.get("hour_series") or 0) >= int(limits["hourly_series"]):
        return False, "The hourly show limit has been reached."
    pending = max(0, int(pending_bytes or 0))
    hour_bytes = int(usage.get("hour_bytes") or 0)
    day_bytes = int(usage.get("day_bytes") or 0)
    hourly_cap = int(limits["hourly_bytes"])
    daily_cap = int(limits["daily_bytes"])
    hour_state = _byte_window(hour_bytes, hourly_cap, pending)
    day_state = _byte_window(day_bytes, daily_cap, pending)
    if hour_state == "reached":
        return False, "The hourly data limit has been reached."
    if hour_state == "would":
        return False, "The hourly data limit would be passed."
    if day_state == "reached":
        return False, "The daily data limit has been reached."
    if day_state == "would":
        return False, "The daily data limit would be passed."
    alone = []
    if hour_state == "alone":
        alone.append("hourly")
    if day_state == "alone":
        alone.append("daily")
    if alone:
        window = " and ".join(alone)
        return True, (
            f"This delete is larger than the {window} data cap, and nothing else "
            "was deleted in that window, so it will run alone."
        )
    return True, ""


def _byte_window(used: int, cap: int, pending: int) -> str | None:
    if cap <= 0:
        return None
    if used >= cap:
        return "reached"
    if pending and used + pending > cap:
        if used == 0 and pending > cap:
            return "alone"
        return "would"
    return None


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


def explain_caps(events: list[dict], limits: dict, now: datetime, last_at: object = None) -> dict:
    """Usage versus each cap, and the moment a delete is allowed again.

    events are ``{"at": datetime | str, "bytes": int}``. The binding reason is
    the cap that stays closed the longest. Ties prefer the hourly data cap.
    """
    parsed = []
    for event in events:
        moment = _as_datetime(event.get("at"))
        if moment is None:
            continue
        parsed.append({"at": moment, "bytes": int(event.get("bytes") or 0)})
    parsed.sort(key=lambda item: item["at"])
    hour_start = now - timedelta(hours=1)
    day_start = now - timedelta(days=1)
    hour_events = [item for item in parsed if item["at"] >= hour_start]
    day_events = [item for item in parsed if item["at"] >= day_start]
    hour_bytes = sum(item["bytes"] for item in hour_events)
    day_bytes = sum(item["bytes"] for item in day_events)
    hour_series = len(hour_events)
    hourly_series = int(limits["hourly_series"]) or 1
    hourly_bytes = int(limits["hourly_bytes"]) or 1
    daily_bytes = int(limits["daily_bytes"]) or 1
    gap = int(limits["min_interval_seconds"])
    latest = _as_datetime(last_at)
    if latest is None and parsed:
        latest = parsed[-1]["at"]

    ready = now
    waits: list[tuple[datetime, str]] = []
    if latest is not None and gap > 0 and (now - latest).total_seconds() < gap:
        moment = latest + timedelta(seconds=gap)
        waits.append((moment, BLOCK_COOLDOWN))
    series_ready = _ready_after_count(hour_events, hourly_series, timedelta(hours=1))
    if series_ready > now:
        waits.append((series_ready, BLOCK_HOURLY_SHOWS))
    bytes_ready = _ready_after_bytes(hour_events, hourly_bytes, timedelta(hours=1))
    if bytes_ready > now:
        waits.append((bytes_ready, BLOCK_HOURLY_GB))
    day_ready = _ready_after_bytes(day_events, daily_bytes, timedelta(days=1))
    if day_ready > now:
        waits.append((day_ready, BLOCK_DAILY_TB))
    reason = BLOCK_NONE
    if waits:
        waits.sort(key=lambda item: (item[0], _BLOCK_RANK.get(item[1], 0)))
        ready, reason = waits[-1]
    caps = [
        {
            "id": "hour_bytes",
            "used": hour_bytes,
            "limit": hourly_bytes,
            "label": f"Used {_amount(hour_bytes)} of {_amount(hourly_bytes)} this hour",
        },
        {
            "id": "hour_series",
            "used": hour_series,
            "limit": hourly_series,
            "label": f"Used {hour_series} of {hourly_series} shows this hour",
        },
        {
            "id": "day_bytes",
            "used": day_bytes,
            "limit": daily_bytes,
            "label": f"Used {_amount(day_bytes)} of {_amount(daily_bytes)} today",
        },
    ]
    return {"reason": reason, "next_delete_at": ready, "caps": caps}


def queue_block(
    *,
    events: list[dict],
    limits: dict,
    now: datetime,
    last_at: object = None,
    paused: bool = False,
    dry_run: bool = False,
    approval_required: bool = False,
    jobs: list[dict] | None = None,
    restores_ahead: bool = False,
) -> dict:
    """Why the delete backlog is not moving, plus the same reason on each job."""
    caps = explain_caps(events, limits, now, last_at)
    rows = list(jobs or [])
    queued_deletes = [
        job for job in rows if job.get("kind") == "delete-later" and job.get("status") == "queued"
    ]
    held = bool(paused or dry_run)
    approval = bool(
        approval_required
        and queued_deletes
        and all(str((job.get("payload") or {}).get("source") or "user") != "user" for job in queued_deletes)
    )
    retry_at = _next_retry(queued_deletes, now)
    reason = BLOCK_NONE
    next_at: datetime | None = None
    if queued_deletes and held:
        reason = BLOCK_PAUSED
    elif approval:
        reason = BLOCK_APPROVAL
    elif queued_deletes and restores_ahead:
        reason = BLOCK_RESTORES
    elif queued_deletes and (caps["reason"] != BLOCK_NONE or retry_at is not None):
        cap_at = caps["next_delete_at"] if caps["reason"] != BLOCK_NONE else None
        if cap_at is not None and (retry_at is None or cap_at >= retry_at):
            reason = caps["reason"]
            next_at = cap_at
        elif retry_at is not None:
            reason = BLOCK_RETRY
            next_at = retry_at
    for job in rows:
        job["waiting_on"] = _job_wait(job, reason, now)
    log = ""
    if reason in CAP_BLOCKS:
        label = _label_for(caps["caps"], reason)
        stamp = ""
        if next_at is not None:
            stamp = f" Next delete at {next_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}."
        log = f"Deletes are waiting on the {reason}. {label}.{stamp}"
    return {
        "block": reason,
        "next_delete_at": None if next_at is None else next_at.astimezone(timezone.utc).isoformat(),
        "caps": caps["caps"],
        "block_log": log,
        "jobs": rows,
    }


def restore_line(queued: int, in_progress: int) -> str:
    return f"Restored {queued} queued tasks ({in_progress} were in progress)."


def _job_wait(job: dict, reason: str, now: datetime) -> str:
    if job.get("status") != "queued":
        return BLOCK_NONE
    if _as_datetime(job.get("not_before")) and _as_datetime(job.get("not_before")) > now:
        if reason == BLOCK_RETRY or job.get("kind") != "delete-later":
            return BLOCK_RETRY
    if job.get("kind") == "delete-later" and reason != BLOCK_NONE:
        return reason
    return BLOCK_NONE


def _next_retry(jobs: list[dict], now: datetime) -> datetime | None:
    moments = []
    for job in jobs:
        moment = _as_datetime(job.get("not_before"))
        if moment is not None and moment > now:
            moments.append(moment)
    if not moments:
        return None
    return min(moments)


def _label_for(caps: list[dict], reason: str) -> str:
    key = {
        BLOCK_HOURLY_GB: "hour_bytes",
        BLOCK_HOURLY_SHOWS: "hour_series",
        BLOCK_DAILY_TB: "day_bytes",
    }.get(reason)
    for cap in caps:
        if cap["id"] == key:
            return str(cap["label"])
    if reason == BLOCK_COOLDOWN:
        return "Waiting out the pause between deletes"
    return reason


def _ready_after_count(events: list[dict], limit: int, window) -> datetime:
    if len(events) < limit or limit <= 0:
        return datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(events, key=lambda item: item["at"])
    excess = len(ordered) - limit + 1
    return ordered[excess - 1]["at"] + window


def _ready_after_bytes(events: list[dict], limit: int, window) -> datetime:
    ordered = sorted(events, key=lambda item: item["at"])
    total = sum(item["bytes"] for item in ordered)
    if total < limit or limit <= 0:
        return datetime.min.replace(tzinfo=timezone.utc)
    dropped = 0
    for event in ordered:
        dropped += event["bytes"]
        if total - dropped < limit:
            return event["at"] + window
    return ordered[-1]["at"] + window


def _amount(num: int) -> str:
    return format_bytes(int(num)).replace(".0 ", " ")


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
