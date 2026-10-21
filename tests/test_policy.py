from datetime import timedelta

from app.policy import decide, plan_manual_prune, plan_manual_restore
from tests.builders import NOW, episode, make_series, policy, watch


def office():
    return make_series(
        [
            episode(1, 1, 1, file_id=11),
            episode(2, 2, 1, file_id=21),
            episode(3, 2, 2, file_id=22),
            episode(4, 0, 1, file_id=31),
        ],
        season_sizes={1: 1_000, 2: 5_000, 0: 400},
        title="The Office",
    )


def test_prune_removes_later_seasons_and_keeps_season_one():
    decision = decide(office(), watch(), NOW, policy())

    assert decision.action == "prune"
    assert decision.delete_file_ids == (21, 22)
    assert 11 not in decision.delete_file_ids
    assert 31 not in decision.delete_file_ids
    assert decision.delete_seasons == (2,)
    assert 0 not in decision.delete_seasons
    assert decision.bytes_to_free == 5_000
    assert decision.season_monitored[1] is True
    assert decision.season_monitored[2] is False
    assert decision.season_monitored[0] is False
    assert decision.unmonitor_episode_ids == (2, 3, 4)
    assert 1 not in decision.unmonitor_episode_ids


def test_recent_watch_keeps_files():
    seen = NOW - timedelta(days=89)
    decision = decide(office(), watch(last_viewed_at=seen), NOW, policy())
    assert decision.action == "hold"
    assert decision.delete_file_ids == ()


def test_watch_on_the_boundary_still_counts():
    seen = NOW - timedelta(days=90)
    decision = decide(office(), watch(last_viewed_at=seen), NOW, policy())
    assert decision.action == "hold"


def test_watch_past_the_window_prunes():
    seen = NOW - timedelta(days=90, seconds=1)
    decision = decide(office(), watch(last_viewed_at=seen), NOW, policy())
    assert decision.action == "prune"


def test_season_one_play_searches_missing_later_seasons_only():
    series = make_series(
        [
            episode(1, 1, 1, file_id=11),
            episode(2, 2, 1, aired=True),
            episode(3, 3, 1, aired=False),
        ],
        season_monitored={1: True, 2: False, 3: False},
    )
    decision = decide(
        series,
        watch(season1_last_viewed_at=NOW - timedelta(days=1), last_viewed_at=NOW - timedelta(days=1)),
        NOW,
        policy(),
    )

    assert decision.action == "restore"
    assert decision.search_seasons == (2,)
    assert 1 not in decision.search_seasons
    assert decision.season_monitored[2] is True
    assert decision.season_monitored[3] is True
    assert decision.delete_file_ids == ()


def test_playing_season_one_on_a_complete_show_leaves_it():
    decision = decide(office(), watch(active_seasons=frozenset({1}), active_users=("Ada",)), NOW, policy())
    assert decision.action == "restore"
    assert decision.search_seasons == ()
    assert decision.delete_file_ids == ()
    assert decision.season_monitored[0] is False
    assert "Ada" in decision.reason


def test_playing_a_later_season_holds():
    decision = decide(office(), watch(active_seasons=frozenset({3})), NOW, policy())
    assert decision.action == "hold"
    assert decision.delete_file_ids == ()


def test_unmatched_show_is_not_pruned():
    decision = decide(office(), watch(matched=False), NOW, policy())
    assert decision.action == "skip"
    assert decision.delete_file_ids == ()
    assert "Nothing" in decision.reason


def test_incomplete_history_blocks_prune():
    decision = decide(office(), watch(history_reliable=False), NOW, policy())
    assert decision.action == "skip"
    assert "incomplete" in decision.reason


def test_already_trimmed_show_is_a_skip():
    series = make_series(
        [episode(1, 1, 1, file_id=11)],
        season_sizes={1: 100},
        monitor_new="none",
    )
    decision = decide(series, watch(), NOW, policy())
    assert decision.action == "skip"


def test_manual_prune_does_not_claim_the_show_was_idle_in_plex():
    decision = plan_manual_prune(office(), policy())
    assert decision.action == "prune"
    assert decision.reason.startswith("You chose this show.")
    assert 11 not in decision.delete_file_ids


def test_manual_restore_asks_sonarr_to_fetch():
    series = make_series(
        [episode(1, 1, 1, file_id=11), episode(2, 2, 1, aired=True)],
        season_monitored={1: True, 2: False},
    )
    decision = plan_manual_restore(series, policy(), NOW)
    assert decision.action == "restore"
    assert decision.search_seasons == (2,)
    assert "You asked Sonarr" in decision.reason
