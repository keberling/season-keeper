"""Season 0 is ignored: never searched, restored, deleted, or counted."""

from datetime import timedelta

from app.engine import apply_decision
from app.inventory import build_inventory
from app.policy import decide
from app.state import State
from tests.builders import NOW, FakeSonarr, episode, make_series, policy, watch


def _ugly():
    return make_series(
        [
            episode(1, 1, 1, file_id=11),
            episode(2, 0, 1, aired=True),
            episode(3, 2, 1, aired=True),
        ],
        sonarr_id=9,
        title="Ugly Americans",
        season_monitored={0: True, 1: True, 2: False},
        season_sizes={0: 400, 1: 1_000, 2: 0},
    )


def test_restore_never_searches_or_monitors_specials(tmp_path):
    series = _ugly()
    viewing = watch(
        season1_last_viewed_at=NOW - timedelta(hours=1),
        last_viewed_at=NOW - timedelta(hours=1),
    )
    decision = decide(series, viewing, NOW, policy())

    assert decision.action == "restore"
    assert decision.search_seasons == (2,)
    assert 0 not in decision.search_seasons
    assert decision.season_monitored[0] is False
    assert decision.season_monitored[2] is True
    assert 2 in decision.unmonitor_episode_ids
    assert 2 not in decision.monitor_episode_ids
    assert "0" not in decision.reason

    sonarr = FakeSonarr()
    result = apply_decision(
        series,
        decision,
        watch=viewing,
        actions=sonarr,
        state=State(tmp_path / "state.sqlite"),
        settings=policy(dry_run=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    assert result.searches_ran == [2]
    assert sonarr.searches == [(9, 2)]
    assert all(season != 0 for _series_id, season in sonarr.searches)
    flags = {season["seasonNumber"]: season["monitored"] for season in sonarr.updated[0]["seasons"]}
    assert flags[0] is False
    assert flags[2] is True
    monitored_off = [ids for ids, on in sonarr.monitored if on is False]
    assert any(2 in ids for ids in monitored_off)
    assert sonarr.deleted == []


def test_prune_deletes_later_seasons_and_leaves_specials(tmp_path):
    series = make_series(
        [
            episode(1, 1, 1, file_id=11),
            episode(2, 2, 1, file_id=21),
            episode(3, 2, 2, file_id=22),
            episode(4, 0, 1, file_id=31),
        ],
        season_sizes={1: 1_000, 2: 5_000, 0: 400},
        title="The Office",
    )
    decision = decide(series, watch(), NOW, policy())
    assert decision.action == "prune"
    assert decision.delete_file_ids == (21, 22)
    assert decision.delete_seasons == (2,)
    assert decision.bytes_to_free == 5_000
    assert decision.season_monitored[0] is False

    rows, totals = build_inventory(series, frozenset({1}), decision)
    assert all(row.season_number != 0 for row in rows)
    assert totals.later_bytes == 5_000

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
    assert sonarr.deleted == [21, 22]
    assert 31 not in sonarr.deleted
    assert sonarr.searches == []
    flags = {season["seasonNumber"]: season["monitored"] for season in sonarr.updated[0]["seasons"]}
    assert flags[0] is False
