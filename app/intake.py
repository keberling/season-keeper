"""Turn new Plex watchlist items into a Sonarr season-1 add or a Radarr movie add."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import Settings
from app.human import watchlist_line
from app.models import SeriesSnapshot, ShowActivity, WatchIndex
from app.radarr import RadarrClient, movie_add_payload, movie_monitor_payload
from app.sonarr import (
    SonarrClient,
    parse_series,
    series_add_payload,
    series_update_payload,
)
from app.state import State
from app.watchlist import WatchlistItem, identity_key, load_watchlist

logger = logging.getLogger("trimarr")

_DONE = {"added", "searched", "skipped"}
_COUNTED = {"added", "searched"}


@dataclass
class _Outcome:
    action: str
    detail: str
    sonarr_id: int | None = None
    radarr_id: int | None = None
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None


def season_one_gap(series: SeriesSnapshot) -> bool:
    """Season 1 needs a monitor and a search when it is off, empty, or missing an aired episode."""
    season = next((item for item in series.seasons if item.season_number == 1), None)
    episodes = [episode for episode in series.episodes if episode.season_number == 1]
    if season is None or not season.monitored:
        return True
    if not episodes:
        return True
    return any(episode.aired and not episode.has_file for episode in episodes)


def apply_watchlist_marks(index: WatchIndex, marks: dict[int, tuple[datetime, str]]) -> None:
    """A matched show keeps the first watchlist time so the quiet-day clock can restart."""
    for tvdb_id, (when, who) in marks.items():
        if tvdb_id not in index.matched_tvdb:
            continue
        activity = index.by_tvdb.setdefault(tvdb_id, ShowActivity())
        activity.watchlisted_at = when
        activity.watchlisted_by = who


def run_watchlist_intake(
    settings: Settings,
    state: State,
    now: datetime | None = None,
    *,
    sonarr: SonarrClient | None = None,
    radarr: RadarrClient | None = None,
    http: httpx.Client | None = None,
) -> list[dict]:
    moment = now or datetime.now(timezone.utc)
    own_http = http is None
    client = http or httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=True)
    own_sonarr = False
    own_radarr = False
    try:
        if sonarr is None and settings.tv_intake_enabled and settings.sonarr_url and settings.sonarr_api_key.strip():
            sonarr = SonarrClient(settings.sonarr_base, settings.sonarr_api_key, settings.http_timeout_seconds)
            own_sonarr = True
        if radarr is None and settings.movie_intake_enabled and settings.radarr_configured:
            radarr = RadarrClient(settings.radarr_base, settings.radarr_api_key, settings.http_timeout_seconds)
            own_radarr = True
        items, feed_errors = load_watchlist(settings, client)
        if settings.watchlist_rss_urls or settings.plex_token.strip():
            state.note_watchlist_feed(
                error=" ".join(feed_errors) if feed_errors else None,
                at=moment,
            )
        return _process(settings, state, items, sonarr, radarr, moment)
    finally:
        if own_http:
            client.close()
        if own_sonarr and sonarr is not None:
            sonarr.close()
        if own_radarr and radarr is not None:
            radarr.close()


def _process(
    settings: Settings,
    state: State,
    items: list[WatchlistItem],
    sonarr: SonarrClient | None,
    radarr: RadarrClient | None,
    now: datetime,
) -> list[dict]:
    shows: list[dict] | None = None
    movies: list[dict] | None = None
    sonarr_tag: int | None = None
    radarr_tag: int | None = None
    results: list[dict] = []
    for item in items:
        key = identity_key(item)
        previous = state.get_watchlist_item(key)
        # Only a finished add is skipped. A missed item has no completed row, so the
        # next good fetch still picks it up while it remains in the feed.
        if previous and previous["action"] in _DONE:
            _stamp(state, previous.get("tvdb_id") or item.tvdb_id, _when(previous, now), previous.get("added_by") or item.added_by)
            continue
        seen_at = _when(previous, now)
        try:
            if item.media == "movie":
                outcome, movies, radarr_tag = _movie(
                    item, settings, state, radarr, movies, radarr_tag, seen_at, now
                )
            else:
                outcome, shows, sonarr_tag = _show(
                    item, settings, state, sonarr, shows, sonarr_tag, seen_at, now
                )
        except Exception as exc:
            outcome = _Outcome("error", _scrub(str(exc), settings) or "The watchlist item could not be processed.")
        _remember(settings, state, item, key, seen_at, now, previous, outcome)
        results.append(
            {
                "key": key,
                "title": item.label(),
                "added_by": item.added_by,
                "action": outcome.action,
                "detail": outcome.detail,
                "sonarr_id": outcome.sonarr_id,
                "radarr_id": outcome.radarr_id,
            }
        )
    return results


def _show(
    item: WatchlistItem,
    settings: Settings,
    state: State,
    sonarr: SonarrClient | None,
    shows: list[dict] | None,
    tag_id: int | None,
    seen_at: datetime,
    now: datetime,
) -> tuple[_Outcome, list[dict] | None, int | None]:
    _stamp(state, item.tvdb_id, seen_at, item.added_by)
    if not settings.tv_intake_enabled:
        return (
            _Outcome("disabled", "TV watchlist intake is off, so it was not added.", tvdb_id=item.tvdb_id),
            shows,
            tag_id,
        )
    if sonarr is None:
        return (
            _Outcome("needs-settings", "Save Sonarr before this show can be added.", tvdb_id=item.tvdb_id),
            shows,
            tag_id,
        )
    if shows is None:
        shows = sonarr.list_series()
    match = next((row for row in shows if _same_series(row, item)), None)
    if match is None:
        return _add_show(item, settings, state, sonarr, seen_at), shows, tag_id
    series_id = int(match["id"])
    snapshot = parse_series(match, sonarr.list_episodes(series_id), now)
    _stamp(state, snapshot.tvdb_id or item.tvdb_id, seen_at, item.added_by)
    tvdb_id = snapshot.tvdb_id or item.tvdb_id
    if not season_one_gap(snapshot):
        return (
            _Outcome(
                "skipped",
                "Season 1 is already in Sonarr and on disk.",
                sonarr_id=series_id,
                tvdb_id=tvdb_id,
            ),
            shows,
            tag_id,
        )
    if settings.dry_run:
        return (
            _Outcome(
                "preview",
                f"Would monitor season 1 of {item.label()} and search.",
                sonarr_id=series_id,
                tvdb_id=tvdb_id,
            ),
            shows,
            tag_id,
        )
    if tag_id is None:
        tag_id = sonarr.ensure_tag(settings.sonarr_tag)
    _monitor_season_one(sonarr, snapshot, tag_id)
    return (
        _Outcome(
            "searched",
            "Season 1 was missing, so it is monitored and searched.",
            sonarr_id=series_id,
            tvdb_id=tvdb_id,
        ),
        shows,
        tag_id,
    )


def _add_show(
    item: WatchlistItem,
    settings: Settings,
    state: State,
    sonarr: SonarrClient,
    seen_at: datetime,
) -> _Outcome:
    term = _show_term(item)
    if term is None:
        return _Outcome("error", f"Could not find a TVDB, TMDB, or IMDb id for {item.label()}.")
    if not settings.sonarr_root_folder.strip() or not settings.sonarr_quality_profile_id:
        if item.tvdb_id is None:
            looked = sonarr.lookup_series(term)
            if looked and looked.get("tvdbId"):
                _stamp(state, int(looked["tvdbId"]), seen_at, item.added_by)
                item = item.with_ids(tvdb_id=int(looked["tvdbId"]))
        return _Outcome(
            "needs-settings",
            "Choose a Sonarr root folder and quality profile before this show can be added.",
            tvdb_id=item.tvdb_id,
        )
    looked = sonarr.lookup_series(term)
    if not looked:
        return _Outcome("error", f"Sonarr could not find {item.label()}.")
    tvdb_id = int(looked["tvdbId"]) if looked.get("tvdbId") else item.tvdb_id
    _stamp(state, tvdb_id, seen_at, item.added_by)
    if settings.dry_run:
        return (
            _Outcome(
                "preview",
                f"Would add {item.label()} to Sonarr, monitor only season 1, and search.",
                tvdb_id=tvdb_id,
            )
        )
    tag_id = sonarr.ensure_tag(settings.sonarr_tag)
    created = sonarr.add_series(
        series_add_payload(
            looked,
            profile_id=int(settings.sonarr_quality_profile_id),
            root=settings.sonarr_root_folder.strip(),
            tag_id=tag_id,
        )
    )
    sonarr_id = int(created["id"]) if created.get("id") else None
    return (
        _Outcome(
            "added",
            "Added it to Sonarr, monitoring only season 1, and searched.",
            sonarr_id=sonarr_id,
            tvdb_id=tvdb_id,
            imdb_id=item.imdb_id,
            tmdb_id=item.tmdb_id,
        )
    )


def _movie(
    item: WatchlistItem,
    settings: Settings,
    state: State,
    radarr: RadarrClient | None,
    movies: list[dict] | None,
    tag_id: int | None,
    seen_at: datetime,
    now: datetime,
) -> tuple[_Outcome, list[dict] | None, int | None]:
    del state, seen_at, now
    if not settings.movie_intake_enabled:
        return (
            _Outcome("disabled", "Movie watchlist intake is off, so it was not added."),
            movies,
            tag_id,
        )
    if not settings.radarr_configured or radarr is None:
        return (
            _Outcome("needs-settings", "Save the Radarr URL and API key before this movie can be added."),
            movies,
            tag_id,
        )
    if movies is None:
        movies = radarr.list_movies()
    match = next((row for row in movies if _same_movie(row, item)), None)
    if match is None:
        outcome = _add_movie(item, settings, radarr)
        return outcome, movies, tag_id
    movie_id = int(match["id"])
    monitored = bool(match.get("monitored"))
    has_file = bool(match.get("hasFile") or match.get("movieFile"))
    if monitored and has_file:
        return (
            _Outcome("skipped", "Already in Radarr and on disk.", radarr_id=movie_id, tmdb_id=item.tmdb_id),
            movies,
            tag_id,
        )
    if settings.dry_run:
        return (
            _Outcome(
                "preview",
                f"Would monitor {item.label()} in Radarr and search.",
                radarr_id=movie_id,
                tmdb_id=item.tmdb_id,
            ),
            movies,
            tag_id,
        )
    if tag_id is None:
        tag_id = radarr.ensure_tag(settings.radarr_tag)
    if not monitored or tag_id not in [int(tag) for tag in (match.get("tags") or [])]:
        radarr.update_movie(movie_monitor_payload(match, tag_id=tag_id))
    radarr.search_movie(movie_id)
    return (
        _Outcome(
            "searched",
            "It was in Radarr but not monitored or missing, so it was monitored and searched.",
            radarr_id=movie_id,
            tmdb_id=item.tmdb_id,
        ),
        movies,
        tag_id,
    )


def _add_movie(item: WatchlistItem, settings: Settings, radarr: RadarrClient) -> _Outcome:
    term = _movie_term(item)
    if term is None:
        return _Outcome("error", f"Could not find a TVDB, TMDB, or IMDb id for {item.label()}.")
    if not settings.radarr_root_folder.strip() or not settings.radarr_quality_profile_id:
        return _Outcome(
            "needs-settings",
            "Choose a Radarr root folder and quality profile before this movie can be added.",
        )
    looked = radarr.lookup_movie(term)
    if not looked:
        return _Outcome("error", f"Radarr could not find {item.label()}.")
    tmdb_id = int(looked["tmdbId"]) if looked.get("tmdbId") else item.tmdb_id
    if settings.dry_run:
        return _Outcome(
            "preview",
            f"Would add {item.label()} to Radarr and search.",
            tmdb_id=tmdb_id,
        )
    tag_id = radarr.ensure_tag(settings.radarr_tag)
    created = radarr.add_movie(
        movie_add_payload(
            looked,
            profile_id=int(settings.radarr_quality_profile_id),
            root=settings.radarr_root_folder.strip(),
            tag_id=tag_id,
        )
    )
    radarr_id = int(created["id"]) if created.get("id") else None
    return _Outcome(
        "added",
        "Added it to Radarr and searched.",
        radarr_id=radarr_id,
        tmdb_id=tmdb_id,
        imdb_id=item.imdb_id,
    )


def _monitor_season_one(sonarr: SonarrClient, snapshot: SeriesSnapshot, tag_id: int) -> None:
    season_monitored = {1: True}
    if any(season.season_number == 0 for season in snapshot.seasons) or any(
        episode.season_number == 0 for episode in snapshot.episodes
    ):
        season_monitored[0] = False
    payload = series_update_payload(
        snapshot.raw,
        season_monitored=season_monitored,
        series_monitored=True,
        monitor_new_items=None,
    )
    tags = [int(tag) for tag in (payload.get("tags") or [])]
    if tag_id not in tags:
        tags.append(tag_id)
    payload["tags"] = tags
    sonarr.update_series(payload)
    monitor_ids = [episode.id for episode in snapshot.episodes if episode.season_number == 1 and not episode.monitored]
    if monitor_ids:
        sonarr.monitor_episodes(monitor_ids, True)
    specials = [episode.id for episode in snapshot.episodes if episode.season_number == 0 and episode.monitored]
    if specials:
        sonarr.monitor_episodes(specials, False)
    sonarr.search_season(snapshot.id, 1)


def _show_term(item: WatchlistItem) -> str | None:
    if item.tvdb_id:
        return f"tvdb:{item.tvdb_id}"
    if item.imdb_id:
        return f"imdb:{item.imdb_id}"
    if item.tmdb_id:
        return f"tmdb:{item.tmdb_id}"
    return None


def _movie_term(item: WatchlistItem) -> str | None:
    if item.tmdb_id:
        return f"tmdb:{item.tmdb_id}"
    if item.imdb_id:
        return f"imdb:{item.imdb_id}"
    return None


def _same_series(row: dict, item: WatchlistItem) -> bool:
    tvdb = int(row.get("tvdbId") or 0)
    tmdb = int(row.get("tmdbId") or 0)
    imdb = str(row.get("imdbId") or "")
    if item.tvdb_id and tvdb == item.tvdb_id:
        return True
    if item.imdb_id and imdb == item.imdb_id:
        return True
    if item.tmdb_id and tmdb == item.tmdb_id:
        return True
    return False


def _same_movie(row: dict, item: WatchlistItem) -> bool:
    tmdb = int(row.get("tmdbId") or 0)
    imdb = str(row.get("imdbId") or "")
    if item.tmdb_id and tmdb == item.tmdb_id:
        return True
    if item.imdb_id and imdb == item.imdb_id:
        return True
    return False


def _stamp(state: State, tvdb_id: int | None, when: datetime, who: str) -> None:
    if not tvdb_id:
        return
    state.remember_watchlist_mark(int(tvdb_id), when, who or "Someone")


def _when(previous: dict | None, now: datetime) -> datetime:
    if previous and previous.get("first_seen_at"):
        parsed = datetime.fromisoformat(str(previous["first_seen_at"]))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return now


def _scrub(text: str, settings: Settings) -> str:
    for secret in (
        settings.sonarr_api_key,
        settings.plex_token,
        settings.radarr_api_key,
        settings.app_token,
        settings.plex_watchlist_rss_url,
        settings.plex_friends_watchlist_rss_url,
    ):
        if secret:
            text = text.replace(secret, "******")
    return text[:300]


def _remember(
    settings: Settings,
    state: State,
    item: WatchlistItem,
    key: str,
    seen_at: datetime,
    now: datetime,
    previous: dict | None,
    outcome: _Outcome,
) -> None:
    tvdb_id = outcome.tvdb_id or item.tvdb_id
    _stamp(state, tvdb_id, seen_at, item.added_by)
    state.save_watchlist_item(
        item_key=key,
        media=item.media,
        title=item.label(),
        added_by=item.added_by,
        plex_guid=item.plex_guid,
        imdb_id=outcome.imdb_id or item.imdb_id,
        tvdb_id=tvdb_id,
        tmdb_id=outcome.tmdb_id or item.tmdb_id,
        first_seen_at=seen_at.isoformat(),
        action=outcome.action,
        detail=outcome.detail,
        sonarr_id=outcome.sonarr_id,
        radarr_id=outcome.radarr_id,
    )
    if previous and previous.get("action") == outcome.action and previous.get("detail") == outcome.detail:
        return
    message = watchlist_line(item.added_by, item.label(), outcome.detail)
    logger.info("%s", message)
    state.record_activity(
        at=now,
        kind="watchlist",
        title=item.label(),
        message=message,
        sonarr_id=outcome.sonarr_id,
        seasons=1 if outcome.action in _COUNTED else 0,
    )
