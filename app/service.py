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
from app.sonarr import SonarrClient, parse_series, select_tagged
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
) -> RunReport:
    started = datetime.now(timezone.utc)
    moment = now or started
    try:
        report = _run_cycle(settings, state, sonarr, plex, moment)
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
) -> RunReport:
    try:
        version = sonarr.status_version()
    except Exception:
        version = "unknown"
    try:
        plex_name = plex.server_name()
    except Exception:
        plex_name = "unknown"
    logger.info("Talking to Sonarr %s and Plex %s", version, plex_name)

    tag_id = sonarr.ensure_tag(settings.sonarr_tag)
    rows = select_tagged(sonarr.list_series(), tag_id)
    snapshots = [
        parse_series(row, sonarr.list_episodes(int(row["id"])), now) for row in rows
    ]
    shows = plex.list_shows()
    window = timedelta(days=settings.watch_window_days)
    history, complete = plex.recent_history(now - window)
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

    return execute(
        snapshots,
        index,
        sonarr,
        state,
        settings.policy(),
        now,
        sonarr.queued_seasons(),
        sonarr.running_searches(),
    )


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


def _load_one(sonarr: SonarrClient, settings: Settings, series_id: int, now: datetime):
    tag_id = sonarr.ensure_tag(settings.sonarr_tag)
    match = None
    for row in sonarr.list_series():
        if int(row["id"]) == int(series_id):
            match = row
            break
    if match is None:
        raise SeriesNotFound(f"Sonarr has no series {series_id}")
    if tag_id not in (match.get("tags") or []):
        raise SeriesNotFound(
            f"Add the {settings.sonarr_tag} tag to this series in Sonarr before testing it"
        )
    return parse_series(match, sonarr.list_episodes(int(series_id)), now)
