"""Sonarr API client and the small amount of parsing the engine needs."""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.http_retry import request_with_retry
from app.models import EpisodeSnapshot, SeasonSnapshot, SeriesSnapshot

logger = logging.getLogger("trimarr.sonarr")


class SonarrError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def parse_timestamp(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_air_date(value: str | None, now: datetime) -> bool:
    aired_at = parse_timestamp(value)
    if aired_at is None:
        return False
    return aired_at <= now.astimezone(timezone.utc)


def parse_episode(raw: dict, now: datetime) -> EpisodeSnapshot:
    file_id = raw.get("episodeFileId") or None
    if file_id == 0:
        file_id = None
    has_file = bool(raw.get("hasFile")) or file_id is not None
    episode_file = raw.get("episodeFile") or {}
    file_size = int(episode_file.get("size") or raw.get("size") or 0)
    return EpisodeSnapshot(
        id=int(raw["id"]),
        season_number=int(raw.get("seasonNumber") or 0),
        episode_number=int(raw.get("episodeNumber") or 0),
        monitored=bool(raw.get("monitored")),
        has_file=has_file,
        episode_file_id=int(file_id) if file_id else None,
        aired=parse_air_date(raw.get("airDateUtc"), now),
        file_size=file_size,
        file_added_at=parse_timestamp(episode_file.get("dateAdded")),
        air_date=parse_timestamp(raw.get("airDateUtc")),
    )


def parse_series(raw: dict, episode_rows: list[dict], now: datetime) -> SeriesSnapshot:
    episodes = tuple(parse_episode(row, now) for row in episode_rows)
    seasons = []
    for row in raw.get("seasons") or []:
        stats = row.get("statistics") or {}
        seasons.append(
            SeasonSnapshot(
                season_number=int(row["seasonNumber"]),
                monitored=bool(row.get("monitored")),
                size_on_disk=int(stats.get("sizeOnDisk") or 0),
            )
        )
    known = {season.season_number for season in seasons}
    for episode in episodes:
        if episode.season_number not in known:
            seasons.append(SeasonSnapshot(episode.season_number, False, 0))
            known.add(episode.season_number)
    year = raw.get("year") or None
    if year == 0:
        year = None
    tvdb = raw.get("tvdbId") or None
    if tvdb == 0:
        tvdb = None
    monitor_new = raw.get("monitorNewItems")
    status = str(raw.get("status") or "").strip().lower() or None
    return SeriesSnapshot(
        id=int(raw["id"]),
        title=str(raw.get("title") or f"Series {raw['id']}"),
        year=int(year) if year else None,
        tvdb_id=int(tvdb) if tvdb else None,
        path=str(raw.get("path") or ""),
        monitored=bool(raw.get("monitored")),
        monitor_new_items=str(monitor_new) if monitor_new else None,
        seasons=tuple(sorted(seasons, key=lambda season: season.season_number)),
        episodes=episodes,
        status=status,
        raw=raw,
    )


def series_update_payload(
    raw: dict,
    *,
    season_monitored: dict[int, bool],
    series_monitored: bool,
    monitor_new_items: str | None,
) -> dict:
    """Copy a Sonarr series and change only monitoring fields."""
    payload = copy.deepcopy(raw)
    for key in ("statistics", "previousAiring", "nextAiring", "episodesChanged"):
        payload.pop(key, None)
    payload["monitored"] = series_monitored
    if monitor_new_items is not None:
        payload["monitorNewItems"] = monitor_new_items
    seasons = payload.setdefault("seasons", [])
    present = set()
    for season in seasons:
        number = int(season["seasonNumber"])
        present.add(number)
        if number in season_monitored:
            season["monitored"] = season_monitored[number]
        season.pop("statistics", None)
        season.pop("images", None)
    for number, monitored in sorted(season_monitored.items()):
        if number not in present:
            seasons.append({"seasonNumber": number, "monitored": monitored})
    return payload


def series_tag_payload(raw: dict, tags: list[int]) -> dict:
    """Copy a Sonarr series and change only its tags.

    Season and series monitored flags stay as Sonarr sent them.
    """
    payload = copy.deepcopy(raw)
    for key in ("statistics", "previousAiring", "nextAiring", "episodesChanged"):
        payload.pop(key, None)
    payload["tags"] = [int(tag) for tag in tags]
    for season in payload.get("seasons") or []:
        season.pop("statistics", None)
        season.pop("images", None)
    return payload


def partition_ignored(series_rows: list[dict], tag_id: int) -> tuple[list[dict], list[dict]]:
    managed: list[dict] = []
    ignored: list[dict] = []
    for row in series_rows:
        tags = row.get("tags") or []
        if tag_id in tags:
            ignored.append(row)
        else:
            managed.append(row)
    return managed, ignored


def series_add_payload(looked_up: dict, *, profile_id: int, root: str, tag_id: int) -> dict:
    """Turn a Sonarr lookup into an add that monitors and searches season 1 only."""
    payload = copy.deepcopy(looked_up)
    for key in ("id", "statistics", "previousAiring", "nextAiring", "episodesChanged", "episodes"):
        payload.pop(key, None)
    payload["qualityProfileId"] = int(profile_id)
    payload["rootFolderPath"] = root
    payload["monitored"] = True
    payload["seasonFolder"] = True
    payload["tags"] = [int(tag_id)]
    payload["addOptions"] = {"monitor": "firstSeason", "searchForMissingEpisodes": True}
    seasons = payload.setdefault("seasons", [])
    present: set[int] = set()
    for season in seasons:
        number = int(season.get("seasonNumber") or 0)
        present.add(number)
        season["monitored"] = number == 1
        season.pop("statistics", None)
        season.pop("images", None)
    if 1 not in present:
        seasons.append({"seasonNumber": 1, "monitored": True})
    return payload


class SonarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = request_with_retry(self._client, method, self._url(path), **kwargs)
        if response.status_code >= 400:
            body = response.text[:500]
            raise SonarrError(
                f"Sonarr {method} {path} returned {response.status_code}: {body}",
                status=response.status_code,
            )
        if not response.content:
            return None
        return response.json()

    def status_version(self) -> str:
        data = self._request("GET", "/api/v3/system/status")
        return str((data or {}).get("version") or "unknown")

    def ensure_tag(self, label: str) -> int:
        tags = self._request("GET", "/api/v3/tag") or []
        for tag in tags:
            if str(tag.get("label", "")).lower() == label.lower():
                return int(tag["id"])
        created = self._request("POST", "/api/v3/tag", json={"label": label})
        logger.info("Created Sonarr tag %s", label)
        return int(created["id"])

    def list_series(self) -> list[dict]:
        return self._request("GET", "/api/v3/series") or []

    def root_folders(self) -> list[dict]:
        return self._request("GET", "/api/v3/rootfolder") or []

    def quality_profiles(self) -> list[dict]:
        return self._request("GET", "/api/v3/qualityprofile") or []

    def lookup_series(self, term: str) -> dict | None:
        rows = self._request("GET", "/api/v3/series/lookup", params={"term": term}) or []
        if not rows:
            return None
        return rows[0]

    def add_series(self, payload: dict) -> dict:
        return self._request("POST", "/api/v3/series", json=payload) or {}

    def earliest_arrivals(self, series_id: int, episode_seasons: dict[int, int]) -> dict[int, datetime]:
        """Earliest grab or import for each season, from Sonarr history."""
        from app.arrivals import arrivals_from_history

        records: list[dict] = []
        page = 1
        page_size = 200
        while page <= 20:
            data = self._request(
                "GET",
                "/api/v3/history",
                params={
                    "seriesId": series_id,
                    "page": page,
                    "pageSize": page_size,
                    "sortKey": "date",
                    "sortDirection": "ascending",
                },
            )
            if isinstance(data, list):
                batch = data
                total = len(data)
            else:
                batch = (data or {}).get("records") or []
                total = int((data or {}).get("totalRecords") or len(batch))
            records.extend(batch)
            if isinstance(data, list) or page * page_size >= total or not batch:
                break
            page += 1
        return arrivals_from_history(records, episode_seasons)

    def list_episodes(self, series_id: int) -> list[dict]:
        return (
            self._request(
                "GET",
                "/api/v3/episode",
                params={"seriesId": series_id, "includeEpisodeFile": True},
            )
            or []
        )

    def update_series(self, payload: dict) -> None:
        series_id = payload["id"]
        self._request("PUT", f"/api/v3/series/{series_id}", json=payload)

    def monitor_episodes(self, episode_ids: list[int], monitored: bool) -> None:
        for start in range(0, len(episode_ids), 100):
            chunk = episode_ids[start : start + 100]
            self._request(
                "PUT",
                "/api/v3/episode/monitor",
                json={"episodeIds": chunk, "monitored": monitored},
            )

    def delete_episode_files(self, episode_file_ids: list[int]) -> None:
        for start in range(0, len(episode_file_ids), 100):
            chunk = episode_file_ids[start : start + 100]
            try:
                self._request(
                    "DELETE",
                    "/api/v3/episodefile/bulk",
                    json={"episodeFileIds": chunk},
                )
            except SonarrError as exc:
                if exc.status == 404:
                    logger.info("Episode files were already gone")
                    continue
                raise

    def search_season(self, series_id: int, season_number: int) -> None:
        self._request(
            "POST",
            "/api/v3/command",
            json={
                "name": "SeasonSearch",
                "seriesId": series_id,
                "seasonNumber": season_number,
            },
        )

    def queued_seasons(self) -> set[tuple[int, int]]:
        keys: set[tuple[int, int]] = set()
        page = 1
        page_size = 200
        while page <= 50:
            data = self._request(
                "GET",
                "/api/v3/queue",
                params={"page": page, "pageSize": page_size, "includeSeries": False},
            )
            if isinstance(data, list):
                records = data
                total = len(data)
            else:
                records = (data or {}).get("records") or []
                total = int((data or {}).get("totalRecords") or len(records))
            for record in records:
                series_id = record.get("seriesId")
                season = record.get("seasonNumber")
                if series_id is None or season is None:
                    continue
                keys.add((int(series_id), int(season)))
            if isinstance(data, list) or page * page_size >= total or not records:
                break
            page += 1
        return keys

    def running_searches(self) -> set[tuple[int, int]]:
        commands = self._request("GET", "/api/v3/command") or []
        keys: set[tuple[int, int]] = set()
        finished = {"completed", "failed", "cancelled", "aborted"}
        for command in commands:
            if command.get("name") != "SeasonSearch":
                continue
            if str(command.get("status") or "").lower() in finished:
                continue
            body = command.get("body") or command
            series_id = body.get("seriesId", command.get("seriesId"))
            season = body.get("seasonNumber", command.get("seasonNumber"))
            if series_id is None or season is None:
                continue
            keys.add((int(series_id), int(season)))
        return keys
