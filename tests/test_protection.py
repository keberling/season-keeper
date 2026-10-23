"""Manual picks respect new-season grace unless the override setting is on.

The dialog, the queued job, and the automatic scan have to agree on which
seasons are deleted. A re-download must not restart the grace clock, and a
delete that would push the hour or day over its cap waits unless the window
is empty and that one job is larger than the cap by itself.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.arrivals import arrivals_from_history, resolve_arrivals, seed_arrivals_from_history
from app.config import Settings, SettingsWrite, apply_update, load_settings, public_settings
from app.models import SeriesResult
from app.policy import decide, plan_manual_prune
from app.queue_limit import GIB, TIB, deletion_allowed
from app.service import execute_queued_delete, preview_series
from app.state import State
from tests.builders import NOW, FakeSonarr, episode, make_series, policy, watch
from tests.test_admin import _quiet_env
from tests.test_queue import LIMITS, _job, _report, _settings, _state

ADDED = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
LABEL = "Protected until Dec 14 (added Sep 15)"
SKIP = "Skipped S2, S6: protected until Dec 14"
REMOVED = "Removed S2, S6 despite protection (manual pick)"


def _dexter():
    return make_series(
        [
            episode(1, 1, 1, file_id=11, file_added_at=OLD),
            episode(2, 2, 1, file_id=21, file_added_at=ADDED),
            episode(3, 3, 1, file_id=31, file_added_at=OLD),
            episode(4, 4, 1, file_id=41, file_added_at=OLD),
            episode(5, 5, 1, file_id=51, file_added_at=OLD),
            episode(6, 6, 1, file_id=61, file_added_at=ADDED),
        ],
        season_sizes={1: 10, 2: 100, 3: 10, 4: 20, 5: 30, 6: 200},
        sonarr_id=42,
        title="Dexter",
        year=2006,
    )


def _dated_library(series, history=None):
    class Library(FakeSonarr):
        def list_series(self):
            return [series.raw]

        def list_episodes(self, _series_id):
            rows = []
            for item in series.episodes:
                episode_file = None
                if item.episode_file_id:
                    episode_file = {"id": item.episode_file_id, "size": item.file_size}
                    if item.file_added_at is not None:
                        episode_file["dateAdded"] = item.file_added_at.isoformat()
                rows.append(
                    {
                        "id": item.id,
                        "seasonNumber": item.season_number,
                        "episodeNumber": item.episode_number,
                        "monitored": item.monitored,
                        "hasFile": item.has_file,
                        "episodeFileId": item.episode_file_id or 0,
                        "airDateUtc": "2010-01-01T00:00:00Z",
                        "episodeFile": episode_file,
                    }
                )
            return rows

        def ensure_tag(self, _label):
            return 99

        def queued_seasons(self):
            return set()

        def running_searches(self):
            return set()

        def earliest_arrivals(self, _series_id, _episode_seasons):
            return dict(history or {})

    return Library()


def test_the_override_setting_defaults_off_and_can_be_saved(tmp_path, monkeypatch):
    settings = Settings()
    assert settings.manual_pick_overrides_protection is False
    assert settings.policy().manual_pick_overrides_protection is False
    assert public_settings(settings)["manual_pick_overrides_protection"] is False

    current = Settings(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="secret-key-value",
        plex_url="http://plex.local:32400",
        plex_token="plex-token-value",
    )
    updated = apply_update(
        current,
        SettingsWrite(
            sonarr_url="http://sonarr.local:8989",
            plex_url="http://plex.local:32400",
            manual_pick_overrides_protection=True,
        ),
    )
    assert updated.manual_pick_overrides_protection is True
    assert updated.policy().manual_pick_overrides_protection is True

    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "sonarr_url": "http://sonarr.local:8989",
                "sonarr_api_key": "secret-key-value",
                "plex_url": "http://plex.local:32400",
                "plex_token": "plex-token-value",
                "manual_pick_overrides_protection": True,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings()
    assert loaded.manual_pick_overrides_protection is True
    assert loaded.policy().manual_pick_overrides_protection is True

    root = Path(__file__).resolve().parents[1] / "app"
    admin = (root / "admin_page.html").read_text(encoding="utf-8")
    assert 'id="override-protection" type="checkbox"' in admin
    assert 'id="override-protection" type="checkbox" checked' not in admin
    status = (root / "status_page.html").read_text(encoding="utf-8")
    assert "plan.protection_note" in status
    assert "planWord(season)" in status
    stats = (root / "stats_page.html").read_text(encoding="utf-8")
    assert 'data-kind="protected"' in stats


def test_a_manual_plan_skips_protected_seasons_and_leaves_them_out_of_the_total():
    series = _dexter()
    decision = plan_manual_prune(series, policy(), NOW)
    assert decision.action == "prune"
    assert decision.delete_file_ids == (31, 41, 51)
    assert decision.bytes_to_free == 60
    plans = dict(decision.season_plans)
    assert plans[2] == LABEL
    assert plans[6] == LABEL
    assert decision.protection_result == SKIP
    assert decision.protection_note == f"New seasons are protected for 90 days. {SKIP}"
    assert "You chose this show." in decision.reason


def test_override_on_includes_protected_seasons_and_says_they_were_removed():
    series = _dexter()
    decision = plan_manual_prune(series, policy(manual_pick_overrides_protection=True), NOW)
    assert decision.delete_file_ids == (21, 31, 41, 51, 61)
    assert decision.bytes_to_free == 360
    plans = dict(decision.season_plans)
    assert plans[2] == LABEL
    assert plans[6] == LABEL
    assert decision.protection_result == REMOVED
    assert "despite protection" in decision.protection_note
    assert "90 days" in decision.protection_note


def test_an_automatic_run_still_skips_protected_seasons_when_override_is_on():
    series = _dexter()
    decision = decide(series, watch(), NOW, policy(manual_pick_overrides_protection=True))
    assert decision.delete_file_ids == (31, 41, 51)
    assert 21 not in decision.delete_file_ids
    assert 61 not in decision.delete_file_ids
    assert decision.protection_result == SKIP
    assert decision.bytes_to_free == 60


def test_opening_a_show_does_not_scan_history_and_reports_the_live_mode(tmp_path, monkeypatch):
    from app.main import holder, seed_next_arrival

    series = _dexter()
    state = _state(tmp_path)
    sonarr = _dated_library(series, history={2: OLD, 6: OLD})
    calls = {"n": 0}
    real = sonarr.earliest_arrivals

    def wrapped(series_id, episode_seasons):
        calls["n"] += 1
        return real(series_id, episode_seasons)

    sonarr.earliest_arrivals = wrapped

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getattr__(self, name):
            return getattr(sonarr, name)

        def close(self):
            pass

    holder.state = state
    holder.settings = _settings(dry_run=False)
    holder.running = False
    try:
        live = preview_series(holder.settings, state, sonarr, series.id, NOW)
        assert calls["n"] == 0
        assert live["delete"]["preview"] is True
        assert live["delete"]["dry_run"] is False
        assert live["download"]["preview"] is True
        assert live["download"]["dry_run"] is False
        assert state.season_arrivals(series.id)[2] == ADDED
        again = preview_series(holder.settings, state, sonarr, series.id, NOW)
        assert calls["n"] == 0
        assert again["delete"]["dry_run"] is False

        paused = preview_series(_settings(dry_run=True), state, sonarr, series.id, NOW)
        assert paused["delete"]["preview"] is True
        assert paused["delete"]["dry_run"] is True
        assert calls["n"] == 0

        monkeypatch.setattr("app.main.SonarrClient", Client)
        assert seed_next_arrival() is True
        assert calls["n"] == 1
        assert state.arrival_seeded(series.id) is True
        assert state.season_arrivals(series.id)[2] == OLD
        assert seed_next_arrival() is False
        assert calls["n"] == 1
    finally:
        if holder.mutation.locked():
            holder.mutation.release()
        holder.running = False
        holder.state.close()
        holder.state = None
        holder.settings = None


def test_a_failed_history_seed_is_not_retried_immediately(tmp_path, monkeypatch):
    from app.main import holder, seed_next_arrival

    series = _dexter()
    state = _state(tmp_path)
    sonarr = _dated_library(series)
    calls = {"n": 0}

    def boom(_series_id, _episode_seasons):
        calls["n"] += 1
        raise RuntimeError("history timed out")

    sonarr.earliest_arrivals = boom

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getattr__(self, name):
            return getattr(sonarr, name)

        def close(self):
            pass

    holder.state = state
    holder.settings = _settings(dry_run=False)
    holder.running = False
    try:
        preview_series(holder.settings, state, sonarr, series.id, NOW)
        assert calls["n"] == 0
        monkeypatch.setattr("app.main.SonarrClient", Client)
        assert seed_next_arrival() is False
        assert calls["n"] == 1
        assert state.arrival_seeded(series.id) is False
        assert seed_next_arrival() is False
        assert calls["n"] == 1
        assert state.next_arrival_seed(NOW) is None
    finally:
        holder.running = False
        holder.state.close()
        holder.state = None
        holder.settings = None


def test_the_dialog_uses_the_same_file_count_and_size_as_the_plan(tmp_path):
    series = _dexter()
    state = _state(tmp_path)
    sonarr = _dated_library(series)
    preview = preview_series(_settings(), state, sonarr, series.id, NOW)
    delete = preview["delete"]
    assert delete["files_targeted"] == 3
    assert delete["bytes_to_free"] == 60
    assert delete["protection_result"] == SKIP
    by_season = {row["season_number"]: row for row in delete["seasons"]}
    assert by_season[2]["plan"] == LABEL
    assert by_season[6]["plan"] == LABEL
    assert sonarr.deleted == []

    chosen = _settings(manual_pick_overrides_protection=True)
    again = preview_series(chosen, State(tmp_path / "override.sqlite"), _dated_library(series), series.id, NOW)
    assert again["delete"]["files_targeted"] == 5
    assert again["delete"]["bytes_to_free"] == 360
    assert again["delete"]["protection_result"] == REMOVED
    plans = {row["season_number"]: row["plan"] for row in again["delete"]["seasons"]}
    assert plans[2] == LABEL


def test_a_manual_approval_skips_protected_seasons_and_matches_the_plan(tmp_path):
    series = _dexter()
    state = _state(tmp_path)
    sonarr = _dated_library(series)
    _report(state, sonarr_id=series.id, title="Dexter")
    arrivals = resolve_arrivals(state, series, sonarr)
    expected = plan_manual_prune(series, policy(), NOW, arrivals)
    result = execute_queued_delete(
        _settings(),
        state,
        sonarr,
        series.id,
        {"payload": {"source": "user", "override_protection": False}},
        NOW,
    )
    assert sorted(sonarr.deleted) == sorted(expected.delete_file_ids)
    assert sonarr.deleted == [31, 41, 51]
    assert result.bytes_to_free == expected.bytes_to_free == 60
    notes = state.list_activity(kind="protected")
    assert notes[0]["message"] == SKIP


def test_a_manual_approval_with_override_removes_protected_seasons(tmp_path):
    series = _dexter()
    state = _state(tmp_path)
    sonarr = _dated_library(series)
    _report(state, sonarr_id=series.id, title="Dexter")
    arrivals = resolve_arrivals(state, series, sonarr)
    expected = plan_manual_prune(
        series,
        policy(manual_pick_overrides_protection=True),
        NOW,
        arrivals,
    )
    result = execute_queued_delete(
        _settings(manual_pick_overrides_protection=True),
        state,
        sonarr,
        series.id,
        {"payload": {"source": "user", "override_protection": True}},
        NOW,
    )
    assert sorted(sonarr.deleted) == sorted(expected.delete_file_ids)
    assert sonarr.deleted == [21, 31, 41, 51, 61]
    assert result.protection_result == REMOVED
    assert state.list_activity(kind="protected")[0]["message"] == REMOVED


def test_an_automatic_job_skips_protected_seasons_even_if_the_flag_is_set(tmp_path):
    series = _dexter()
    state = _state(tmp_path)
    sonarr = _dated_library(series)
    _report(state, sonarr_id=series.id, title="Dexter", last_viewed_at=(NOW - timedelta(days=200)).isoformat())
    result = execute_queued_delete(
        _settings(manual_pick_overrides_protection=True),
        state,
        sonarr,
        series.id,
        {"payload": {"source": "cycle", "override_protection": True}},
        NOW,
    )
    assert sonarr.deleted == [31, 41, 51]
    assert result.protection_result == SKIP
    assert state.list_activity(kind="protected")[0]["message"] == SKIP


def test_the_finished_task_shows_the_protection_line(tmp_path, monkeypatch):
    from app.main import holder, process_next_job

    state = _state(tmp_path)
    holder.state = state
    holder.settings = _settings()
    holder.running = False
    _job(
        state,
        kind="delete-later",
        series_id=42,
        priority=2,
        title="Dexter",
        payload={"source": "user", "override_protection": False},
    )
    message = SKIP

    def finish(_job):
        return SeriesResult(
            sonarr_id=42,
            title="Dexter",
            year=2006,
            tvdb_id=1,
            path="/tv/Dexter",
            plex_matched=True,
            match_source="tvdb",
            action="prune",
            reason="You chose this show.",
            last_viewed_at=None,
            season1_last_viewed_at=None,
            active_seasons=[],
            delete_seasons=[3, 4, 5],
            search_seasons=[],
            searches_ran=[],
            searches_waiting=[],
            bytes_to_free=60,
            dry_run=False,
            error=None,
            files_targeted=3,
            applied=True,
            protection_result=message,
        )

    monkeypatch.setattr("app.main._run_claimed", finish)
    try:
        assert process_next_job() is True
        done = holder.state.queue_view()["jobs"][0]
        assert done["status"] == "done"
        assert done["detail"] == message
    finally:
        if holder.mutation.locked():
            holder.mutation.release()
        holder.running = False
        holder.state.close()
        holder.state = None
        holder.settings = None


def test_a_redownload_does_not_move_the_first_seen_date_forward(tmp_path):
    series = _dexter()
    state = State(tmp_path / "arrivals.sqlite")
    first = resolve_arrivals(state, series, None)
    assert first[2] == ADDED
    assert first[3] == OLD

    recent = NOW - timedelta(days=1)
    episodes = []
    for item in series.episodes:
        if item.season_number == 2:
            episodes.append(episode(item.id, 2, 1, file_id=21, file_added_at=recent))
        else:
            episodes.append(item)
    refreshed = make_series(
        episodes,
        season_sizes={1: 10, 2: 100, 3: 10, 4: 20, 5: 30, 6: 200},
        sonarr_id=42,
        title="Dexter",
        year=2006,
    )
    second = resolve_arrivals(state, refreshed, None)
    assert second[2] == ADDED
    decision = plan_manual_prune(refreshed, policy(), NOW, second)
    assert 21 not in decision.delete_file_ids

    earlier = OLD - timedelta(days=30)
    state.remember_arrivals(42, {2: earlier, 6: recent})
    stored = state.season_arrivals(42)
    assert stored[2] == earlier
    assert stored[6] == ADDED


def test_history_seeds_an_earlier_arrival_than_the_current_file_date(tmp_path):
    series = _dexter()
    state = State(tmp_path / "history.sqlite")
    sonarr = _dated_library(series, history={2: OLD, 6: OLD})
    calls = {"n": 0}
    real = sonarr.earliest_arrivals

    def wrapped(series_id, episode_seasons):
        calls["n"] += 1
        return real(series_id, episode_seasons)

    sonarr.earliest_arrivals = wrapped
    arrivals = resolve_arrivals(state, series, sonarr)
    assert calls["n"] == 0
    assert arrivals[2] == ADDED
    assert arrivals[6] == ADDED
    assert seed_arrivals_from_history(state, series, sonarr, NOW) is True
    assert calls["n"] == 1
    stored = state.season_arrivals(series.id)
    assert stored[2] == OLD
    assert stored[6] == OLD
    assert seed_arrivals_from_history(state, series, sonarr, NOW) is False
    assert calls["n"] == 1
    decision = plan_manual_prune(series, policy(), NOW, stored)
    assert decision.delete_file_ids == (21, 31, 41, 51, 61)
    assert decision.protected_seasons == ()

    mapped = arrivals_from_history(
        [
            {"eventType": "grabbed", "date": "2012-01-01T00:00:00Z", "episodeId": 2},
            {"eventType": "downloadFolderImported", "date": "2026-09-15T12:00:00Z", "episodeId": 2},
            {"eventType": "episodeFileDeleted", "date": "2008-01-01T00:00:00Z", "episodeId": 2},
            {"eventType": "seriesFolderImported", "date": "2011-06-01T00:00:00Z", "seasonNumber": 6},
            {"eventType": "grabbed", "date": "2010-01-01T00:00:00Z", "seasonNumber": 0},
        ],
        {2: 2},
    )
    assert mapped[2] == datetime(2012, 1, 1, tzinfo=timezone.utc)
    assert mapped[6] == datetime(2011, 6, 1, tzinfo=timezone.utc)
    assert 0 not in mapped


def test_the_next_job_counts_against_the_hourly_cap(tmp_path):
    state = _state(tmp_path)
    state.record_deletion(at=NOW - timedelta(minutes=5), sonarr_id=1, nbytes=200 * GIB)
    _job(state, kind="delete-later", series_id=2, priority=2, title="Too big", nbytes=400 * GIB)
    _job(state, kind="delete-later", series_id=3, priority=2, title="Fits", nbytes=50 * GIB)
    claimed = state.claim_next_job(NOW, limits=LIMITS)
    assert claimed["sonarr_id"] == 3
    waiting = [job for job in state.queue_view()["jobs"] if job["status"] == "queued"]
    assert [job["sonarr_id"] for job in waiting] == [2]


def test_an_oversized_job_runs_alone_only_when_the_window_is_empty(tmp_path, caplog):
    empty = _state(tmp_path)
    _job(empty, kind="delete-later", series_id=8, priority=2, title="Huge", nbytes=600 * GIB)
    with caplog.at_level(logging.INFO, logger="trimarr"):
        claimed = empty.claim_next_job(NOW, limits=LIMITS)
    assert claimed is not None
    assert claimed["sonarr_id"] == 8
    assert "run alone" in caplog.text
    assert "hourly" in caplog.text

    busy = State(tmp_path / "busy.sqlite")
    busy.record_deletion(at=NOW - timedelta(minutes=5), sonarr_id=1, nbytes=100 * GIB)
    _job(busy, kind="delete-later", series_id=9, priority=2, title="Huge", nbytes=600 * GIB)
    assert busy.claim_next_job(NOW, limits=LIMITS) is None

    allowed, reason = deletion_allowed(
        {"last_at": None, "hour_series": 0, "hour_bytes": 0, "day_bytes": 2 * TIB},
        LIMITS,
        NOW,
        pending_bytes=2 * TIB,
    )
    assert allowed is False
    assert "daily" in reason.lower()

    exact, exact_reason = deletion_allowed(
        {"last_at": None, "hour_series": 0, "hour_bytes": 100 * GIB, "day_bytes": 0},
        LIMITS,
        NOW,
        pending_bytes=400 * GIB,
    )
    assert exact is True
    assert exact_reason == ""
