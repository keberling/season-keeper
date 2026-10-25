"""Plex library matching and watch activity.

Matching is by TVDB id. A play with no show id does not block deletes of other
shows. If that play's show title matches one or more library shows, those
shows are held and are not searched. If the history pages stop before the
watch window is covered, pruning is disabled for that run.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.models import PlexShow, ShowActivity, WatchIndex
from app.progress import Progress, count_phrase, report

logger = logging.getLogger("trimarr.plex")

_TVDB = re.compile(r"(?:thetvdb|tvdb)://(\d+)", re.IGNORECASE)
_METADATA_ID = re.compile(r"/library/metadata/(\d+)")


class PlexError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def tvdb_from_guids(guids: list[str]) -> int | None:
    for guid in guids:
        match = _TVDB.search(guid)
        if match:
            return int(match.group(1))
    return None


def extract_guids(item: dict) -> list[str]:
    found: list[str] = []
    for key in ("guid", "Guid", "guids"):
        value = item.get(key)
        if isinstance(value, str) and value:
            found.append(value)
        elif isinstance(value, dict):
            guid = value.get("id") or value.get("tag")
            if guid:
                found.append(str(guid))
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and entry:
                    found.append(entry)
                elif isinstance(entry, dict):
                    guid = entry.get("id") or entry.get("tag")
                    if guid:
                        found.append(str(guid))
    return found


def parse_season(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def rating_key_from_path(value: Any) -> str | None:
    """Plex id from a rating key or a `/library/metadata/{id}` path."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        return str(value)
    text = str(value).strip()
    if text.isdigit():
        number = int(text)
        if number <= 0:
            return None
        return str(number)
    match = _METADATA_ID.search(text)
    if match:
        return match.group(1)
    return None


def copy_direct_ids(item: dict) -> None:
    """Fill show and episode ids from key paths when Plex omitted the rating keys."""
    existing = item.get("grandparentRatingKey")
    if existing:
        normalized = rating_key_from_path(existing)
        if normalized:
            item["grandparentRatingKey"] = normalized
    else:
        show_id = rating_key_from_path(item.get("grandparentKey"))
        if show_id:
            item["grandparentRatingKey"] = show_id
    if not item.get("ratingKey"):
        episode_id = rating_key_from_path(item.get("key"))
        show_id = str(item.get("grandparentRatingKey") or "")
        if episode_id and episode_id != show_id:
            item["ratingKey"] = episode_id


def container_items(payload: dict, *keys: str) -> tuple[list[dict], int | None]:
    container = payload.get("MediaContainer", payload)
    if not isinstance(container, dict):
        return [], None
    total = container.get("totalSize")
    total_size = int(total) if total is not None else None
    for key in keys:
        raw = container.get(key)
        if raw is None:
            continue
        if isinstance(raw, dict):
            return [raw], total_size
        if isinstance(raw, list):
            return raw, total_size
    return [], total_size


def parse_show(item: dict) -> PlexShow:
    return PlexShow(
        rating_key=str(item.get("ratingKey")),
        title=str(item.get("title") or "Unknown show"),
        tvdb_id=tvdb_from_guids(extract_guids(item)),
    )


def _bump(current: datetime | None, new: datetime) -> datetime:
    if current is None or new > current:
        return new
    return current


def build_watch_index(
    shows: list[PlexShow],
    history_items: list[dict],
    sessions: list[dict],
    *,
    history_complete: bool,
    overrides: dict[int, str],
    now: datetime,
) -> WatchIndex:
    by_rating = {show.rating_key: show for show in shows}
    rating_to_tvdb: dict[str, int] = {}
    match_source: dict[int, str] = {}
    for show in shows:
        if show.tvdb_id is None:
            continue
        rating_to_tvdb[show.rating_key] = show.tvdb_id
        match_source.setdefault(show.tvdb_id, "tvdb")

    for tvdb_id, rating_key in overrides.items():
        key = str(rating_key)
        if key not in by_rating:
            logger.warning(
                "Override for TVDB %s points at Plex rating key %s, which is not in a TV library",
                tvdb_id,
                rating_key,
            )
            continue
        previous = rating_to_tvdb.get(key)
        rating_to_tvdb[key] = int(tvdb_id)
        match_source[int(tvdb_id)] = "override"
        if previous is not None and previous != int(tvdb_id):
            if previous not in rating_to_tvdb.values():
                match_source.pop(previous, None)

    activities: dict[int, ShowActivity] = {}
    ambiguous: list[str] = []
    titles: dict[str, list[int]] = {}
    for show in shows:
        tvdb_id = rating_to_tvdb.get(show.rating_key)
        if tvdb_id is None:
            continue
        bucket = titles.setdefault(show.title.strip(), [])
        if tvdb_id not in bucket:
            bucket.append(tvdb_id)

    def activity_for(tvdb_id: int) -> ShowActivity:
        return activities.setdefault(tvdb_id, ShowActivity())

    def hold_exact_title(item: dict, viewed: datetime) -> None:
        """A keyless play may keep a show, and it must not cause a search."""
        raw_title = item.get("grandparentTitle")
        if not isinstance(raw_title, str) or not raw_title.strip():
            return
        for tvdb_id in titles.get(raw_title.strip(), []):
            row = activity_for(tvdb_id)
            row.last_viewed_at = _bump(row.last_viewed_at, viewed)
            row.unknown_season_at = _bump(row.unknown_season_at, viewed)

    def note_play(item: dict, *, active: bool) -> None:
        copy_direct_ids(item)
        if item.get("type") not in (None, "episode"):
            return
        if item.get("type") is None and not item.get("grandparentRatingKey"):
            return
        title = str(item.get("grandparentTitle") or item.get("title") or "unknown episode")
        rating_key = item.get("grandparentRatingKey")
        season = parse_season(item.get("parentIndex"))
        viewed = now
        if not active and item.get("viewedAt") is not None:
            viewed = datetime.fromtimestamp(int(item["viewedAt"]), timezone.utc)
        if not rating_key:
            ambiguous.append(title)
            hold_exact_title(item, viewed)
            return
        tvdb_id = rating_to_tvdb.get(str(rating_key))
        if tvdb_id is None:
            return
        row = activity_for(tvdb_id)
        row.last_viewed_at = _bump(row.last_viewed_at, viewed)
        if active:
            user = item.get("User") or {}
            name = user.get("title") or user.get("name")
            if name:
                row.active_users.add(str(name))
        if season is None:
            row.unknown_season_at = _bump(row.unknown_season_at, viewed)
            return
        if active:
            row.active_seasons.add(season)
        if season > 0:
            row.season_last_viewed[season] = _bump(row.season_last_viewed.get(season), viewed)
        if season == 1:
            row.season1_last_viewed_at = _bump(row.season1_last_viewed_at, viewed)

    for item in history_items:
        note_play(item, active=False)
    for item in sessions:
        note_play(item, active=True)

    return WatchIndex(
        by_tvdb=activities,
        matched_tvdb=set(match_source),
        match_source=match_source,
        history_complete=history_complete,
        ambiguous_plays=ambiguous,
    )


class PlexClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float,
        library_timeout: float,
        page_size: int,
        max_items: int,
        hydrate_limit: int,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.library_timeout = library_timeout
        self.page_size = page_size
        self.max_items = max_items
        self.hydrate_limit = hydrate_limit
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "X-Plex-Token": token,
                "X-Plex-Product": "Trimarr",
                "X-Plex-Version": "1.0.0",
                "X-Plex-Client-Identifier": "trimarr",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, path: str, *, timeout: float | None = None, params: dict | None = None) -> dict:
        response = self._client.get(self._url(path), params=params, timeout=timeout or self.timeout)
        if response.status_code >= 400:
            raise PlexError(
                f"Plex GET {path} returned {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type and response.text.lstrip().startswith("<"):
            raise PlexError(f"Plex GET {path} returned XML instead of JSON")
        data = response.json()
        if not isinstance(data, dict):
            raise PlexError(f"Plex GET {path} returned an unexpected payload")
        return data

    def server_name(self) -> str:
        data = self._request("/identity")
        container = data.get("MediaContainer", data)
        return str(container.get("friendlyName") or container.get("machineIdentifier") or "plex")

    def list_shows(self, progress: Progress | None = None) -> list[PlexShow]:
        sections, _ = container_items(self._request("/library/sections"), "Directory")
        shows: list[PlexShow] = []
        seen: set[str] = set()
        for section in sections:
            if section.get("type") != "show":
                continue
            key = section.get("key")
            if not key:
                continue
            title = str(section.get("title") or "TV")
            report(
                progress,
                f'Plex library "{title}", {count_phrase(len(shows), "show")} so far.',
                done=len(shows),
            )
            payload = self._request(
                f"/library/sections/{key}/all",
                params={"includeGuids": 1},
                timeout=self.library_timeout,
            )
            items, _ = container_items(payload, "Metadata")
            for item in items:
                if item.get("type") not in (None, "show"):
                    continue
                show = parse_show(item)
                if show.rating_key in seen:
                    continue
                seen.add(show.rating_key)
                shows.append(show)
            report(
                progress,
                f'Plex library "{title}", {count_phrase(len(shows), "show")} so far.',
                done=len(shows),
            )
        logger.debug("Indexed %s Plex shows across TV libraries", len(shows))
        return shows

    def recent_history(
        self,
        cutoff: datetime,
        progress: Progress | None = None,
        window_days: int | None = None,
    ) -> tuple[list[dict], bool]:
        cutoff_epoch = int(cutoff.timestamp())
        kept: list[dict] = []
        start = 0
        complete = True
        window = f"{window_days}-day " if window_days is not None else ""

        def note() -> None:
            report(
                progress,
                f"Plex history, {count_phrase(len(kept), 'play')} inside the {window}window.",
                done=len(kept),
            )

        while True:
            payload = self._request(
                "/status/sessions/history/all",
                params={
                    "sort": "viewedAt:desc",
                    "includeGuids": 1,
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": self.page_size,
                },
            )
            items, total = container_items(payload, "Metadata", "Video")
            if not items:
                note()
                break
            stop = False
            hit_cap = False
            for item in items:
                viewed_raw = item.get("viewedAt")
                if viewed_raw is None:
                    # A row with no timestamp is not evidence that the show is idle.
                    kept.append(item)
                    continue
                if int(viewed_raw) < cutoff_epoch:
                    stop = True
                    break
                kept.append(item)
                if len(kept) >= self.max_items:
                    hit_cap = True
                    break
            note()
            if hit_cap:
                logger.warning(
                    "Stopped reading Plex history at %s items, still inside the watch window",
                    self.max_items,
                )
                return self._enrich(kept, progress), False
            if stop:
                break
            start += len(items)
            if total is not None and start >= total:
                break
            if len(items) < self.page_size:
                break
        return self._enrich(kept, progress), complete

    def active_sessions(self) -> list[dict]:
        payload = self._request("/status/sessions")
        items, _ = container_items(payload, "Metadata", "Video")
        return items

    def _enrich(self, items: list[dict], progress: Progress | None = None) -> list[dict]:
        """Resolve show ids for every episode row. A lookup cap is not applied.

        `hydrate_limit` remains on the client so older environment values still
        load. Stopping after that many lookups left later plays without a show
        id, which used to block deletes for the whole library.
        """
        for item in items:
            copy_direct_ids(item)
        already = sum(
            1
            for item in items
            if item.get("type") in (None, "episode") and item.get("grandparentRatingKey")
        )
        hydrated = 0
        lookups = 0
        logger.debug(
            "Resolving every episode id in this window (hydrate_limit=%s is not a cap)",
            self.hydrate_limit,
        )
        for item in items:
            if item.get("type") not in (None, "episode"):
                continue
            if item.get("grandparentRatingKey") and parse_season(item.get("parentIndex")) is not None:
                continue
            rating_key = item.get("ratingKey")
            if not rating_key:
                continue
            had_show = bool(item.get("grandparentRatingKey"))
            try:
                meta = self._metadata(str(rating_key))
            except PlexError as exc:
                if exc.status == 404:
                    item["historyDeleted"] = True
                    lookups += 1
                    report(
                        progress,
                        f"Filling in play details, {count_phrase(lookups, 'lookup')}.",
                        done=lookups,
                    )
                    continue
                raise
            lookups += 1
            report(
                progress,
                f"Filling in play details, {count_phrase(lookups, 'lookup')}.",
                done=lookups,
            )
            if not meta:
                continue
            if not item.get("grandparentRatingKey") and meta.get("grandparentRatingKey"):
                item["grandparentRatingKey"] = meta.get("grandparentRatingKey")
                if not had_show:
                    hydrated += 1
            if parse_season(item.get("parentIndex")) is None and meta.get("parentIndex") is not None:
                item["parentIndex"] = meta.get("parentIndex")
            item.setdefault("grandparentTitle", meta.get("grandparentTitle"))
            item.setdefault("type", meta.get("type") or "episode")
        unresolved = sum(
            1
            for item in items
            if item.get("type") == "episode" and not item.get("grandparentRatingKey")
        )
        logger.debug(
            "Plex history: %s plays in the window, %s already had a show id, %s hydrated, %s still unresolved",
            len(items),
            already,
            hydrated,
            unresolved,
        )
        return items

    def _metadata(self, rating_key: str) -> dict | None:
        payload = self._request(f"/library/metadata/{rating_key}")
        items, _ = container_items(payload, "Metadata", "Video")
        return items[0] if items else None


def empty_index() -> WatchIndex:
    return WatchIndex(
        by_tvdb={},
        matched_tvdb=set(),
        match_source={},
        history_complete=False,
        ambiguous_plays=[],
    )


def cutoff_for(now: datetime, window: timedelta) -> datetime:
    return now - window
