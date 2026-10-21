"""Decide what to do with one series.

Rules:
- Season numbers in keep_seasons (default: season 1) are never deleted.
- A season 1 play, or a season 1 session, inside the watch window monitors
  every later season and searches the ones with missing aired episodes.
- Any other play inside the window leaves the files alone.
- No play inside the window, and a complete Plex history, unmonitors and
  deletes every season that is not kept.
- A series that is not matched to Plex is left alone.
- A history scan that stops before the window is covered leaves every series
  alone. Plays with no show id do not. A title match can only hold a show.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from app.human import ago, format_bytes, season_list
from app.inventory import season_disk_bytes
from app.models import Decision, PolicySettings, SeriesSnapshot, ShowWatch


def is_recent(when: datetime | None, now: datetime, window: timedelta) -> bool:
    if when is None:
        return False
    if when > now:
        return True
    return (now - when) <= window


def _desired_monitor_new(current: str | None, preference: str) -> str | None:
    if current is None or preference == "keep":
        return None
    if preference == current:
        return None
    return preference


def decide(
    series: SeriesSnapshot,
    watch: ShowWatch,
    now: datetime,
    settings: PolicySettings,
) -> Decision:
    window = settings.watch_window
    keep = settings.keep_seasons | frozenset({1})
    days = window.days

    if not watch.matched:
        tvdb = series.tvdb_id if series.tvdb_id is not None else "missing"
        return _quiet(
            "skip",
            f"No Plex match for TVDB {tvdb}. Nothing on this series will be deleted.",
        )

    season1_active = 1 in watch.active_seasons or is_recent(
        watch.season1_last_viewed_at, now, window
    )
    show_active = (
        bool(watch.active_seasons)
        or is_recent(watch.unknown_season_at, now, window)
        or is_recent(watch.last_viewed_at, now, window)
    )

    if season1_active:
        return _restore(series, watch, now, settings, keep)
    if show_active:
        return _hold(watch, now)
    if not watch.history_reliable:
        return _quiet(
            "skip",
            "Plex watch history is incomplete, so later seasons were left in place.",
        )
    return _prune(series, settings, days)


def _quiet(action: str, reason: str) -> Decision:
    return Decision(
        action=action,
        reason=reason,
        season_monitored=None,
        series_monitored=None,
        monitor_new_items=None,
        monitor_episode_ids=(),
        unmonitor_episode_ids=(),
        delete_file_ids=(),
        delete_seasons=(),
        search_seasons=(),
        bytes_to_free=0,
    )


def _latest_play_has_unknown_season(watch: ShowWatch) -> bool:
    if watch.unknown_season_at is None:
        return False
    if watch.last_viewed_at is None:
        return True
    return watch.unknown_season_at >= watch.last_viewed_at


def _hold(watch: ShowWatch, now: datetime) -> Decision:
    if watch.active_seasons:
        who = f" ({', '.join(watch.active_users)})" if watch.active_users else ""
        playing = season_list(sorted(watch.active_seasons))
        reason = f"Season {playing} is playing now{who}. Files were left in place."
    elif _latest_play_has_unknown_season(watch):
        when = watch.unknown_season_at or watch.last_viewed_at
        reason = (
            f"Last play was {ago(when, now)}, and the season was not known. "
            "Files were left in place."
        )
    elif watch.last_viewed_at is not None:
        reason = (
            f"Last play was {ago(watch.last_viewed_at, now)}, outside season 1. "
            "Files were left in place."
        )
    else:
        reason = "The show has recent watch activity. Files were left in place."
    return _quiet("hold", reason)


def _restore(
    series: SeriesSnapshot,
    watch: ShowWatch,
    now: datetime,
    settings: PolicySettings,
    keep: frozenset[int],
) -> Decision:
    season_map = {season.season_number: True for season in series.seasons}
    monitor_ids: list[int] = []
    search: list[int] = []
    missing_by_season: dict[int, int] = {}

    for episode in series.episodes:
        if not episode.monitored:
            monitor_ids.append(episode.id)
        if episode.season_number in keep or not episode.aired or episode.has_file:
            continue
        missing_by_season[episode.season_number] = (
            missing_by_season.get(episode.season_number, 0) + 1
        )

    search = sorted(missing_by_season)
    monitor_new = _desired_monitor_new(
        series.monitor_new_items, settings.monitor_new_when_active
    )
    series_monitored = True if not series.monitored else None
    season_change = {
        season.season_number: True
        for season in series.seasons
        if not season.monitored
    }
    needs_put = bool(season_change) or series_monitored is not None or monitor_new is not None

    if watch.match_source == "manual":
        lead = "You asked Sonarr to fetch the later seasons."
    elif watch.active_seasons and 1 in watch.active_seasons:
        who = f" ({', '.join(watch.active_users)})" if watch.active_users else ""
        lead = f"Season 1 is playing now{who}."
    elif watch.season1_last_viewed_at is not None:
        lead = f"Season 1 was played {ago(watch.season1_last_viewed_at, now)}."
    else:
        lead = "Season 1 has watch activity inside the window."

    if not needs_put and not monitor_ids and not search:
        return _quiet(
            "skip",
            f"{lead} Later seasons are already monitored and on disk.",
        )

    parts = [lead]
    later = [number for number in season_map if number not in keep]
    if later and (season_change or monitor_ids):
        parts.append(f"Monitor seasons {season_list(later)}.")
    if search:
        parts.append(f"Search seasons {season_list(search)} for missing aired episodes.")
    elif later:
        parts.append("No aired episodes are missing, so Sonarr was not asked to search.")

    return Decision(
        action="restore",
        reason=" ".join(parts),
        season_monitored=season_map if needs_put else None,
        series_monitored=True if needs_put else None,
        monitor_new_items=monitor_new if needs_put else None,
        monitor_episode_ids=tuple(monitor_ids),
        unmonitor_episode_ids=(),
        delete_file_ids=(),
        delete_seasons=(),
        search_seasons=tuple(search),
        bytes_to_free=0,
    )


def _prune(series: SeriesSnapshot, settings: PolicySettings, days: int) -> Decision:
    keep = settings.keep_seasons | frozenset({1})
    season_map = {
        season.season_number: season.season_number in keep for season in series.seasons
    }
    unmonitor_ids = [
        episode.id
        for episode in series.episodes
        if episode.season_number not in keep and episode.monitored
    ]
    delete_ids: list[int] = []
    seen_files: set[int] = set()
    delete_seasons: set[int] = set()
    for episode in series.episodes:
        if episode.season_number in keep or not episode.episode_file_id:
            continue
        delete_seasons.add(episode.season_number)
        if episode.episode_file_id not in seen_files:
            seen_files.add(episode.episode_file_id)
            delete_ids.append(episode.episode_file_id)

    bytes_to_free = sum(season_disk_bytes(series, number) for number in delete_seasons)
    kept_unmonitored = [
        season.season_number
        for season in series.seasons
        if season.season_number in keep and not season.monitored
    ]
    later_still_monitored = [
        season.season_number
        for season in series.seasons
        if season.season_number not in keep and season.monitored
    ]
    monitor_new = _desired_monitor_new(
        series.monitor_new_items, settings.monitor_new_when_idle
    )

    needs_put = (
        bool(later_still_monitored)
        or bool(kept_unmonitored)
        or not series.monitored
        or monitor_new is not None
    )
    if not needs_put and not unmonitor_ids and not delete_ids:
        return _quiet(
            "skip",
            f"No Plex plays in {days} days. Later seasons are already unmonitored and gone.",
        )

    parts = [f"No Plex plays in {days} days."]
    if delete_seasons:
        size = f" ({format_bytes(bytes_to_free)})" if bytes_to_free else ""
        file_word = "episode file" if len(delete_ids) == 1 else "episode files"
        parts.append(
            "Unmonitor and delete "
            f"{len(delete_ids)} {file_word} in seasons {season_list(sorted(delete_seasons))}{size}."
        )
    elif later_still_monitored or unmonitor_ids:
        seasons = later_still_monitored or sorted(
            {
                episode.season_number
                for episode in series.episodes
                if episode.season_number not in keep and episode.monitored
            }
        )
        parts.append(f"Unmonitor seasons {season_list(seasons)}.")
    parts.append(f"Season {season_list(sorted(keep))} stays.")

    return Decision(
        action="prune",
        reason=" ".join(parts),
        season_monitored=season_map if needs_put else None,
        series_monitored=True if needs_put else None,
        monitor_new_items=monitor_new if needs_put else None,
        monitor_episode_ids=(),
        unmonitor_episode_ids=tuple(unmonitor_ids),
        delete_file_ids=tuple(delete_ids),
        delete_seasons=tuple(sorted(delete_seasons)),
        search_seasons=(),
        bytes_to_free=bytes_to_free,
    )


def plan_manual_prune(series: SeriesSnapshot, settings: PolicySettings) -> Decision:
    """Delete later seasons of one series because the user asked for that show."""
    decision = _prune(series, settings, settings.watch_window.days)
    marker = f"No Plex plays in {settings.watch_window.days} days."
    if decision.reason.startswith(marker):
        decision = replace(
            decision,
            reason="You chose this show." + decision.reason[len(marker) :],
        )
    return decision


def plan_manual_restore(series: SeriesSnapshot, settings: PolicySettings, now: datetime) -> Decision:
    """Monitor later seasons and search them so Sonarr can download the files again."""
    watch = ShowWatch(
        matched=True,
        match_source="manual",
        last_viewed_at=now,
        season1_last_viewed_at=now,
        active_seasons=frozenset({1}),
        active_users=(),
        unknown_season_at=None,
        history_reliable=True,
    )
    keep = settings.keep_seasons | frozenset({1})
    return _restore(series, watch, now, settings, keep)
