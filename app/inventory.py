"""Episode counts and sizes for the status page."""

from __future__ import annotations

from app.models import Decision, InventoryTotals, SeasonRow, SeriesSnapshot


def season_disk_bytes(series: SeriesSnapshot, season_number: int) -> int:
    for season in series.seasons:
        if season.season_number == season_number and season.size_on_disk:
            return season.size_on_disk
    sizes: dict[int, int] = {}
    for episode in series.episodes:
        if episode.season_number != season_number or not episode.episode_file_id:
            continue
        if episode.file_size:
            sizes[episode.episode_file_id] = episode.file_size
    return sum(sizes.values())


def build_inventory(
    series: SeriesSnapshot,
    keep_seasons: frozenset[int],
    decision: Decision,
) -> tuple[list[SeasonRow], InventoryTotals]:
    keep = set(keep_seasons) | {1}
    by_season: dict[int, list] = {}
    for episode in series.episodes:
        by_season.setdefault(episode.season_number, []).append(episode)
    numbers = sorted(
        number
        for number in (set(by_season) | {season.season_number for season in series.seasons})
        if number != 0
    )
    search = set(decision.search_seasons)
    delete = set(decision.delete_seasons)
    plans = dict(decision.season_plans)
    rows: list[SeasonRow] = []
    for number in numbers:
        episodes = by_season.get(number, [])
        file_ids = {episode.episode_file_id for episode in episodes if episode.episode_file_id}
        file_count = len(file_ids) if file_ids else sum(1 for episode in episodes if episode.has_file)
        missing_aired = sum(
            1 for episode in episodes if episode.aired and not episode.has_file
        )
        monitored = next(
            (season.monitored for season in series.seasons if season.season_number == number),
            any(episode.monitored for episode in episodes),
        )
        kept = number in keep
        if number in decision.protected_seasons:
            disposition = "held"
        elif kept:
            disposition = "keep"
        elif number in search:
            disposition = "download"
        elif decision.action == "prune" and number not in keep:
            disposition = "delete" if file_count or number in delete else "unmonitor"
        elif decision.action == "restore" and number not in keep:
            disposition = "monitor"
        else:
            disposition = "leave"
        rows.append(
            SeasonRow(
                season_number=number,
                episode_count=len(episodes),
                file_count=file_count,
                missing_aired=missing_aired,
                size_on_disk=season_disk_bytes(series, number),
                monitored=monitored,
                kept=kept,
                disposition=disposition,
                plan=plans.get(number, ""),
            )
        )
    return rows, InventoryTotals.from_rows(rows)
