"""Watching season N makes season N+1 ready. Season 1 still restores the whole show."""

from datetime import timedelta

from app.engine import apply_decision, execute
from app.models import ShowActivity, WatchIndex
from app.policy import decide
from app.service import preview_series
from app.state import State
from tests.builders import NOW, FakeSonarr, episode, make_series, policy, watch
from tests.test_queue import _settings


def _show():
    return make_series(
        [
            episode(1, 1, 1, file_id=11, file_added_at=NOW - timedelta(days=400)),
            episode(2, 2, 1, file_id=21, file_added_at=NOW - timedelta(days=400)),
            episode(3, 3, 1, file_id=31, file_added_at=NOW - timedelta(days=400)),
            episode(40, 0, 1, file_id=9, monitored=True),
            episode(41, 4, 1, aired=True, monitored=False),
            episode(42, 4, 2, aired=False, monitored=False),
        ],
        season_sizes={0: 5, 1: 10, 2: 20, 3: 30, 4: 0},
        season_monitored={0: True, 1: True, 2: True, 3: True, 4: False},
        sonarr_id=9,
        title="Dexter",
        year=2006,
        tvdb_id=79349,
    )


def _played(*seasons: int):
    return watch(
        last_viewed_at=NOW - timedelta(days=1),
        season_plays=tuple((season, NOW - timedelta(days=1)) for season in seasons),
    )


def test_watching_season_three_monitors_and_searches_season_four():
    series = _show()
    decision = decide(series, _played(3), NOW, policy())
    assert decision.action == "restore"
    assert decision.search_seasons == (4,)
    assert decision.monitor_episode_ids == (41, 42)
    assert decision.unmonitor_episode_ids == ()
    assert decision.delete_file_ids == ()
    assert decision.season_monitored == {4: True}
    assert 0 not in (decision.search_seasons or ())
    assert decision.reason == "Watched S3, so S4 was monitored and searched."


def test_the_highest_watched_season_is_the_one_that_counts():
    series = _show()
    decision = decide(series, _played(2, 3), NOW, policy())
    assert decision.search_seasons == (4,)
    assert decision.reason.startswith("Watched S3,")


def test_a_playing_session_counts_without_a_history_row():
    series = _show()
    decision = decide(series, watch(active_seasons=frozenset({3})), NOW, policy())
    assert decision.search_seasons == (4,)
    assert decision.reason == "Watched S3, so S4 was monitored and searched."


def test_season_four_already_on_disk_is_not_searched():
    series = make_series(
        [
            episode(3, 3, 1, file_id=31),
            episode(41, 4, 1, file_id=401, aired=True, monitored=True),
        ],
        season_monitored={3: True, 4: True},
        sonarr_id=9,
        title="Dexter",
        tvdb_id=79349,
    )
    decision = decide(series, _played(3), NOW, policy())
    assert decision.action == "hold"
    assert decision.search_seasons == ()
    assert decision.delete_file_ids == ()
    assert decision.reason == "Watched S3. S4 already downloaded."


def test_the_last_season_has_no_next_season():
    series = make_series(
        [episode(3, 3, 1, file_id=31)],
        season_monitored={3: True},
        title="Dexter",
    )
    decision = decide(series, _played(3), NOW, policy())
    assert decision.action == "hold"
    assert decision.search_seasons == ()
    assert decision.monitor_episode_ids == ()
    assert decision.reason == "Watched S3. There is no season 4."


def test_a_special_does_not_start_a_search_and_is_not_the_next_season():
    series = _show()
    special = decide(series, watch(active_seasons=frozenset({0}), last_viewed_at=NOW), NOW, policy())
    assert special.action == "hold"
    assert special.search_seasons == ()
    assert special.monitor_episode_ids == ()

    followed = decide(series, _played(3), NOW, policy())
    assert 0 not in followed.search_seasons
    assert 40 not in followed.monitor_episode_ids
    assert 40 not in followed.unmonitor_episode_ids


def test_season_one_still_restores_every_missing_later_season():
    series = make_series(
        [
            episode(1, 1, 1, file_id=11),
            episode(2, 2, 1, aired=True),
            episode(3, 3, 1, file_id=31),
            episode(4, 4, 1, aired=True),
        ],
        season_monitored={1: True, 2: False, 3: True, 4: False},
    )
    decision = decide(
        series,
        watch(
            season1_last_viewed_at=NOW - timedelta(days=1),
            last_viewed_at=NOW - timedelta(days=1),
            season_plays=((1, NOW - timedelta(days=1)), (3, NOW - timedelta(days=1))),
        ),
        NOW,
        policy(),
    )
    assert decision.action == "restore"
    assert decision.search_seasons == (2, 4)
    assert "Season 1" in decision.reason
    assert not decision.reason.startswith("Watched S")


def test_a_play_outside_the_window_does_not_fetch_the_next_season():
    series = _show()
    old = NOW - timedelta(days=200)
    decision = decide(
        series,
        watch(last_viewed_at=old, season_plays=((3, old),)),
        NOW,
        policy(),
    )
    assert decision.action == "prune"
    assert 4 not in decision.search_seasons
    assert 31 in decision.delete_file_ids


def test_search_respects_cooldown_and_the_sonarr_queue(tmp_path):
    series = _show()
    decision = decide(series, _played(3), NOW, policy())
    state = State(tmp_path / "binge.sqlite")
    state.mark_search(series.id, 4, NOW - timedelta(hours=1))
    sonarr = FakeSonarr()
    cooled = apply_decision(
        series,
        decision,
        watch=_played(3),
        actions=sonarr,
        state=state,
        settings=policy(dry_run=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    assert sonarr.searches == []
    assert cooled.searches_ran == []
    assert "S4 search on cooldown" in cooled.reason
    assert "and searched" not in cooled.reason

    queued = apply_decision(
        series,
        decision,
        watch=_played(3),
        actions=sonarr,
        state=State(tmp_path / "queue.sqlite"),
        settings=policy(dry_run=False),
        now=NOW,
        queue={(series.id, 4)},
        running_searches=set(),
    )
    assert sonarr.searches == []
    assert "already queued" in queued.reason


def test_preview_logs_the_next_season_without_calling_sonarr(tmp_path):
    series = _show()
    decision = decide(series, _played(3), NOW, policy())
    sonarr = FakeSonarr()
    result = apply_decision(
        series,
        decision,
        watch=_played(3),
        actions=sonarr,
        state=State(tmp_path / "preview.sqlite"),
        settings=policy(dry_run=True),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    assert result.reason.startswith("Preview only. Watched S3, so S4 was monitored and searched.")
    assert result.searches_ran == [4]
    assert sonarr.searches == []
    assert sonarr.updated == []
    assert sonarr.monitored == []
    assert sonarr.deleted == []


def test_live_search_is_recorded_and_a_repeat_is_not(tmp_path):
    series = _show()
    activity = ShowActivity()
    activity.last_viewed_at = NOW - timedelta(days=1)
    activity.season_last_viewed[3] = NOW - timedelta(days=1)
    index = WatchIndex(
        by_tvdb={series.tvdb_id: activity},
        matched_tvdb={series.tvdb_id},
        match_source={series.tvdb_id: "tvdb"},
        history_complete=True,
        ambiguous_plays=[],
    )
    state = State(tmp_path / "live.sqlite")
    sonarr = FakeSonarr()
    settings = policy(dry_run=False, require_delete_approval=False)
    first = execute([series], index, sonarr, state, settings, NOW, set(), set())
    assert first.results[0].searches_ran == [4]
    assert sonarr.searches == [(series.id, 4)]
    assert 31 not in sonarr.deleted
    notes = state.list_activity(kind="restored")
    assert notes[0]["message"] == "Watched S3, so S4 was monitored and searched."

    sonarr.searches.clear()
    execute([series], index, sonarr, state, settings, NOW + timedelta(minutes=5), set(), set())
    assert sonarr.searches == []
    cooled = [row["message"] for row in state.list_activity(kind="restored")]
    assert cooled[0] == "Watched S3, so S4 was monitored. S4 search on cooldown."
    assert len(cooled) == 2


def test_a_show_outside_the_allowlist_is_not_searched(tmp_path):
    series = _show()
    other = make_series(
        [episode(51, 3, 1, file_id=51), episode(61, 4, 1, aired=True, monitored=False)],
        season_monitored={3: True, 4: False},
        sonarr_id=8,
        title="Other",
        tvdb_id=8,
    )
    def activity_for(tvdb):
        row = ShowActivity()
        row.last_viewed_at = NOW - timedelta(days=1)
        row.season_last_viewed[3] = NOW - timedelta(days=1)
        return row

    index = WatchIndex(
        by_tvdb={series.tvdb_id: activity_for(series.tvdb_id), other.tvdb_id: activity_for(other.tvdb_id)},
        matched_tvdb={series.tvdb_id, other.tvdb_id},
        match_source={series.tvdb_id: "tvdb", other.tvdb_id: "tvdb"},
        history_complete=True,
        ambiguous_plays=[],
    )
    sonarr = FakeSonarr()
    report = execute(
        [series, other],
        index,
        sonarr,
        State(tmp_path / "scope.sqlite"),
        policy(dry_run=False, require_delete_approval=False, live_series_ids=frozenset({series.id})),
        NOW,
        set(),
        set(),
    )
    by_id = {result.sonarr_id: result for result in report.results}
    assert by_id[series.id].searches_ran == [4]
    assert by_id[series.id].in_live_scope is True
    assert sonarr.searches == [(series.id, 4)]
    assert by_id[other.id].searches_ran == []
    assert by_id[other.id].in_live_scope is False
    assert by_id[other.id].reason.startswith("Outside the live allowlist")


def test_one_show_queue_uses_sonarr_series_filter():
    """seriesId is ignored by Sonarr and would page every download."""
    seen = []

    class Client:
        def request(self, method, url, **kwargs):
            seen.append(kwargs.get("params") or {})
            response = type("Response", (), {})()
            response.status_code = 200
            response.text = ""
            response.content = b"{}"
            response.json = lambda: {
                "records": [{"seriesId": 721, "seasonNumber": 4}],
                "totalRecords": 1,
            }
            return response

    from app.sonarr import SonarrClient

    sonarr = SonarrClient("http://sonarr.local:8989", "key", 5, client=Client())
    assert sonarr.queued_seasons(721) == {(721, 4)}
    assert seen[0]["seriesIds"] == 721
    assert "seriesId" not in seen[0]
    assert len(seen) == 1


def test_the_series_dialog_repeats_the_binge_sentence():
    page = __import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "status_page.html"
    text = page.read_text(encoding="utf-8")
    assert "function seriesBingeReason" in text
    assert "Watched S" in text


def test_opening_one_show_does_not_download_the_library_or_the_whole_queue(tmp_path):
    series = _show()
    calls = {"library": 0, "queue": []}

    class One(FakeSonarr):
        def get_series(self, series_id):
            assert series_id == series.id
            return series.raw

        def list_series(self):
            calls["library"] += 1
            raise AssertionError("the preview listed every series")

        def list_episodes(self, _series_id):
            return []

        def queued_seasons(self, series_id=None):
            calls["queue"].append(series_id)
            return set()

        def running_searches(self):
            return set()

    preview = preview_series(_settings(dry_run=False), State(tmp_path / "one.sqlite"), One(), series.id, NOW)
    assert calls["library"] == 0
    assert calls["queue"] == [series.id]
    assert preview["delete"]["dry_run"] is False
    assert preview["delete"]["preview"] is True
