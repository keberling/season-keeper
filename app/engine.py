"""Apply a decision to one series, with a last-line check before any delete."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from app.arrivals import resolve_arrivals
from app.human import approval_line, cycle_summary, deleted_line, restored_line
from app.inventory import build_inventory
from app.models import (
    Decision,
    PolicySettings,
    RunReport,
    SeriesResult,
    SeriesSnapshot,
    ShowActivity,
    ShowWatch,
    WatchIndex,
)
from app.policy import decide, managed_keep, without_specials
from app.progress import Progress, report
from app.sonarr import series_update_payload
from app.state import State

logger = logging.getLogger("trimarr")


class SafetyError(RuntimeError):
    pass


class SonarrActions(Protocol):
    def update_series(self, payload: dict) -> None: ...

    def monitor_episodes(self, episode_ids: list[int], monitored: bool) -> None: ...

    def delete_episode_files(self, episode_file_ids: list[int]) -> None: ...

    def search_season(self, series_id: int, season_number: int) -> None: ...


def watch_for(series: SeriesSnapshot, index: WatchIndex) -> ShowWatch:
    reliable = index.reliable_for_prune()
    empty = ShowWatch(
        matched=False,
        match_source=None,
        last_viewed_at=None,
        season1_last_viewed_at=None,
        active_seasons=frozenset(),
        active_users=(),
        unknown_season_at=None,
        history_reliable=reliable,
    )
    if series.tvdb_id is None or series.tvdb_id not in index.matched_tvdb:
        return empty
    activity = index.by_tvdb.get(series.tvdb_id, ShowActivity())
    return ShowWatch(
        matched=True,
        match_source=index.match_source.get(series.tvdb_id),
        last_viewed_at=activity.last_viewed_at,
        season1_last_viewed_at=activity.season1_last_viewed_at,
        active_seasons=frozenset(activity.active_seasons),
        active_users=tuple(sorted(activity.active_users)),
        unknown_season_at=activity.unknown_season_at,
        history_reliable=reliable,
        watchlisted_at=activity.watchlisted_at,
        watchlisted_by=activity.watchlisted_by,
        season_plays=tuple(sorted(activity.season_last_viewed.items())),
    )


def assert_safe(series: SeriesSnapshot, decision: Decision, keep: frozenset[int]) -> None:
    """Refuse deletes and unmonitors that touch a kept season, including season 1."""
    keep = managed_keep(keep)
    episodes = {episode.id: episode for episode in series.episodes}
    for episode_id in decision.unmonitor_episode_ids:
        episode = episodes.get(episode_id)
        if episode is None:
            raise SafetyError(f"Episode {episode_id} is not on {series.label}")
        if episode.season_number in keep:
            raise SafetyError(
                f"Refusing to unmonitor {series.label} season {episode.season_number}"
            )
    file_seasons: dict[int, set[int]] = {}
    for episode in series.episodes:
        if episode.episode_file_id:
            file_seasons.setdefault(episode.episode_file_id, set()).add(episode.season_number)
    for file_id in decision.delete_file_ids:
        seasons = file_seasons.get(file_id)
        if not seasons:
            raise SafetyError(f"File {file_id} is not on {series.label}")
        kept = seasons & keep
        if 0 in seasons:
            raise SafetyError(f"Refusing to delete specials on {series.label}")
        if kept:
            raise SafetyError(
                f"Refusing to delete file {file_id} on {series.label} because it is also season {sorted(kept)}"
            )
    for season in decision.search_seasons:
        if season == 0:
            raise SafetyError(f"Refusing to search specials of {series.label}")
        if season in keep:
            raise SafetyError(f"Refusing to search kept season {season} of {series.label}")


def _binge_wait_reason(reason: str, search_seasons: tuple[int, ...] | list[int], waiting: list[str]) -> str:
    """Say when the next season was not searched because Sonarr already has it or the cooldown is open."""
    prefix = ""
    body = reason
    if body.startswith("Preview only. "):
        prefix = "Preview only. "
        body = body[len(prefix) :]
    if not waiting or not body.startswith("Watched S"):
        return reason
    season = search_seasons[0] if search_seasons else None
    notes: list[str] = []
    for note in waiting:
        if season is not None and "cooldown" in note:
            notes.append(f"S{season} search on cooldown")
        elif season is not None and "queued" in note:
            notes.append(f"S{season} is already queued in Sonarr")
        else:
            notes.append(note)
    if any(note.startswith("S") for note in notes) and " and searched." in body:
        body = body.replace(" and searched.", ".")
    return prefix + body + " " + ". ".join(notes) + "."


def _note_binge(state: State, result: SeriesResult, now: datetime) -> None:
    """Record the binge sentence once. Repeating the same line every scan adds nothing."""
    if not result.in_live_scope or not result.reason.startswith("Watched S"):
        return
    if state.last_activity_message(result.sonarr_id) == result.reason:
        return
    logger.info("%s", result.reason)
    state.record_activity(
        at=now,
        kind="restored",
        sonarr_id=result.sonarr_id,
        title=result.title,
        message=result.reason,
        seasons=len(result.search_seasons),
    )


def _iso(when: datetime | None) -> str | None:
    if when is None:
        return None
    return when.astimezone(timezone.utc).isoformat()


def _apply_keep_monitoring(
    series: SeriesSnapshot,
    decision: Decision,
    actions: SonarrActions,
) -> bool:
    """Turn the fresh current season on without unmonitoring or deleting anything else."""
    seasons = [number for number in decision.ensure_monitored_seasons if number != 0]
    episode_ids = list(decision.monitor_episode_ids)
    season_on: dict[int, bool] = {}
    for number in seasons:
        current = next((season for season in series.seasons if season.season_number == number), None)
        if current is None or not current.monitored:
            season_on[number] = True
    wrote = False
    if season_on or (seasons and not series.monitored):
        actions.update_series(
            series_update_payload(
                series.raw,
                season_monitored=season_on,
                series_monitored=True,
                monitor_new_items=None,
            )
        )
        wrote = True
    if episode_ids:
        actions.monitor_episodes(episode_ids, True)
        wrote = True
    if wrote:
        label = ", ".join(str(number) for number in seasons) or "current"
        logger.info(
            "%s: season %s stays monitored so new episodes can download.",
            series.label,
            label,
        )
    return wrote


def apply_decision(
    series: SeriesSnapshot,
    decision: Decision,
    *,
    watch: ShowWatch | None,
    actions: SonarrActions,
    state: State,
    settings: PolicySettings,
    now: datetime,
    queue: set[tuple[int, int]],
    running_searches: set[tuple[int, int]],
    honor_cooldown: bool = True,
    announce_preview: bool = True,
    confirmed: bool = False,
    defer_deletes: bool = False,
) -> SeriesResult:
    decision = without_specials(series, decision)
    keep = managed_keep(settings.keep_seasons) | frozenset(decision.kept_seasons)
    rows, totals = build_inventory(series, keep, decision)
    reason = decision.reason
    searches_ran: list[int] = []
    waiting: list[str] = []
    error: str | None = None
    applied = False
    watch = watch or ShowWatch(
        matched=False,
        match_source=None,
        last_viewed_at=None,
        season1_last_viewed_at=None,
        active_seasons=frozenset(),
        active_users=(),
        unknown_season_at=None,
        history_reliable=True,
    )

    planned: list[int] = []
    for season in decision.search_seasons:
        if season == 0:
            continue
        if (series.id, season) in queue or (series.id, season) in running_searches:
            waiting.append(f"season {season} is already queued in Sonarr")
            continue
        if honor_cooldown and state.in_cooldown(series.id, season, now, settings.search_cooldown):
            waiting.append(f"season {season} search is on cooldown")
            continue
        planned.append(season)

    needs_approval = (
        decision.action == "prune"
        and not settings.dry_run
        and settings.require_delete_approval
    )
    queued_delete = False
    deferring = (
        defer_deletes
        and decision.action == "prune"
        and not settings.dry_run
        and not needs_approval
        and bool(decision.delete_file_ids)
    )
    if settings.dry_run and announce_preview and decision.action in {"prune", "restore"}:
        reason = "Preview only. " + reason
    if needs_approval:
        reason = "Approval required before any files are deleted. " + reason
    if deferring:
        reason = "Queued for deletion. One show is removed at a time. " + reason
    if decision.reason.startswith("Watched S"):
        reason = _binge_wait_reason(reason, decision.search_seasons, waiting)
    elif waiting:
        reason = reason + " Waiting: " + "; ".join(waiting) + "."

    try:
        assert_safe(series, decision, keep)
        if needs_approval:
            _apply_keep_monitoring(series, decision, actions)
        elif deferring:
            if _apply_keep_monitoring(series, decision, actions):
                applied = True
            queued_delete = True
        elif not settings.dry_run and decision.action in {"prune", "restore"}:
            if decision.season_monitored is not None:
                payload = series_update_payload(
                    series.raw,
                    season_monitored=decision.season_monitored,
                    series_monitored=(
                        True if decision.series_monitored is None else decision.series_monitored
                    ),
                    monitor_new_items=decision.monitor_new_items,
                )
                actions.update_series(payload)
            if decision.unmonitor_episode_ids:
                actions.monitor_episodes(list(decision.unmonitor_episode_ids), False)
            if decision.delete_file_ids:
                actions.delete_episode_files(list(decision.delete_file_ids))
            if decision.monitor_episode_ids:
                actions.monitor_episodes(list(decision.monitor_episode_ids), True)
            for season in planned:
                try:
                    actions.search_season(series.id, season)
                except Exception as exc:
                    waiting.append(f"season {season} search failed: {exc}")
                    continue
                state.mark_search(series.id, season, now)
                searches_ran.append(season)
            applied = True
            if decision.delete_file_ids:
                state.record_freed(
                    sonarr_id=series.id,
                    title=series.title,
                    file_count=len(decision.delete_file_ids),
                    nbytes=decision.bytes_to_free,
                    when=now,
                )
                message = deleted_line(
                    series.label,
                    decision.delete_seasons,
                    decision.bytes_to_free,
                    approved=confirmed,
                )
                logger.info("%s", message)
                state.record_activity(
                    at=now,
                    kind="approved" if confirmed else "deleted",
                    sonarr_id=series.id,
                    title=series.title,
                    message=message,
                    nbytes=decision.bytes_to_free,
                    seasons=len(decision.delete_seasons),
                )
            if searches_ran and not decision.reason.startswith("Watched S"):
                message = restored_line(series.label, searches_ran, decision.reason)
                logger.info("%s", message)
                state.record_activity(
                    at=now,
                    kind="restored",
                    sonarr_id=series.id,
                    title=series.title,
                    message=message,
                    seasons=len(searches_ran),
                )
            if waiting and decision.reason.startswith("Watched S"):
                reason = _binge_wait_reason(decision.reason, decision.search_seasons, waiting)
            elif waiting:
                reason = decision.reason + " Waiting: " + "; ".join(waiting) + "."
        elif not settings.dry_run and _apply_keep_monitoring(series, decision, actions):
            applied = True
        elif settings.dry_run:
            searches_ran = planned
    except SafetyError as exc:
        error = str(exc)
        applied = False
        logger.error("%s", exc)
    except Exception as exc:
        error = str(exc)
        applied = False
        logger.exception("Failed while updating %s", series.label)

    return SeriesResult(
        sonarr_id=series.id,
        title=series.title,
        year=series.year,
        tvdb_id=series.tvdb_id,
        path=series.path,
        plex_matched=watch.matched,
        match_source=watch.match_source,
        action=decision.action,
        reason=reason,
        last_viewed_at=_iso(watch.last_viewed_at),
        season1_last_viewed_at=_iso(watch.season1_last_viewed_at),
        active_seasons=sorted(watch.active_seasons),
        delete_seasons=list(decision.delete_seasons),
        search_seasons=list(decision.search_seasons),
        searches_ran=searches_ran,
        searches_waiting=waiting,
        bytes_to_free=decision.bytes_to_free,
        dry_run=settings.dry_run,
        error=error,
        seasons=rows,
        episode_count=totals.episode_count,
        file_count=totals.file_count,
        size_on_disk=totals.size_on_disk,
        kept_episode_count=totals.kept_episode_count,
        kept_file_count=totals.kept_file_count,
        kept_bytes=totals.kept_bytes,
        later_episode_count=totals.later_episode_count,
        later_file_count=totals.later_file_count,
        later_bytes=totals.later_bytes,
        files_targeted=len(decision.delete_file_ids),
        applied=applied,
        pending_approval=needs_approval and error is None,
        protected_seasons=list(decision.protected_seasons),
        queued_delete=queued_delete and error is None,
        protection_note=decision.protection_note,
        protection_result=decision.protection_result,
    )


def execute(
    series_list: list[SeriesSnapshot],
    index: WatchIndex,
    actions: SonarrActions,
    state: State,
    settings: PolicySettings,
    now: datetime,
    queue: set[tuple[int, int]],
    running_searches: set[tuple[int, int]],
    progress: Progress | None = None,
    defer_deletes: bool = False,
) -> RunReport:
    started = now
    results: list[SeriesResult] = []
    total = len(series_list)
    dismissed = state.dismissed_ids()
    previous = state.load_report() or {}
    already_pending = {
        int(row.get("sonarr_id") or 0)
        for row in (previous.get("series") or [])
        if row.get("pending_approval")
    }
    allowlist = settings.live_series_ids
    if total == 0:
        report(progress, "Checking shows, 0 of 0.", done=0, total=0)
    for index_number, series in enumerate(series_list, start=1):
        report(progress, f"Checking shows, {index_number} of {total}.", done=index_number, total=total)
        watch = watch_for(series, index)
        arrivals = resolve_arrivals(state, series, actions)
        decision = decide(series, watch, now, settings, arrivals=arrivals)
        outside = bool(allowlist) and series.id not in allowlist and not settings.dry_run
        scoped = replace(settings, dry_run=True) if outside else settings
        result = apply_decision(
            series,
            decision,
            watch=watch,
            actions=actions,
            state=state,
            settings=scoped,
            now=now,
            queue=queue,
            running_searches=running_searches,
            announce_preview=not outside,
            defer_deletes=defer_deletes,
        )
        if outside and result.action in {"prune", "restore"}:
            result.searches_ran = []
            result.reason = (
                "Outside the live allowlist, so this show was left unchanged. " + result.reason
            )
        result.in_live_scope = not settings.dry_run and not outside
        _note_binge(state, result, now)
        if result.action != "prune":
            if series.id in dismissed:
                state.clear_dismissed(series.id)
        elif series.id in dismissed:
            result.pending_approval = False
            result.dismissed = True
            result.reason = (
                "Dismissed. Files stay until you approve a delete. " + result.reason
            )
        results.append(result)
    finished = datetime.now(timezone.utc)
    for result in results:
        if not result.pending_approval or result.sonarr_id in already_pending:
            continue
        name = f"{result.title} ({result.year})" if result.year else result.title
        message = approval_line(name, result.delete_seasons, result.bytes_to_free)
        logger.info("%s", message)
        state.record_activity(
            at=finished,
            kind="approval",
            sonarr_id=result.sonarr_id,
            title=result.title,
            message=message,
            nbytes=result.bytes_to_free,
            seasons=len(result.delete_seasons),
        )
    counts: dict[str, int] = {}
    pending = 0
    pending_bytes = 0
    protected = 0
    searches = 0
    restored_seasons = 0
    freed_bytes = 0
    queued = 0
    queued_bytes = 0
    for result in results:
        counts[result.action] = counts.get(result.action, 0) + 1
        protected += len(result.protected_seasons)
        if result.queued_delete:
            queued += 1
            queued_bytes += result.bytes_to_free
        if result.pending_approval:
            pending += 1
            pending_bytes += result.bytes_to_free
        if result.applied and result.action == "restore":
            searches += len(result.searches_ran)
            restored_seasons += len(result.searches_ran)
        if result.applied and result.action == "prune" and result.files_targeted:
            freed_bytes += result.bytes_to_free
    summary = cycle_summary(
        dry_run=settings.dry_run,
        prune=counts.get("prune", 0),
        hold=counts.get("hold", 0),
        restore=counts.get("restore", 0),
        skip=counts.get("skip", 0),
        pending=pending,
        protected=protected,
        searches=searches,
        restored_seasons=restored_seasons,
        freed_bytes=freed_bytes,
        pending_bytes=pending_bytes,
        queued=queued,
        queued_bytes=queued_bytes,
    )
    logger.info("%s", summary)
    state.record_cycle(
        finished_at=finished.isoformat(),
        dry_run=settings.dry_run,
        summary=summary,
        prune=counts.get("prune", 0),
        hold=counts.get("hold", 0),
        restore=counts.get("restore", 0),
        skip=counts.get("skip", 0),
        pending=pending,
        protected=protected,
        searches=searches,
        restored_seasons=restored_seasons,
        freed_bytes=freed_bytes,
        pending_bytes=pending_bytes,
    )
    return RunReport(
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        dry_run=settings.dry_run,
        history_complete=index.history_complete,
        ambiguous_plays=list(index.ambiguous_plays),
        results=results,
        error=None,
    )
