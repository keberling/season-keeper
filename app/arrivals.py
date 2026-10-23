"""Remember when each season first arrived so a re-download does not restart grace.

A request uses the dates already stored, plus the current file date when a
season has never been seen. Sonarr history is read once per series by a
background seeder, never while a page is waiting.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

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


def _season_numbers(series: SeriesSnapshot) -> set[int]:
    seasons = {episode.season_number for episode in series.episodes}
    seasons.update(season.season_number for season in series.seasons)
    seasons.discard(0)
    return seasons


def resolve_arrivals(state: State, series: SeriesSnapshot, sonarr=None) -> dict[int, datetime]:
    """Stored first-seen dates. A missing season uses the current file date.

    ``sonarr`` is accepted so callers can stay the same. History is not read
    here; that scan is what made a single show preview take minutes.
    """
    del sonarr
    stored = state.season_arrivals(series.id)
    resolved: dict[int, datetime] = {}
    for number in _season_numbers(series):
        if number in stored:
            resolved[number] = stored[number]
            continue
        added = earliest_file_added(series, number)
        if added is not None:
            resolved[number] = added
    state.remember_arrivals(series.id, resolved)
    if not state.arrival_seeded(series.id):
        state.note_arrival_seed(series.id, datetime.now(timezone.utc))
    return resolved


def seed_arrivals_from_history(state: State, series: SeriesSnapshot, sonarr, now: datetime) -> bool:
    """Read Sonarr history once and keep any date earlier than the file date.

    A second call for the same show does nothing. A failed read is not marked
    done, so the caller can try again later.
    """
    if state.arrival_seeded(series.id):
        return False
    fetch = getattr(sonarr, "earliest_arrivals", None)
    history: dict[int, datetime] = {}
    if fetch is not None:
        logger.info("Reading Sonarr history once for %s.", series.label)
        history = fetch(
            series.id,
            {episode.id: episode.season_number for episode in series.episodes},
        ) or {}
    resolved: dict[int, datetime] = {}
    for number, when in history.items():
        if int(number) == 0 or when is None:
            continue
        resolved[int(number)] = when
    for number in _season_numbers(series):
        if number in resolved:
            continue
        added = earliest_file_added(series, number)
        if added is not None:
            resolved[number] = added
    state.remember_arrivals(series.id, resolved)
    state.mark_arrival_seeded(series.id, now)
    return True


def serve_arrival_seeds(stop: threading.Event, step) -> None:
    """Seed one show at a time. step returns True when it seeded a show."""
    while not stop.is_set():
        worked = False
        try:
            worked = bool(step())
        except Exception:
            logger.exception("Could not seed a season arrival")
        if stop.wait(1 if worked else 5):
            return
