from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from app.engine import SafetyError, apply_decision, assert_safe, execute
from app.inventory import build_inventory
from app.models import WatchIndex
from app.policy import decide, plan_manual_restore
from app.sonarr import parse_series, series_update_payload
from app.state import State
from tests.builders import NOW, FakeSonarr, episode, make_series, policy, watch


def _state(tmp_path) -> State:
    return State(tmp_path / "state.sqlite")


def office():
    return make_series(
        [
            episode(1, 1, 1, file_id=11, file_size=1000),
            episode(2, 2, 1, file_id=21, file_size=2000),
            episode(3, 2, 2, file_id=22, file_size=3000),
        ],
        season_sizes={1: 1_000, 2: 8_000},
        title="The Office",
    )


def test_inventory_counts_episodes_files_and_bytes():
    decision = decide(office(), watch(), NOW, policy())
    rows, totals = build_inventory(office(), frozenset({1}), decision)

    by_season = {row.season_number: row for row in rows}
    assert by_season[1].episode_count == 1
    assert by_season[1].file_count == 1
    assert by_season[1].size_on_disk == 1_000
    assert by_season[1].disposition == "keep"
    assert by_season[2].episode_count == 2
    assert by_season[2].file_count == 2
    assert by_season[2].size_on_disk == 8_000
    assert by_season[2].disposition == "delete"
    assert totals.later_bytes == 8_000
    assert totals.later_episode_count == 2
    assert totals.kept_bytes == 1_000


def test_preview_records_savings_and_calls_nothing(tmp_path):
    sonarr = FakeSonarr()
    series = office()
    result = apply_decision(
        series,
        decide(series, watch(), NOW, policy()),
        watch=watch(),
        actions=sonarr,
        state=_state(tmp_path),
        settings=policy(dry_run=True),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )

    assert result.action == "prune"
    assert result.applied is False
    assert result.bytes_to_free == 8_000
    assert result.later_episode_count == 2
    assert result.later_file_count == 2
    assert result.kept_episode_count == 1
    assert result.reason.startswith("Preview only.")
    assert sonarr.deleted == []
    assert sonarr.updated == []
    assert sonarr.monitored == []


def test_live_delete_removes_later_files_only(tmp_path):
    sonarr = FakeSonarr()
    series = office()
    result = apply_decision(
        series,
        decide(series, watch(), NOW, policy()),
        watch=watch(),
        actions=sonarr,
        state=_state(tmp_path),
        settings=policy(dry_run=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )

    assert result.applied is True
    assert sonarr.deleted == [21, 22]
    assert 11 not in sonarr.deleted
    assert sonarr.updated[0]["seasons"][0]["monitored"] is True
    assert any(season["seasonNumber"] == 2 and season["monitored"] is False for season in sonarr.updated[0]["seasons"])
    assert "statistics" not in sonarr.updated[0]
    unmonitor = [call for call in sonarr.monitored if call[1] is False][0][0]
    assert 1 not in unmonitor


def test_refuses_a_file_that_also_belongs_to_season_one(tmp_path):
    series = make_series(
        [
            episode(1, 1, 1, file_id=50),
            episode(2, 2, 1, file_id=50),
        ],
        season_sizes={1: 100, 2: 100},
    )
    decision = decide(series, watch(), NOW, policy())
    sonarr = FakeSonarr()
    result = apply_decision(
        series,
        decision,
        watch=watch(),
        actions=sonarr,
        state=_state(tmp_path),
        settings=policy(dry_run=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    assert result.error
    assert "season" in result.error.lower()
    assert sonarr.deleted == []
    assert sonarr.updated == []


def test_restore_searches_once_then_cools_down(tmp_path):
    series = make_series(
        [episode(1, 1, 1, file_id=11), episode(2, 2, 1, aired=True)],
        season_monitored={1: True, 2: False},
    )
    viewing = watch(season1_last_viewed_at=NOW - timedelta(hours=1), last_viewed_at=NOW - timedelta(hours=1))
    decision = decide(series, viewing, NOW, policy())
    state = _state(tmp_path)
    sonarr = FakeSonarr()
    settings = policy(dry_run=False)
    first = apply_decision(
        series,
        decision,
        watch=viewing,
        actions=sonarr,
        state=state,
        settings=settings,
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    assert first.searches_ran == [2]
    assert sonarr.searches == [(1, 2)]

    sonarr.searches.clear()
    second = apply_decision(
        series,
        decision,
        watch=viewing,
        actions=sonarr,
        state=state,
        settings=settings,
        now=NOW + timedelta(hours=1),
        queue=set(),
        running_searches=set(),
    )
    assert second.searches_ran == []
    assert sonarr.searches == []
    assert any("cooldown" in note for note in second.searches_waiting)


def test_manual_restore_ignores_cooldown(tmp_path):
    series = make_series(
        [episode(1, 1, 1, file_id=11), episode(2, 2, 1, aired=True)],
        season_monitored={1: True, 2: False},
    )
    state = _state(tmp_path)
    state.mark_search(series.id, 2, NOW)
    sonarr = FakeSonarr()
    result = apply_decision(
        series,
        plan_manual_restore(series, policy(), NOW),
        watch=watch(),
        actions=sonarr,
        state=state,
        settings=policy(dry_run=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
        honor_cooldown=False,
        announce_preview=False,
    )
    assert result.searches_ran == [2]


def test_execute_skips_an_unmatched_series(tmp_path):
    series = office()
    index = WatchIndex(
        by_tvdb={},
        matched_tvdb=set(),
        match_source={},
        history_complete=True,
        ambiguous_plays=[],
    )
    report = execute([series], index, FakeSonarr(), _state(tmp_path), policy(), NOW, set(), set())
    assert report.results[0].action == "skip"
    assert report.results[0].later_bytes == 8_000
    assert report.to_dict()["later_bytes"] == 8_000


def test_payload_strips_statistics_and_sets_monitoring():
    series = office()
    payload = series_update_payload(
        series.raw,
        season_monitored={1: True, 2: False},
        series_monitored=True,
        monitor_new_items="none",
    )
    assert "statistics" not in payload
    assert payload["monitorNewItems"] == "none"
    flags = {season["seasonNumber"]: season["monitored"] for season in payload["seasons"]}
    assert flags == {1: True, 2: False}
    assert "statistics" not in payload["seasons"][0]


def test_parse_series_reads_file_size():
    raw = {
        "id": 9,
        "title": "Example",
        "year": 2020,
        "tvdbId": 100,
        "path": "/tv/Example",
        "monitored": True,
        "monitorNewItems": "all",
        "seasons": [{"seasonNumber": 1, "monitored": True, "statistics": {"sizeOnDisk": 10}}],
    }
    episodes = [
        {
            "id": 1,
            "seasonNumber": 1,
            "episodeNumber": 1,
            "monitored": True,
            "hasFile": True,
            "episodeFileId": 4,
            "airDateUtc": "2020-01-01T00:00:00Z",
            "episodeFile": {"size": 4321},
        }
    ]
    parsed = parse_series(raw, episodes, NOW)
    assert parsed.episodes[0].file_size == 4321
    assert parsed.episodes[0].has_file is True


def test_assert_safe_blocks_kept_season_delete():
    series = office()
    decision = decide(series, watch(), NOW, policy())
    bad = replace(decision, delete_file_ids=(11,))
    try:
        assert_safe(series, bad, frozenset({1}))
    except Exception as exc:
        assert "Refusing" in str(exc)
    else:
        raise AssertionError("expected a safety error")


def test_page_has_the_preview_and_one_show_actions():
    page = (Path(__file__).resolve().parents[1] / "app" / "status_page.html").read_text(encoding="utf-8")
    for text in (
        "Preview mode",
        "Test delete on this show",
        "Download later seasons",
        "Season 1",
        "Would free",
        "Available to save",
        "Ignore",
        "delete-later",
        "download-later",
        "/ignore",
    ):
        assert text in page
