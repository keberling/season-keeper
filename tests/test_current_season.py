"""Continuing shows keep the season that is still airing."""

import json
from datetime import timedelta
from pathlib import Path

from app.config import Settings, load_settings
from app.engine import apply_decision
from app.policy import decide, plan_manual_prune
from app.sonarr import parse_series
from app.state import State
from tests.builders import NOW, FakeSonarr, episode, make_series, policy, watch
from tests.test_admin import _quiet_env

OLD = NOW - timedelta(days=400)
OCT_14 = NOW - timedelta(days=6)
STALE = NOW - timedelta(days=90, seconds=1)


def _show(episodes, *, status="continuing", season_monitored=None, sizes=None):
    return make_series(
        episodes,
        title="Long Run",
        status=status,
        season_monitored=season_monitored,
        season_sizes=sizes or {},
    )


def test_continuing_show_keeps_season_one_and_the_fresh_current_season(tmp_path):
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 5, 1, file_id=51, file_added_at=OLD, air_date=OLD),
            episode(3, 12, 1, file_id=121, file_added_at=OLD, air_date=NOW - timedelta(days=20)),
            episode(4, 12, 2, file_id=122, file_added_at=OLD, air_date=OCT_14),
            episode(5, 0, 1, file_id=91, file_added_at=OCT_14, air_date=OCT_14),
        ],
        sizes={1: 100, 5: 500, 12: 800, 0: 50},
    )
    decision = decide(series, watch(), NOW, policy())

    assert decision.action == "prune"
    assert decision.delete_file_ids == (51,)
    assert decision.delete_seasons == (5,)
    assert 12 not in decision.delete_seasons
    assert 0 not in decision.delete_seasons
    assert 91 not in decision.delete_file_ids
    assert decision.protected_seasons == ()
    assert decision.kept_seasons == (12,)
    assert decision.season_monitored[12] is True
    assert decision.season_monitored[5] is False
    assert decision.season_monitored[0] is False
    assert decision.reason == (
        "No Plex plays in 90 days. Unmonitor and delete 1 episode file in seasons 5 (500 B). "
        "Still airing: keeping season 1 and season 12 (last episode aired Oct 14)."
    )
    assert "New season" not in decision.reason
    assert "Season 1, 12 stays." not in decision.reason

    sonarr = FakeSonarr()
    result = apply_decision(
        series,
        decision,
        watch=watch(),
        actions=sonarr,
        state=State(tmp_path / "state.sqlite"),
        settings=policy(dry_run=True),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    by_season = {row.season_number: row for row in result.seasons}
    assert by_season[12].kept is True
    assert by_season[12].disposition == "keep"
    assert by_season[5].disposition == "delete"
    assert 0 not in by_season
    assert result.bytes_to_free == 500
    assert result.kept_bytes == 900


def test_ended_and_unknown_shows_still_prune_the_latest_season():
    episodes = [
        episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
        episode(2, 12, 1, file_id=121, file_added_at=OLD, air_date=OCT_14),
    ]
    ended = decide(_show(episodes, status="ended"), watch(), NOW, policy())
    assert ended.action == "prune"
    assert ended.delete_file_ids == (121,)
    assert "Still airing" not in ended.reason
    assert "Season 1 stays." in ended.reason

    unknown = decide(_show(episodes, status=None), watch(), NOW, policy())
    assert unknown.delete_file_ids == (121,)
    assert unknown.kept_seasons == ()

    upcoming = decide(_show(episodes, status="upcoming"), watch(), NOW, policy())
    assert upcoming.delete_file_ids == (121,)


def test_a_stale_current_season_can_be_pruned():
    inside = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 12, 1, file_id=121, file_added_at=OLD, air_date=NOW - timedelta(days=90)),
        ]
    )
    held = decide(inside, watch(), NOW, policy())
    assert held.action == "hold"
    assert held.delete_file_ids == ()
    assert "last episode aired Jul 22" in held.reason

    stale = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 12, 1, file_id=121, file_added_at=OLD, air_date=STALE),
        ]
    )
    pruned = decide(stale, watch(), NOW, policy())
    assert pruned.action == "prune"
    assert pruned.delete_file_ids == (121,)
    assert pruned.kept_seasons == ()
    assert "Still airing" not in pruned.reason


def test_a_new_season_that_starts_airing_becomes_the_current_one():
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 12, 1, file_id=121, file_added_at=OLD, air_date=NOW - timedelta(days=120)),
            episode(3, 13, 1, monitored=False, aired=True, air_date=NOW - timedelta(days=2)),
        ],
        season_monitored={1: True, 12: True, 13: False},
        sizes={1: 100, 12: 700},
    )
    decision = decide(series, watch(), NOW, policy())

    assert decision.action == "prune"
    assert decision.delete_file_ids == (121,)
    assert decision.delete_seasons == (12,)
    assert 13 not in decision.delete_seasons
    assert decision.kept_seasons == (13,)
    assert decision.season_monitored[13] is True
    assert decision.season_monitored[12] is False
    assert decision.monitor_episode_ids == (3,)
    assert decision.ensure_monitored_seasons == (13,)
    assert decision.reason == (
        "No Plex plays in 90 days. Unmonitor and delete 1 episode file in seasons 12 (700 B). "
        "Still airing: keeping season 1 and season 13 (last episode aired Oct 18)."
    )


def test_an_unaired_future_season_does_not_become_current():
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 12, 1, file_id=121, file_added_at=OLD, air_date=OCT_14),
            episode(3, 13, 1, monitored=True, aired=False, air_date=NOW + timedelta(days=7)),
        ],
        season_monitored={1: True, 12: True, 13: True},
    )
    decision = decide(series, watch(), NOW, policy())

    assert decision.action == "hold"
    assert decision.kept_seasons == (12,)
    assert 13 not in decision.kept_seasons
    assert 121 not in decision.delete_file_ids
    assert decision.protected_seasons == (13,)
    assert "keeping season 1 and season 12" in decision.reason
    assert "season 13" not in decision.reason.split("Still airing", 1)[-1]


def test_middle_seasons_of_an_airing_show_are_still_pruned():
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 5, 1, file_id=51, file_added_at=OLD, air_date=NOW - timedelta(days=1)),
            episode(3, 12, 1, file_id=121, file_added_at=OLD, air_date=OCT_14),
        ],
        sizes={5: 400, 12: 800},
    )
    decision = decide(series, watch(), NOW, policy())
    assert decision.delete_file_ids == (51,)
    assert decision.kept_seasons == (12,)
    assert "season 5" not in decision.reason.split("Still airing")[-1]


def test_grace_on_the_current_season_is_one_reason_not_two():
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 5, 1, file_id=51, file_added_at=OLD, air_date=OLD),
            episode(3, 8, 1, file_id=81, file_added_at=NOW - timedelta(days=3), air_date=OLD),
            episode(4, 12, 1, file_id=121, file_added_at=NOW - timedelta(days=10), air_date=OLD),
        ],
        sizes={5: 200, 8: 300, 12: 900},
    )
    decision = decide(series, watch(), NOW, policy())

    assert decision.delete_file_ids == (51,)
    assert 121 not in decision.delete_file_ids
    assert 81 not in decision.delete_file_ids
    assert decision.protected_seasons == (8,)
    assert decision.kept_seasons == (12,)
    assert "New season 12" not in decision.reason
    assert "New season 8 added 2026-10-17, protected until 2027-01-15." in decision.reason
    assert "Still airing: keeping season 1 and season 12 (added 2026-10-10)." in decision.reason

    fresh_air = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 12, 1, file_id=121, file_added_at=NOW - timedelta(days=4), air_date=OCT_14),
        ]
    )
    both = decide(fresh_air, watch(), NOW, policy())
    assert both.action == "hold"
    assert both.protected_seasons == ()
    assert both.reason == "Still airing: keeping season 1 and season 12 (last episode aired Oct 14)."


def test_turning_the_toggle_off_prunes_the_current_season_and_keeps_grace():
    episodes = [
        episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
        episode(2, 12, 1, file_id=121, file_added_at=OLD, air_date=OCT_14),
    ]
    off = decide(_show(episodes), watch(), NOW, policy(keep_current_season=False))
    assert off.delete_file_ids == (121,)
    assert "Still airing" not in off.reason

    grace_only = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 12, 1, file_id=121, file_added_at=NOW - timedelta(days=2), air_date=OLD),
        ]
    )
    held = decide(grace_only, watch(), NOW, policy(keep_current_season=False))
    assert held.action == "hold"
    assert held.protected_seasons == (12,)
    assert held.reason == "New season 12 added 2026-10-18, protected until 2027-01-16."
    assert "Still airing" not in held.reason


def test_a_chosen_delete_can_still_remove_the_current_season():
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 12, 1, file_id=121, file_added_at=OLD, air_date=OCT_14),
        ]
    )
    decision = plan_manual_prune(series, policy())
    assert decision.action == "prune"
    assert decision.delete_file_ids == (121,)
    assert decision.reason.startswith("You chose this show.")


def test_a_season_one_play_still_searches_the_current_season():
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 12, 1, aired=True, air_date=OCT_14),
        ],
        season_monitored={1: True, 12: False},
    )
    decision = decide(
        series,
        watch(season1_last_viewed_at=NOW - timedelta(days=1), last_viewed_at=NOW - timedelta(days=1)),
        NOW,
        policy(),
    )
    assert decision.action == "restore"
    assert decision.search_seasons == (12,)


def test_the_current_season_is_monitored_while_deletes_wait_for_approval(tmp_path):
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 5, 1, file_id=51, file_added_at=OLD, air_date=OLD),
            episode(3, 12, 1, monitored=False, file_id=121, file_added_at=OLD, air_date=OCT_14),
        ],
        season_monitored={1: True, 5: True, 12: False},
        sizes={5: 500, 12: 800},
    )
    decision = decide(series, watch(), NOW, policy())
    assert decision.action == "prune"
    assert decision.ensure_monitored_seasons == (12,)
    assert decision.monitor_episode_ids == (3,)

    sonarr = FakeSonarr()
    result = apply_decision(
        series,
        decision,
        watch=watch(),
        actions=sonarr,
        state=State(tmp_path / "state.sqlite"),
        settings=policy(dry_run=False, require_delete_approval=True),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    assert result.pending_approval is True
    assert result.applied is False
    assert sonarr.deleted == []
    assert sonarr.monitored == [([3], True)]
    flags = {season["seasonNumber"]: season["monitored"] for season in sonarr.updated[0]["seasons"]}
    assert flags[12] is True
    assert flags[5] is True
    assert flags[1] is True


def test_an_approved_prune_deletes_middle_seasons_and_keeps_the_current_one(tmp_path):
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 5, 1, file_id=51, file_added_at=OLD, air_date=OLD),
            episode(3, 12, 1, file_id=121, file_added_at=OLD, air_date=OCT_14),
        ],
        sizes={1: 100, 5: 500, 12: 800},
    )
    decision = decide(series, watch(), NOW, policy())
    sonarr = FakeSonarr()
    result = apply_decision(
        series,
        decision,
        watch=watch(),
        actions=sonarr,
        state=State(tmp_path / "state.sqlite"),
        settings=policy(dry_run=False, require_delete_approval=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
        confirmed=True,
    )
    assert result.applied is True
    assert result.pending_approval is False
    assert sonarr.deleted == [51]
    flags = {season["seasonNumber"]: season["monitored"] for season in sonarr.updated[0]["seasons"]}
    assert flags[12] is True
    assert flags[5] is False
    assert flags[1] is True
    assert flags.get(0) is None


def test_a_fresh_current_season_with_nothing_else_to_delete_is_held_and_monitored(tmp_path):
    series = _show(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD, air_date=OLD),
            episode(2, 12, 1, monitored=False, file_id=121, file_added_at=OLD, air_date=OCT_14),
        ],
        season_monitored={1: True, 12: False},
    )
    decision = decide(series, watch(), NOW, policy())
    assert decision.action == "hold"
    assert decision.delete_file_ids == ()
    assert decision.reason == "Still airing: keeping season 1 and season 12 (last episode aired Oct 14)."

    sonarr = FakeSonarr()
    result = apply_decision(
        series,
        decision,
        watch=watch(),
        actions=sonarr,
        state=State(tmp_path / "state.sqlite"),
        settings=policy(dry_run=False, require_delete_approval=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    assert result.applied is True
    assert sonarr.deleted == []
    assert sonarr.monitored == [([2], True)]
    flags = {season["seasonNumber"]: season["monitored"] for season in sonarr.updated[0]["seasons"]}
    assert flags == {1: True, 12: True}


def test_parse_series_reads_continuing_status():
    raw = {
        "id": 9,
        "title": "Example",
        "year": 2020,
        "tvdbId": 100,
        "path": "/tv/Example",
        "monitored": True,
        "status": "Continuing",
        "seasons": [{"seasonNumber": 1, "monitored": True}],
    }
    parsed = parse_series(raw, [], NOW)
    assert parsed.status == "continuing"
    ended = parse_series({**raw, "status": "ended"}, [], NOW)
    assert ended.status == "ended"


def test_keep_current_season_defaults_on_and_can_be_saved(tmp_path, monkeypatch):
    settings = Settings(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="key",
        plex_url="http://plex.local:32400",
        plex_token="token",
    )
    assert settings.keep_current_season is True
    assert settings.policy().keep_current_season is True

    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "sonarr_url": "http://sonarr.local:8989",
                "sonarr_api_key": "secret-key-value",
                "plex_url": "http://plex.local:32400",
                "plex_token": "plex-token-value",
                "keep_current_season": False,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings()
    assert loaded.keep_current_season is False
    assert loaded.policy().keep_current_season is False

    admin = (Path(__file__).resolve().parents[1] / "app" / "admin_page.html").read_text(encoding="utf-8")
    assert "Keep current season of airing shows" in admin
    assert 'id="keep-current" type="checkbox" checked' in admin
