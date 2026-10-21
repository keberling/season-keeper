"""Fixtures for policy and engine tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import EpisodeSnapshot, PolicySettings, SeasonSnapshot, SeriesSnapshot, ShowWatch

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def policy(**overrides) -> PolicySettings:
    values = dict(
        watch_window=timedelta(days=90),
        keep_seasons=frozenset({1}),
        dry_run=True,
        search_cooldown=timedelta(hours=6),
        monitor_new_when_active="all",
        monitor_new_when_idle="none",
    )
    values.update(overrides)
    return PolicySettings(**values)


def episode(
    episode_id: int,
    season: int,
    number: int,
    *,
    monitored: bool = True,
    file_id: int | None = None,
    aired: bool = True,
    file_size: int = 0,
    file_added_at: datetime | None = None,
    air_date: datetime | None = None,
) -> EpisodeSnapshot:
    return EpisodeSnapshot(
        id=episode_id,
        season_number=season,
        episode_number=number,
        monitored=monitored,
        has_file=file_id is not None,
        episode_file_id=file_id,
        aired=aired,
        file_size=file_size,
        file_added_at=file_added_at,
        air_date=air_date,
    )


def make_series(
    episodes: list[EpisodeSnapshot],
    *,
    sonarr_id: int = 1,
    title: str = "Example",
    year: int | None = 2020,
    tvdb_id: int | None = 100,
    season_sizes: dict[int, int] | None = None,
    season_monitored: dict[int, bool] | None = None,
    monitor_new: str | None = "all",
    series_monitored: bool = True,
) -> SeriesSnapshot:
    sizes = season_sizes or {}
    numbers = sorted({item.season_number for item in episodes} | set(sizes))
    seasons = tuple(
        SeasonSnapshot(
            number,
            True if season_monitored is None else season_monitored.get(number, True),
            sizes.get(number, 0),
        )
        for number in numbers
    )
    raw = {
        "id": sonarr_id,
        "title": title,
        "year": year or 0,
        "path": f"/tv/{title}",
        "qualityProfileId": 1,
        "seasonFolder": True,
        "monitored": series_monitored,
        "tvdbId": tvdb_id or 0,
        "tags": [7],
        "seasons": [
            {
                "seasonNumber": season.season_number,
                "monitored": season.monitored,
                "statistics": {"sizeOnDisk": season.size_on_disk},
            }
            for season in seasons
        ],
    }
    if monitor_new is not None:
        raw["monitorNewItems"] = monitor_new
    return SeriesSnapshot(
        id=sonarr_id,
        title=title,
        year=year,
        tvdb_id=tvdb_id,
        path=f"/tv/{title}",
        monitored=series_monitored,
        monitor_new_items=monitor_new,
        seasons=seasons,
        episodes=tuple(episodes),
        raw=raw,
    )


def watch(**overrides) -> ShowWatch:
    values = dict(
        matched=True,
        match_source="tvdb",
        last_viewed_at=None,
        season1_last_viewed_at=None,
        active_seasons=frozenset(),
        active_users=(),
        unknown_season_at=None,
        history_reliable=True,
    )
    values.update(overrides)
    return ShowWatch(**values)


class FakeSonarr:
    def __init__(self) -> None:
        self.updated: list[dict] = []
        self.monitored: list[tuple[list[int], bool]] = []
        self.deleted: list[int] = []
        self.searches: list[tuple[int, int]] = []

    def update_series(self, payload: dict) -> None:
        self.updated.append(payload)

    def monitor_episodes(self, episode_ids: list[int], monitored: bool) -> None:
        self.monitored.append((list(episode_ids), monitored))

    def delete_episode_files(self, episode_file_ids: list[int]) -> None:
        self.deleted.extend(episode_file_ids)

    def search_season(self, series_id: int, season_number: int) -> None:
        self.searches.append((series_id, season_number))
