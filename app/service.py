"""One scheduled pass, plus the single-show delete and download actions."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.engine import apply_decision
from app.models import RunReport, SeriesResult
from app.plex import PlexClient, build_watch_index
from app.policy import plan_manual_prune, plan_manual_restore
from app.progress import Progress, count_phrase, report as report_progress
from app.sonarr import SonarrClient, parse_series, partition_ignored, series_tag_payload
from app.state import State

logger = logging.getLogger("season_keeper")


class SeriesNotFound(RuntimeError):
    pass


def load_overrides(path: Path) -> dict[int, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not read {path.name}: {exc}") from exc
    raw = data.get("tvdbToPlexRatingKey") or {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"{path.name} field tvdbToPlexRatingKey must be an object")
    parsed: dict[int, str] = {}
    for tvdb_id, rating_key in raw.items():
        parsed[int(tvdb_id)] = str(rating_key)
    return parsed


def run_cycle(
    settings: Settings,
    state: State,
    sonarr: SonarrClient,
    plex: PlexClient,
    now: datetime | None = None,
    progress: Progress | None = None,
) -> RunReport:
    started = datetime.now(timezone.utc)
    moment = now or started
    try:
        report = _run_cycle(settings, state, sonarr, plex, moment, progress)
    except Exception as exc:
        logger.exception("Cycle failed before any library changes")
        report = RunReport(
            started_at=started.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            dry_run=settings.dry_run,
            history_complete=False,
            ambiguous_plays=[],
            results=[],
            error=str(exc),
        )
    state.save_report(report)
    return report


def _run_cycle(
    settings: Settings,
    state: State,
    sonarr: SonarrClient,
    plex: PlexClient,
    now: datetime,
    progress: Progress | None = None,
) -> RunReport:
    report_progress(progress, "Checking Sonarr and Plex.")
    try:
        version = sonarr.status_version()
    except Exception:
        version = "unknown"
    try:
        plex_name = plex.server_name()
    except Exception:
        plex_name = "unknown"
    logger.info("Talking to Sonarr %s and Plex %s", version, plex_name)

    tag_id = sonarr.ensure_tag(settings.ignore_tag)
    rows, ignored = partition_ignored(sonarr.list_series(), tag_id)
    if ignored:
        report_progress(
            progress,
            f"Skipping {count_phrase(len(ignored), 'show')} tagged {settings.ignore_tag}.",
        )
    total = len(rows)
    snapshots = []
    if total == 0:
        report_progress(progress, "Sonarr episodes, 0 of 0.", done=0, total=0)
    for position, row in enumerate(rows, start=1):
        title = str(row.get("title") or f"Series {row.get('id')}")
        report_progress(
            progress,
            f"Sonarr episodes, {position} of {total}: {title}.",
            done=position,
            total=total,
        )
        snapshots.append(parse_series(row, sonarr.list_episodes(int(row["id"])), now))
    shows = plex.list_shows(progress=progress)
    window = timedelta(days=settings.watch_window_days)
    history, complete = plex.recent_history(
        now - window,
        progress=progress,
        window_days=settings.watch_window_days,
    )
    sessions = plex.active_sessions()
    overrides = load_overrides(settings.data_dir / "overrides.json")
    index = build_watch_index(
        shows,
        history,
        sessions,
        history_complete=complete,
        overrides=overrides,
        now=now,
    )
    from app.engine import execute

    report = execute(
        snapshots,
        index,
        sonarr,
        state,
        settings.policy(),
        now,
        sonarr.queued_seasons(),
        sonarr.running_searches(),
        progress,
    )
    report.results.extend(_ignored_result(row, settings.ignore_tag) for row in ignored)
    return report


def preview_series(
    settings: Settings,
    state: State,
    sonarr: SonarrClient,
    series_id: int,
    now: datetime | None = None,
) -> dict:
    moment = now or datetime.now(timezone.utc)
    series = _load_one(sonarr, settings, series_id, moment)
    policy = settings.policy()
    preview_policy = replace(policy, dry_run=True)
    delete = apply_decision(
        series,
        plan_manual_prune(series, policy),
        watch=None,
        actions=sonarr,
        state=state,
        settings=preview_policy,
        now=moment,
        queue=set(),
        running_searches=set(),
        announce_preview=False,
    )
    download = apply_decision(
        series,
        plan_manual_restore(series, policy, moment),
        watch=None,
        actions=sonarr,
        state=state,
        settings=preview_policy,
        now=moment,
        queue=sonarr.queued_seasons(),
        running_searches=sonarr.running_searches(),
        honor_cooldown=False,
        announce_preview=False,
    )
    return {"delete": delete.to_dict(), "download": download.to_dict()}


def delete_later_seasons(
    settings: Settings,
    state: State,
    sonarr: SonarrClient,
    series_id: int,
    now: datetime | None = None,
) -> SeriesResult:
    moment = now or datetime.now(timezone.utc)
    series = _load_one(sonarr, settings, series_id, moment)
    policy = replace(settings.policy(), dry_run=False)
    result = apply_decision(
        series,
        plan_manual_prune(series, policy),
        watch=None,
        actions=sonarr,
        state=state,
        settings=policy,
        now=moment,
        queue=set(),
        running_searches=set(),
        announce_preview=False,
    )
    _preserve_watch(state, result)
    state.replace_series(result.to_dict())
    logger.info("Manual delete for %s: %s", series.label, result.reason)
    return result


def download_later_seasons(
    settings: Settings,
    state: State,
    sonarr: SonarrClient,
    series_id: int,
    now: datetime | None = None,
) -> SeriesResult:
    moment = now or datetime.now(timezone.utc)
    series = _load_one(sonarr, settings, series_id, moment)
    policy = replace(settings.policy(), dry_run=False)
    result = apply_decision(
        series,
        plan_manual_restore(series, policy, moment),
        watch=None,
        actions=sonarr,
        state=state,
        settings=policy,
        now=moment,
        queue=sonarr.queued_seasons(),
        running_searches=sonarr.running_searches(),
        honor_cooldown=False,
        announce_preview=False,
    )
    _preserve_watch(state, result)
    state.replace_series(result.to_dict())
    logger.info("Manual download for %s: %s", series.label, result.reason)
    return result


def _preserve_watch(state: State, result: SeriesResult) -> None:
    current = state.load_report()
    if not current:
        return
    for row in current.get("series") or []:
        if row.get("sonarr_id") != result.sonarr_id:
            continue
        result.plex_matched = bool(row.get("plex_matched"))
        result.match_source = row.get("match_source")
        result.last_viewed_at = row.get("last_viewed_at")
        result.season1_last_viewed_at = row.get("season1_last_viewed_at")
        result.active_seasons = list(row.get("active_seasons") or [])
        return


def set_keep_all(
    settings: Settings,
    state: State,
    sonarr: SonarrClient,
    series_id: int,
    keep: bool,
) -> dict:
    """Add or remove the ignore tag. This does not delete files or search."""
    tag_id = sonarr.ensure_tag(settings.ignore_tag)
    match = _find_series(sonarr, series_id)
    current_tags = [int(tag) for tag in (match.get("tags") or [])]
    if keep:
        tags = current_tags if tag_id in current_tags else [*current_tags, tag_id]
    else:
        tags = [tag for tag in current_tags if tag != tag_id]
    if tags != current_tags:
        sonarr.update_series(series_tag_payload(match, tags))
    row = _report_row(state, int(series_id))
    if row is None:
        if keep:
            row = _ignored_result(match, settings.ignore_tag).to_dict()
        else:
            row = _plain_result(
                match,
                action="skip",
                reason="Included again. Refresh the preview to read this show.",
            ).to_dict()
    else:
        row["error"] = None
        row["bytes_to_free"] = 0
        row["applied"] = False
        if keep:
            row["action"] = "ignore"
            row["later_bytes"] = 0
            row["dry_run"] = True
            row["reason"] = (
                f"Tagged {settings.ignore_tag}. The schedule will not delete or download this show."
            )
        else:
            row["action"] = "skip"
            row["reason"] = "Included again. Refresh the preview to read this show."
    state.replace_series(row)
    return row


def _find_series(sonarr: SonarrClient, series_id: int) -> dict:
    for row in sonarr.list_series():
        if int(row["id"]) == int(series_id):
            return row
    raise SeriesNotFound(f"Sonarr has no series {series_id}")


def _report_row(state: State, series_id: int) -> dict | None:
    current = state.load_report()
    if not current:
        return None
    for row in current.get("series") or []:
        if int(row.get("sonarr_id") or 0) == series_id:
            return dict(row)
    return None


def _series_identity(row: dict) -> tuple[int, str, int | None, int | None, str]:
    year = row.get("year") or None
    if year == 0:
        year = None
    tvdb = row.get("tvdbId") or None
    if tvdb == 0:
        tvdb = None
    title = str(row.get("title") or f"Series {row.get('id')}")
    return (
        int(row["id"]),
        title,
        int(year) if year else None,
        int(tvdb) if tvdb else None,
        str(row.get("path") or ""),
    )


def _plain_result(row: dict, *, action: str, reason: str) -> SeriesResult:
    sonarr_id, title, year, tvdb_id, path = _series_identity(row)
    return SeriesResult(
        sonarr_id=sonarr_id,
        title=title,
        year=year,
        tvdb_id=tvdb_id,
        path=path,
        plex_matched=False,
        match_source=None,
        action=action,
        reason=reason,
        last_viewed_at=None,
        season1_last_viewed_at=None,
        active_seasons=[],
        delete_seasons=[],
        search_seasons=[],
        searches_ran=[],
        searches_waiting=[],
        bytes_to_free=0,
        dry_run=True,
        error=None,
    )


def _ignored_result(row: dict, tag: str) -> SeriesResult:
    return _plain_result(
        row,
        action="ignore",
        reason=f"Tagged {tag}. The schedule will not delete or download this show.",
    )


def _load_one(sonarr: SonarrClient, settings: Settings, series_id: int, now: datetime):
    match = _find_series(sonarr, series_id)
    return parse_series(match, sonarr.list_episodes(int(series_id)), now)
