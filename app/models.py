"""Data the policy and the status page share."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class EpisodeSnapshot:
    id: int
    season_number: int
    episode_number: int
    monitored: bool
    has_file: bool
    episode_file_id: int | None
    aired: bool
    file_size: int = 0
    file_added_at: datetime | None = None
    air_date: datetime | None = None


@dataclass(frozen=True)
class SeasonSnapshot:
    season_number: int
    monitored: bool
    size_on_disk: int


@dataclass
class SeriesSnapshot:
    id: int
    title: str
    year: int | None
    tvdb_id: int | None
    path: str
    monitored: bool
    monitor_new_items: str | None
    seasons: tuple[SeasonSnapshot, ...]
    episodes: tuple[EpisodeSnapshot, ...]
    status: str | None = None
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def label(self) -> str:
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title


@dataclass
class ShowActivity:
    last_viewed_at: datetime | None = None
    season1_last_viewed_at: datetime | None = None
    active_seasons: set[int] = field(default_factory=set)
    active_users: set[str] = field(default_factory=set)
    season_last_viewed: dict[int, datetime] = field(default_factory=dict)
    unknown_season_at: datetime | None = None
    watchlisted_at: datetime | None = None
    watchlisted_by: str | None = None


@dataclass(frozen=True)
class PlexShow:
    rating_key: str
    title: str
    tvdb_id: int | None


@dataclass
class WatchIndex:
    by_tvdb: dict[int, ShowActivity]
    matched_tvdb: set[int]
    match_source: dict[int, str]
    history_complete: bool
    ambiguous_plays: list[str]

    def reliable_for_prune(self) -> bool:
        """True when history paging covered the whole watch window.

        Plays with no show id stay in ambiguous_plays. They do not flip this
        bit, and they are never used as a reason to delete.
        """
        return self.history_complete


@dataclass(frozen=True)
class ShowWatch:
    matched: bool
    match_source: str | None
    last_viewed_at: datetime | None
    season1_last_viewed_at: datetime | None
    active_seasons: frozenset[int]
    active_users: tuple[str, ...]
    unknown_season_at: datetime | None
    history_reliable: bool
    watchlisted_at: datetime | None = None
    watchlisted_by: str | None = None
    season_plays: tuple[tuple[int, datetime], ...] = ()


@dataclass(frozen=True)
class PolicySettings:
    watch_window: timedelta
    keep_seasons: frozenset[int]
    dry_run: bool
    search_cooldown: timedelta
    monitor_new_when_active: str
    monitor_new_when_idle: str
    require_delete_approval: bool = True
    live_series_ids: frozenset[int] = field(default_factory=frozenset)
    new_season_grace: timedelta = timedelta(days=90)
    keep_current_season: bool = True
    manual_pick_overrides_protection: bool = False


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    season_monitored: dict[int, bool] | None
    series_monitored: bool | None
    monitor_new_items: str | None
    monitor_episode_ids: tuple[int, ...]
    unmonitor_episode_ids: tuple[int, ...]
    delete_file_ids: tuple[int, ...]
    delete_seasons: tuple[int, ...]
    search_seasons: tuple[int, ...]
    bytes_to_free: int
    protected_seasons: tuple[int, ...] = ()
    kept_seasons: tuple[int, ...] = ()
    ensure_monitored_seasons: tuple[int, ...] = ()
    season_plans: tuple[tuple[int, str], ...] = ()
    protection_note: str = ""
    protection_result: str = ""


@dataclass(frozen=True)
class InventoryTotals:
    episode_count: int
    file_count: int
    size_on_disk: int
    kept_episode_count: int
    kept_file_count: int
    kept_bytes: int
    later_episode_count: int
    later_file_count: int
    later_bytes: int

    @classmethod
    def from_rows(cls, rows: list[SeasonRow]) -> InventoryTotals:
        def tally(selected: list[SeasonRow]) -> tuple[int, int, int]:
            return (
                sum(row.episode_count for row in selected),
                sum(row.file_count for row in selected),
                sum(row.size_on_disk for row in selected),
            )

        kept = [row for row in rows if row.kept]
        later = [row for row in rows if not row.kept]
        episodes, files, size = tally(rows)
        kept_episodes, kept_files, kept_bytes = tally(kept)
        later_episodes, later_files, later_bytes = tally(later)
        return cls(
            episode_count=episodes,
            file_count=files,
            size_on_disk=size,
            kept_episode_count=kept_episodes,
            kept_file_count=kept_files,
            kept_bytes=kept_bytes,
            later_episode_count=later_episodes,
            later_file_count=later_files,
            later_bytes=later_bytes,
        )


@dataclass(frozen=True)
class SeasonRow:
    season_number: int
    episode_count: int
    file_count: int
    missing_aired: int
    size_on_disk: int
    monitored: bool
    kept: bool
    disposition: str
    plan: str = ""

    def to_dict(self) -> dict:
        return {
            "season_number": self.season_number,
            "episode_count": self.episode_count,
            "file_count": self.file_count,
            "missing_aired": self.missing_aired,
            "size_on_disk": self.size_on_disk,
            "monitored": self.monitored,
            "kept": self.kept,
            "disposition": self.disposition,
            "plan": self.plan,
        }


@dataclass
class SeriesResult:
    sonarr_id: int
    title: str
    year: int | None
    tvdb_id: int | None
    path: str
    plex_matched: bool
    match_source: str | None
    action: str
    reason: str
    last_viewed_at: str | None
    season1_last_viewed_at: str | None
    active_seasons: list[int]
    delete_seasons: list[int]
    search_seasons: list[int]
    searches_ran: list[int]
    searches_waiting: list[str]
    bytes_to_free: int
    dry_run: bool
    error: str | None
    seasons: list[SeasonRow] = field(default_factory=list)
    episode_count: int = 0
    file_count: int = 0
    size_on_disk: int = 0
    kept_episode_count: int = 0
    kept_file_count: int = 0
    kept_bytes: int = 0
    later_episode_count: int = 0
    later_file_count: int = 0
    later_bytes: int = 0
    files_targeted: int = 0
    applied: bool = False
    pending_approval: bool = False
    dismissed: bool = False
    in_live_scope: bool = False
    protected_seasons: list[int] = field(default_factory=list)
    queued_delete: bool = False
    protection_note: str = ""
    protection_result: str = ""

    def to_dict(self) -> dict:
        return {
            "sonarr_id": self.sonarr_id,
            "title": self.title,
            "year": self.year,
            "tvdb_id": self.tvdb_id,
            "path": self.path,
            "plex_matched": self.plex_matched,
            "match_source": self.match_source,
            "action": self.action,
            "reason": self.reason,
            "last_viewed_at": self.last_viewed_at,
            "season1_last_viewed_at": self.season1_last_viewed_at,
            "active_seasons": self.active_seasons,
            "delete_seasons": self.delete_seasons,
            "search_seasons": self.search_seasons,
            "searches_ran": self.searches_ran,
            "searches_waiting": self.searches_waiting,
            "bytes_to_free": self.bytes_to_free,
            "dry_run": self.dry_run,
            "error": self.error,
            "seasons": [season.to_dict() for season in self.seasons],
            "episode_count": self.episode_count,
            "file_count": self.file_count,
            "size_on_disk": self.size_on_disk,
            "kept_episode_count": self.kept_episode_count,
            "kept_file_count": self.kept_file_count,
            "kept_bytes": self.kept_bytes,
            "later_episode_count": self.later_episode_count,
            "later_file_count": self.later_file_count,
            "later_bytes": self.later_bytes,
            "files_targeted": self.files_targeted,
            "applied": self.applied,
            "pending_approval": self.pending_approval,
            "dismissed": self.dismissed,
            "in_live_scope": self.in_live_scope,
            "protected_seasons": list(self.protected_seasons),
            "queued_delete": self.queued_delete,
            "protection_note": self.protection_note,
            "protection_result": self.protection_result,
        }


@dataclass
class RunReport:
    started_at: str
    finished_at: str
    dry_run: bool
    history_complete: bool
    ambiguous_plays: list[str]
    results: list[SeriesResult]
    error: str | None

    def to_dict(self) -> dict:
        series = [result.to_dict() for result in self.results]
        summary = summarize_dicts(series)
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "history_complete": self.history_complete,
            "ambiguous_plays": self.ambiguous_plays,
            "error": self.error,
            "series": series,
            **summary,
        }


def summarize_dicts(rows: list[dict]) -> dict:
    counts = {"restore": 0, "hold": 0, "prune": 0, "skip": 0, "error": 0}
    later_bytes = 0
    kept_bytes = 0
    bytes_to_free = 0
    later_episodes = 0
    kept_episodes = 0
    later_files = 0
    protected_seasons = 0
    for row in rows:
        if row.get("error"):
            counts["error"] += 1
        else:
            action = row.get("action")
            if action in counts:
                counts[action] += 1
        later_bytes += int(row.get("later_bytes") or 0)
        kept_bytes += int(row.get("kept_bytes") or 0)
        later_episodes += int(row.get("later_episode_count") or 0)
        kept_episodes += int(row.get("kept_episode_count") or 0)
        later_files += int(row.get("later_file_count") or 0)
        if row.get("action") == "prune" and not row.get("error"):
            bytes_to_free += int(row.get("bytes_to_free") or 0)
        protected_seasons += len(row.get("protected_seasons") or [])
    return {
        "counts": counts,
        "later_bytes": later_bytes,
        "kept_bytes": kept_bytes,
        "bytes_to_free": bytes_to_free,
        "later_episode_count": later_episodes,
        "kept_episode_count": kept_episodes,
        "later_file_count": later_files,
        "protected_season_count": protected_seasons,
    }
