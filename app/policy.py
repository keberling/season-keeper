"""Decide what to do with one series.

Rules:
- Season numbers in keep_seasons (default: season 1) are never deleted.
- Season 0 (specials) is ignored. It is never monitored, searched, restored,
  deleted, counted, or kept. When a show is activated, season 0 is set
  unmonitored in Sonarr.
- A season 1 play, or a season 1 session, inside the watch window monitors
  every later season and searches the ones with missing aired episodes.
- Any other play inside the window leaves the files alone.
- No play inside the window, and a complete Plex history, unmonitors and
  deletes every season that is not kept.
- A season whose files were added inside the new-season grace period is
  held. It is not unmonitored or deleted until that period ends.
- A show Sonarr marks as continuing also keeps its current season (the
  highest non-special season that has aired) while that season is fresh.
  Fresh means the latest aired episode is inside the watch window, or the
  season is still inside the new-season grace, whichever lasts longer.
  That season stays monitored so new episodes can download. One sentence
  explains it. Ended shows are unchanged. Specials are never that season.
- A show added to a Plex watchlist inside the window is held, so a new
  watchlist entry is not pruned on the same day. A season 1 play still
  restores. The watchlist does not search later seasons by itself.
- A series that is not matched to Plex is left alone.
- A history scan that stops before the window is covered leaves every series
  alone. Plays with no show id do not. A title match can only hold a show.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from app.human import ago, aired_on, format_bytes, season_list, season_phrase
from app.inventory import season_disk_bytes
from app.models import Decision, PolicySettings, SeriesSnapshot, ShowWatch


def is_recent(when: datetime | None, now: datetime, window: timedelta) -> bool:
    if when is None:
        return False
    if when > now:
        return True
    return (now - when) <= window


def season_arrival(series: SeriesSnapshot, season_number: int) -> datetime | None:
    """Earliest episode-file dateAdded for a season, or the earliest air date."""
    episodes = [episode for episode in series.episodes if episode.season_number == season_number]
    added = [episode.file_added_at for episode in episodes if episode.file_added_at is not None]
    if added:
        return min(added)
    aired = [episode.air_date for episode in episodes if episode.air_date is not None]
    if aired:
        return min(aired)
    return None


def protected_arrivals(
    series: SeriesSnapshot,
    now: datetime,
    grace: timedelta,
    keep: frozenset[int],
) -> dict[int, datetime]:
    """Seasons that are too new to unmonitor or delete. Specials are never included."""
    numbers = {episode.season_number for episode in series.episodes} | {
        season.season_number for season in series.seasons
    }
    found: dict[int, datetime] = {}
    for number in numbers:
        if number == 0 or number in keep:
            continue
        arrived = season_arrival(series, number)
        if arrived is not None and is_recent(arrived, now, grace):
            found[number] = arrived
    return found


def _grace_reason(protected: dict[int, datetime], grace: timedelta) -> str:
    sentences = []
    for number, arrived in sorted(protected.items()):
        added = arrived.astimezone(timezone.utc).date().isoformat()
        until = (arrived + grace).astimezone(timezone.utc).date().isoformat()
        sentences.append(f"New season {number} added {added}, protected until {until}.")
    return " ".join(sentences)


def _episode_aired(episode, now: datetime) -> bool:
    if episode.season_number <= 0:
        return False
    if episode.air_date is not None:
        return episode.air_date <= now
    return bool(episode.aired)


def current_aired_season(series: SeriesSnapshot, now: datetime) -> int | None:
    """Highest non-special season that already has an aired episode."""
    aired = {
        episode.season_number
        for episode in series.episodes
        if _episode_aired(episode, now)
    }
    if not aired:
        return None
    return max(aired)


def latest_aired_at(series: SeriesSnapshot, season_number: int, now: datetime) -> datetime | None:
    dates = [
        episode.air_date
        for episode in series.episodes
        if episode.season_number == season_number
        and episode.air_date is not None
        and _episode_aired(episode, now)
    ]
    if not dates:
        return None
    return max(dates)


@dataclass(frozen=True)
class _AiringKeep:
    season: int
    reason: str
    monitor_episode_ids: tuple[int, ...]
    monitor_season: bool


def _airing_keep(
    series: SeriesSnapshot,
    settings: PolicySettings,
    now: datetime,
    keep: frozenset[int],
    protected: dict[int, datetime],
) -> _AiringKeep | None:
    """Season 1 plus the fresh current season of a show that is still continuing."""
    if not settings.keep_current_season:
        return None
    if (series.status or "").strip().lower() != "continuing":
        return None
    current = current_aired_season(series, now)
    if current is None or current in keep:
        return None
    last = latest_aired_at(series, current, now)
    by_air = last is not None and is_recent(last, now, settings.watch_window)
    by_grace = current in protected
    if not by_air and not by_grace:
        return None
    phrase = season_phrase(sorted(set(keep) | {current}))
    if by_air and last is not None:
        detail = f"last episode aired {aired_on(last)}"
    else:
        arrived = protected.get(current) or season_arrival(series, current)
        if arrived is None:
            detail = "current season"
        else:
            detail = f"added {arrived.astimezone(timezone.utc).date().isoformat()}"
    monitor_ids = tuple(
        episode.id
        for episode in series.episodes
        if episode.season_number == current and not episode.monitored
    )
    season_on = next(
        (season.monitored for season in series.seasons if season.season_number == current),
        not monitor_ids,
    )
    return _AiringKeep(
        season=current,
        reason=f"Still airing: keeping {phrase} ({detail}).",
        monitor_episode_ids=monitor_ids,
        monitor_season=not season_on or bool(monitor_ids) or not series.monitored,
    )


def managed_keep(keep: frozenset[int]) -> frozenset[int]:
    """Season 1 is always kept. Season 0 is specials and is never part of the keep set."""
    return (keep - frozenset({0})) | frozenset({1})


def _specials_ids(series: SeriesSnapshot) -> tuple[set[int], set[int]]:
    episode_ids = {episode.id for episode in series.episodes if episode.season_number == 0}
    file_ids = {
        episode.episode_file_id
        for episode in series.episodes
        if episode.season_number == 0 and episode.episode_file_id
    }
    return episode_ids, file_ids


def has_specials(series: SeriesSnapshot) -> bool:
    return any(season.season_number == 0 for season in series.seasons) or any(
        episode.season_number == 0 for episode in series.episodes
    )


def without_specials(series: SeriesSnapshot, decision: Decision) -> Decision:
    """Drop season 0 from a decision. A series update turns specials off instead."""
    episode_ids, file_ids = _specials_ids(series)
    season_monitored = decision.season_monitored
    unmonitor = [episode_id for episode_id in decision.unmonitor_episode_ids if episode_id not in episode_ids]
    if season_monitored is not None and has_specials(series):
        season_monitored = {
            number: monitored
            for number, monitored in season_monitored.items()
            if number != 0
        }
        season_monitored[0] = False
        for episode in series.episodes:
            if episode.season_number == 0 and episode.monitored and episode.id not in unmonitor:
                unmonitor.append(episode.id)
    elif season_monitored is not None:
        season_monitored = {
            number: monitored for number, monitored in season_monitored.items() if number != 0
        }
    bytes_to_free = decision.bytes_to_free
    if 0 in decision.delete_seasons:
        bytes_to_free = max(0, bytes_to_free - season_disk_bytes(series, 0))
    return replace(
        decision,
        season_monitored=season_monitored,
        monitor_episode_ids=tuple(
            episode_id for episode_id in decision.monitor_episode_ids if episode_id not in episode_ids
        ),
        unmonitor_episode_ids=tuple(dict.fromkeys(unmonitor)),
        delete_file_ids=tuple(file_id for file_id in decision.delete_file_ids if file_id not in file_ids),
        delete_seasons=tuple(season for season in decision.delete_seasons if season != 0),
        search_seasons=tuple(season for season in decision.search_seasons if season != 0),
        bytes_to_free=bytes_to_free,
    )


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
    keep = managed_keep(settings.keep_seasons)
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
    if is_recent(watch.watchlisted_at, now, window):
        return _watchlist_hold(watch, now)
    if not watch.history_reliable:
        return _quiet(
            "skip",
            "Plex watch history is incomplete, so later seasons were left in place.",
        )
    return _prune(series, settings, days, now=now, grace=settings.new_season_grace)


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


def _watchlist_hold(watch: ShowWatch, now: datetime) -> Decision:
    who = (watch.watchlisted_by or "Someone").strip() or "Someone"
    when = ago(watch.watchlisted_at, now) if watch.watchlisted_at is not None else "recently"
    return _quiet(
        "hold",
        f"{who} added this to a Plex watchlist {when}. "
        "The quiet-day clock restarted, so nothing was deleted.",
    )


def _hold(watch: ShowWatch, now: datetime) -> Decision:
    numbered = sorted(season for season in watch.active_seasons if season != 0)
    if numbered:
        who = f" ({', '.join(watch.active_users)})" if watch.active_users else ""
        playing = season_list(numbered)
        reason = f"Season {playing} is playing now{who}. Files were left in place."
    elif watch.active_seasons:
        who = f" ({', '.join(watch.active_users)})" if watch.active_users else ""
        reason = f"A special is playing now{who}. Files were left in place."
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
    season_map = {
        season.season_number: True
        for season in series.seasons
        if season.season_number != 0
    }
    monitor_ids: list[int] = []
    unmonitor_ids: list[int] = []
    search: list[int] = []
    missing_by_season: dict[int, int] = {}
    specials_on = False

    for episode in series.episodes:
        if episode.season_number == 0:
            if episode.monitored:
                specials_on = True
                unmonitor_ids.append(episode.id)
            continue
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
        if season.season_number != 0 and not season.monitored
    }
    if any(season.season_number == 0 and season.monitored for season in series.seasons):
        specials_on = True
    if has_specials(series):
        season_map[0] = False
    needs_put = (
        bool(season_change)
        or series_monitored is not None
        or monitor_new is not None
        or specials_on
    )

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
    later = [number for number in season_map if number != 0 and number not in keep]
    if later and (season_change or monitor_ids):
        parts.append(f"Monitor seasons {season_list(later)}.")
    if search:
        parts.append(f"Search seasons {season_list(search)} for missing aired episodes.")
    elif later:
        parts.append("No aired episodes are missing, so Sonarr was not asked to search.")

    return without_specials(
        series,
        Decision(
            action="restore",
            reason=" ".join(parts),
            season_monitored=season_map if needs_put else None,
            series_monitored=True if needs_put else None,
            monitor_new_items=monitor_new if needs_put else None,
            monitor_episode_ids=tuple(monitor_ids),
            unmonitor_episode_ids=tuple(unmonitor_ids),
            delete_file_ids=(),
            delete_seasons=(),
            search_seasons=tuple(search),
            bytes_to_free=0,
        ),
    )


def _prune(
    series: SeriesSnapshot,
    settings: PolicySettings,
    days: int,
    now: datetime | None = None,
    grace: timedelta | None = None,
) -> Decision:
    keep = managed_keep(settings.keep_seasons)
    protected = (
        protected_arrivals(series, now, grace, keep)
        if now is not None and grace is not None
        else {}
    )
    airing = _airing_keep(series, settings, now, keep, protected) if now is not None else None
    if airing is not None:
        keep = keep | frozenset({airing.season})
        protected.pop(airing.season, None)
    protected_files = {
        episode.episode_file_id
        for episode in series.episodes
        if episode.season_number in protected and episode.episode_file_id
    }
    season_map = {
        season.season_number: season.season_number in keep
        for season in series.seasons
        if season.season_number != 0 and season.season_number not in protected
    }
    if has_specials(series):
        season_map[0] = False
    unmonitor_ids = [
        episode.id
        for episode in series.episodes
        if episode.season_number != 0
        and episode.season_number not in keep
        and episode.season_number not in protected
        and episode.monitored
    ]
    delete_ids: list[int] = []
    seen_files: set[int] = set()
    delete_seasons: set[int] = set()
    special_files = _specials_ids(series)[1]
    for episode in series.episodes:
        if (
            episode.season_number == 0
            or episode.season_number in keep
            or episode.season_number in protected
            or not episode.episode_file_id
        ):
            continue
        if episode.episode_file_id in special_files or episode.episode_file_id in protected_files:
            continue
        delete_seasons.add(episode.season_number)
        if episode.episode_file_id not in seen_files:
            seen_files.add(episode.episode_file_id)
            delete_ids.append(episode.episode_file_id)

    bytes_to_free = sum(season_disk_bytes(series, number) for number in delete_seasons)
    kept_unmonitored = [
        season.season_number
        for season in series.seasons
        if season.season_number != 0 and season.season_number in keep and not season.monitored
    ]
    later_still_monitored = [
        season.season_number
        for season in series.seasons
        if season.season_number != 0
        and season.season_number not in keep
        and season.season_number not in protected
        and season.monitored
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
    other_kept_off = [
        number for number in kept_unmonitored if airing is None or number != airing.season
    ]
    if (
        not unmonitor_ids
        and not delete_ids
        and not later_still_monitored
        and not other_kept_off
        and (airing is not None or (protected and grace is not None))
    ):
        parts: list[str] = []
        if protected and grace is not None:
            parts.append(_grace_reason(protected, grace))
        if airing is not None:
            parts.append(airing.reason)
        return Decision(
            action="hold",
            reason=" ".join(parts),
            season_monitored=None,
            series_monitored=None,
            monitor_new_items=None,
            monitor_episode_ids=airing.monitor_episode_ids if airing is not None else (),
            unmonitor_episode_ids=(),
            delete_file_ids=(),
            delete_seasons=(),
            search_seasons=(),
            bytes_to_free=0,
            protected_seasons=tuple(sorted(protected)),
            kept_seasons=(airing.season,) if airing is not None else (),
            ensure_monitored_seasons=(airing.season,) if airing is not None and airing.monitor_season else (),
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
                if episode.season_number != 0
                and episode.season_number not in keep
                and episode.season_number not in protected
                and episode.monitored
            }
        )
        parts.append(f"Unmonitor seasons {season_list(seasons)}.")
    if protected and grace is not None:
        parts.append(_grace_reason(protected, grace))
    if airing is not None:
        parts.append(airing.reason)
    else:
        parts.append(f"Season {season_list(sorted(keep))} stays.")

    if needs_put:
        for episode in series.episodes:
            if episode.season_number == 0 and episode.monitored and episode.id not in unmonitor_ids:
                unmonitor_ids.append(episode.id)
    return without_specials(
        series,
        Decision(
            action="prune",
            reason=" ".join(parts),
            season_monitored=season_map if needs_put else None,
            series_monitored=True if needs_put else None,
            monitor_new_items=monitor_new if needs_put else None,
            monitor_episode_ids=airing.monitor_episode_ids if airing is not None else (),
            unmonitor_episode_ids=tuple(unmonitor_ids),
            delete_file_ids=tuple(delete_ids),
            delete_seasons=tuple(sorted(delete_seasons)),
            search_seasons=(),
            bytes_to_free=bytes_to_free,
            protected_seasons=tuple(sorted(protected)),
            kept_seasons=(airing.season,) if airing is not None else (),
            ensure_monitored_seasons=(airing.season,) if airing is not None and airing.monitor_season else (),
        ),
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
    keep = managed_keep(settings.keep_seasons)
    return _restore(series, watch, now, settings, keep)
