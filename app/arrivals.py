"""Remember when each season first arrived so a re-download does not restart grace."""

from __future__ import annotations

import logging
from datetime import datetime

from app.models import SeriesSnapshot
from app.state import State

logger = logging.getLogger("trimarr")

ARRIVAL_EVENTS = frozenset({"grabbed", "downloadFolderImported", "seriesFolderImported"})


def earliest_file_added(series: SeriesSnapshot, season_number: int) -> datetime | None:
    added = [
        episode.file_added_at
        for episode in series.episodes
        if episode.season_number == season_number and episode.file_added_at is not None
    ]
    if not added:
        return None
    return min(added)


def arrivals_from_history(
    records: list[dict],
    episode_seasons: dict[int, int],
) -> dict[int, datetime]:
    """Earliest grab or import for each season. Later upgrades are ignored."""
    from app.sonarr import parse_timestamp

    found: dict[int, datetime] = {}
    for record in records:
        if str(record.get("eventType") or "") not in ARRIVAL_EVENTS:
            continue
        when = parse_timestamp(record.get("date"))
        if when is None:
            continue
        season = record.get("seasonNumber")
        if season is None:
            data = record.get("data") or {}
            season = data.get("seasonNumber")
        if season is None:
            episode_id = record.get("episodeId")
            if episode_id is None:
                continue
            season = episode_seasons.get(int(episode_id))
        if season is None:
            continue
        number = int(season)
        if number == 0:
            continue
        current = found.get(number)
        if current is None or when < current:
            found[number] = when
    return found


def resolve_arrivals(state: State, series: SeriesSnapshot, sonarr=None) -> dict[int, datetime]:
    """First-seen dates for this show. A later file date never replaces an earlier one."""
    stored = state.season_arrivals(series.id)
    seasons = {episode.season_number for episode in series.episodes}
    seasons.update(season.season_number for season in series.seasons)
    seasons.discard(0)
    history: dict[int, datetime] = {}
    if sonarr is not None and any(number not in stored for number in seasons):
        fetch = getattr(sonarr, "earliest_arrivals", None)
        if fetch is not None:
            try:
                history = fetch(
                    series.id,
                    {episode.id: episode.season_number for episode in series.episodes},
                ) or {}
            except Exception:
                logger.debug("Could not read Sonarr history for %s", series.label, exc_info=True)
    resolved: dict[int, datetime] = {}
    for number in seasons:
        if number in stored:
            resolved[number] = stored[number]
            continue
        candidates = []
        if number in history:
            candidates.append(history[number])
        added = earliest_file_added(series, number)
        if added is not None:
            candidates.append(added)
        if candidates:
            resolved[number] = min(candidates)
    state.remember_arrivals(series.id, resolved)
    return resolved
