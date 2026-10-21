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


_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def aired_on(when: datetime) -> str:
    """UTC calendar date as 'Oct 14'."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    utc = when.astimezone(timezone.utc)
    return f"{_MONTHS[utc.month - 1]} {utc.day}"


def season_list(numbers: list[int] | tuple[int, ...]) -> str:
    if not numbers:
        return "none"
    return ", ".join(str(number) for number in sorted(numbers))


def season_phrase(numbers: list[int] | tuple[int, ...]) -> str:
    """'season 2' or 'season 2 and season 3'."""
    ordered = sorted({int(number) for number in numbers})
    if not ordered:
        return "no seasons"
    labels = [f"season {number}" for number in ordered]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + ", and " + labels[-1]


def _join(parts: list[str]) -> str:
    kept = [part for part in parts if part]
    if not kept:
        return ""
    if len(kept) == 1:
        return kept[0]
    return ", ".join(kept[:-1]) + ", and " + kept[-1]


def _shows(count: int) -> str:
    return "1 show" if count == 1 else f"{count} shows"


def _seasons(count: int) -> str:
    return "1 season" if count == 1 else f"{count} seasons"


def _new_seasons(count: int) -> str:
    return "1 new season" if count == 1 else f"{count} new seasons"


def deleted_line(title: str, seasons: list[int] | tuple[int, ...], nbytes: int, *, approved: bool) -> str:
    verb = "Approved deletion of" if approved else "Deleted"
    return f"{verb} {season_phrase(seasons)} of {title}, freeing {format_bytes(nbytes)}."


def restored_line(title: str, seasons: list[int] | tuple[int, ...], reason: str) -> str:
    lowered = reason.lower()
    if reason.startswith("You asked"):
        because = "you asked for them"
    elif "playing" in lowered or "played" in lowered or "watch" in lowered:
        because = "someone watched"
    else:
        because = "they were missing"
    return f"Restored {season_phrase(seasons)} of {title} because {because}."


def approval_line(title: str, seasons: list[int] | tuple[int, ...], nbytes: int) -> str:
    return (
        f"Approval requested for {title}: {season_phrase(seasons)}, {format_bytes(nbytes)}. "
        "Nothing was deleted."
    )


def watchlist_line(who: str, title: str, what: str) -> str:
    person = who.strip() or "Someone"
    return f"{person} added {title} to a Plex watchlist. {what}"


def dismissed_line(title: str) -> str:
    return f"Dismissed the deletion request for {title}. The files stay on disk."


def queue_line(phase: str, label: str, title: str, error: str | None = None) -> str:
    """Activity sentence for a queued task. phase is queued, started, finished, or failed."""
    if label in {"Run now", "Refresh preview"}:
        sentences = {
            "queued": "Queued a library refresh.",
            "started": "Started a library refresh.",
            "finished": "Finished a library refresh.",
            "failed": "Failed a library refresh.",
        }
        text = sentences.get(phase, f"{phase} a library refresh.")
        if error and phase == "failed":
            return f"{text} {error}"
        return text
    verb = {"queued": "Queued", "started": "Started", "finished": "Finished", "failed": "Failed"}.get(
        phase, phase.capitalize()
    )
    text = f"{verb} {label} for {title}."
    if error and phase == "failed":
        return f"{text} {error}"
    return text


def cycle_summary(
    *,
    dry_run: bool,
    prune: int,
    hold: int,
    restore: int,
    skip: int,
    pending: int,
    protected: int,
    searches: int,
    restored_seasons: int,
    freed_bytes: int,
    pending_bytes: int,
    queued: int = 0,
    queued_bytes: int = 0,
) -> str:
    """One sentence a person can read in the container log."""
    parts: list[str] = []
    if dry_run:
        if prune:
            parts.append(f"{_shows(prune)} would lose later seasons")
        if restore:
            parts.append(f"{_shows(restore)} would be restored")
        if hold:
            parts.append(f"{_shows(hold)} {'is' if hold == 1 else 'are'} held")
        if skip:
            parts.append(f"{_shows(skip)} left alone")
        if protected:
            parts.append(f"{_new_seasons(protected)} protected")
        body = _join(parts) or "nothing to change"
        return f"Preview finished: {body}. Nothing was deleted."

    if freed_bytes:
        parts.append(f"freed {format_bytes(freed_bytes)}")
    if queued:
        parts.append(f"{_shows(queued)} queued for deletion ({format_bytes(queued_bytes)})")
    if pending:
        parts.append(f"{_shows(pending)} waiting for approval ({format_bytes(pending_bytes)})")
    if restored_seasons:
        search_bit = ""
        if searches:
            search_bit = f" with {searches} search" if searches == 1 else f" with {searches} searches"
        parts.append(f"restored {_seasons(restored_seasons)}{search_bit}")
    elif searches:
        parts.append(f"{searches} search" if searches == 1 else f"{searches} searches")
    if hold:
        parts.append(f"{_shows(hold)} held")
    if skip:
        parts.append(f"{_shows(skip)} left alone")
    if protected:
        parts.append(f"{_new_seasons(protected)} protected")
    body = _join(parts) or "nothing to change"
    tail = "" if freed_bytes else " Nothing was deleted."
    return f"Live run finished: {body}.{tail}"
