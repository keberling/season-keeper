"""A newly added season is held until its grace period ends."""

import json
from datetime import timedelta

from app.config import Settings, load_settings
from app.engine import apply_decision
from app.inventory import build_inventory
from app.policy import decide, plan_manual_prune
from app.sonarr import parse_episode
from app.state import State
from tests.builders import NOW, FakeSonarr, episode, make_series, policy, watch
from tests.test_admin import _quiet_env


def _show(season_two, season_three=None):
    episodes = [
        episode(1, 1, 1, file_id=11, file_added_at=NOW - timedelta(days=400)),
        season_two,
    ]
    sizes = {1: 100, 2: 500}
    if season_three is not None:
        episodes.append(season_three)
        sizes[3] = 800
    return make_series(episodes, season_sizes=sizes, title="Ugly Americans")


def test_a_season_added_inside_the_window_is_held_and_not_deleted(tmp_path):
    added = NOW - timedelta(days=10)
    series = _show(episode(2, 2, 1, file_id=21, file_added_at=added))
    decision = decide(series, watch(), NOW, policy())

    assert decision.action == "hold"
    assert decision.delete_file_ids == ()
    assert decision.unmonitor_episode_ids == ()
    assert decision.season_monitored is None
    assert decision.protected_seasons == (2,)
    assert decision.reason == "New season 2 added 2026-10-10, protected until 2027-01-08."
    rows, _totals = build_inventory(series, frozenset({1}), decision)
    assert next(row for row in rows if row.season_number == 2).disposition == "held"

    sonarr = FakeSonarr()
    apply_decision(
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
    assert sonarr.deleted == []
    assert sonarr.updated == []
    assert sonarr.monitored == []


def test_grace_ends_on_the_boundary_and_the_next_moment_prunes():
    inside = episode(2, 2, 1, file_id=21, file_added_at=NOW - timedelta(days=90))
    held = decide(_show(inside), watch(), NOW, policy())
    assert held.action == "hold"

    outside = episode(2, 2, 1, file_id=21, file_added_at=NOW - timedelta(days=90, seconds=1))
    pruned = decide(_show(outside), watch(), NOW, policy())
    assert pruned.action == "prune"
    assert pruned.delete_file_ids == (21,)


def test_missing_file_date_falls_back_to_air_date_and_an_old_file_date_wins():
    aired = _show(episode(2, 2, 1, file_id=21, air_date=NOW - timedelta(days=5)))
    assert decide(aired, watch(), NOW, policy()).action == "hold"

    added_long_ago = _show(
        episode(
            2,
            2,
            1,
            file_id=21,
            file_added_at=NOW - timedelta(days=200),
            air_date=NOW - timedelta(days=1),
        )
    )
    decision = decide(added_long_ago, watch(), NOW, policy())
    assert decision.action == "prune"
    assert decision.delete_file_ids == (21,)


def test_the_earliest_file_date_in_the_season_is_the_one_that_counts():
    series = make_series(
        [
            episode(1, 1, 1, file_id=11, file_added_at=NOW - timedelta(days=400)),
            episode(2, 2, 1, file_id=21, file_added_at=NOW - timedelta(days=200)),
            episode(3, 2, 2, file_id=22, file_added_at=NOW - timedelta(days=1)),
        ],
        season_sizes={1: 100, 2: 500},
    )
    decision = decide(series, watch(), NOW, policy())
    assert decision.action == "prune"
    assert decision.delete_seasons == (2,)


def test_a_shorter_grace_setting_expires_before_the_watch_window():
    added = NOW - timedelta(days=30)
    series = _show(episode(2, 2, 1, file_id=21, file_added_at=added))
    short = decide(series, watch(), NOW, policy(new_season_grace=timedelta(days=14)))
    assert short.action == "prune"
    assert short.delete_file_ids == (21,)
    long = decide(series, watch(), NOW, policy(new_season_grace=timedelta(days=90)))
    assert long.action == "hold"


def test_an_old_season_is_still_pruned_while_a_new_one_is_held():
    series = _show(
        episode(2, 2, 1, file_id=21, file_added_at=NOW - timedelta(days=400)),
        episode(4, 3, 1, file_id=31, file_added_at=NOW - timedelta(days=3)),
    )
    decision = decide(series, watch(), NOW, policy())
    assert decision.action == "prune"
    assert decision.delete_file_ids == (21,)
    assert decision.delete_seasons == (2,)
    assert 31 not in decision.delete_file_ids
    assert 4 not in decision.unmonitor_episode_ids
    assert decision.protected_seasons == (3,)
    assert "New season 3 added 2026-10-17, protected until 2027-01-15." in decision.reason
    rows, _totals = build_inventory(series, frozenset({1}), decision)
    by_season = {row.season_number: row.disposition for row in rows}
    assert by_season[2] == "delete"
    assert by_season[3] == "held"


def test_a_new_special_does_not_hold_the_show_or_get_deleted():
    series = make_series(
        [
            episode(1, 1, 1, file_id=11, file_added_at=NOW - timedelta(days=400)),
            episode(2, 2, 1, file_id=21, file_added_at=NOW - timedelta(days=400)),
            episode(3, 0, 1, file_id=31, file_added_at=NOW - timedelta(days=1)),
        ],
        season_sizes={0: 50, 1: 100, 2: 500},
    )
    decision = decide(series, watch(), NOW, policy())
    assert decision.action == "prune"
    assert decision.delete_file_ids == (21,)
    assert 31 not in decision.delete_file_ids
    assert decision.protected_seasons == ()


def test_a_manual_pick_skips_a_season_inside_grace_unless_the_override_is_on():
    series = _show(episode(2, 2, 1, file_id=21, file_added_at=NOW - timedelta(days=2)))
    skipped = plan_manual_prune(series, policy(), NOW)
    assert skipped.action == "hold"
    assert skipped.delete_file_ids == ()
    assert skipped.bytes_to_free == 0
    assert "Protected until" in dict(skipped.season_plans)[2]
    assert skipped.protection_result.startswith("Skipped S2:")

    removed = plan_manual_prune(
        series,
        policy(manual_pick_overrides_protection=True),
        NOW,
    )
    assert removed.action == "prune"
    assert removed.delete_file_ids == (21,)
    assert removed.protection_result == "Removed S2 despite protection (manual pick)"

    automatic = decide(series, watch(), NOW, policy(manual_pick_overrides_protection=True))
    assert automatic.action == "hold"
    assert automatic.delete_file_ids == ()


def test_grace_defaults_to_the_watch_window_and_can_be_overridden(tmp_path, monkeypatch):
    followed = Settings(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="key",
        plex_url="http://plex.local:32400",
        plex_token="token",
        watch_window_days=45,
    )
    assert followed.new_season_grace_days is None
    assert followed.policy().new_season_grace == timedelta(days=45)

    chosen = followed.model_copy(update={"new_season_grace_days": 10})
    assert chosen.policy().new_season_grace == timedelta(days=10)

    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "sonarr_url": "http://sonarr.local:8989",
                "sonarr_api_key": "secret-key-value",
                "plex_url": "http://plex.local:32400",
                "plex_token": "plex-token-value",
                "watch_window_days": 30,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings()
    assert loaded.new_season_grace_days is None
    assert loaded.policy().new_season_grace == timedelta(days=30)


def test_parse_episode_reads_file_date_added():
    parsed = parse_episode(
        {
            "id": 5,
            "seasonNumber": 3,
            "episodeNumber": 1,
            "monitored": True,
            "hasFile": True,
            "episodeFileId": 9,
            "airDateUtc": "2010-01-01T00:00:00Z",
            "episodeFile": {"size": 10, "dateAdded": "2026-10-10T08:00:00Z"},
        },
        NOW,
    )
    assert parsed.file_added_at == NOW.replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=10)
    assert parsed.air_date is not None
    assert parsed.air_date.year == 2010
