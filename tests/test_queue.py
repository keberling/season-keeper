"""Persistent task queue: priority, delete limits, recheck, and recovery."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from app.config import Settings
from app.engine import apply_decision
from app.jobs import serve
from app.policy import decide
from app.queue_limit import GIB, TIB, delete_eta, deletion_allowed
from app.service import DeleteSkipped, execute_queued_delete
from app.state import State
from app.sonarr import SonarrError
from tests.builders import NOW, FakeSonarr, policy, watch
from tests.test_admin import _connected, _quiet_env
from tests.test_engine import office

LIMITS = {
    "min_interval_seconds": 30,
    "hourly_series": 50,
    "hourly_bytes": 500 * GIB,
    "daily_bytes": 3 * TIB,
}


def _state(tmp_path) -> State:
    return State(tmp_path / "queue.sqlite")


def _job(state: State, *, kind: str, series_id: int, priority: int, title: str, nbytes: int = 0, payload=None):
    job, created = state.enqueue_job(
        created_at=NOW,
        kind=kind,
        label=title,
        title=title,
        dedupe_key=f"{kind}:{series_id}",
        sonarr_id=series_id,
        payload=payload or {},
        priority=priority,
        nbytes=nbytes,
    )
    assert created
    return job


def test_defaults_are_one_show_at_a_time_with_the_stated_caps():
    settings = Settings()
    assert settings.delete_min_interval_seconds == 30
    assert settings.delete_hourly_series == 50
    assert settings.delete_hourly_gigabytes == 500
    assert settings.delete_daily_terabytes == 3


def test_restores_run_before_deletes(tmp_path):
    state = _state(tmp_path)
    _job(state, kind="delete-later", series_id=1, priority=2, title="Old")
    _job(state, kind="delete-later", series_id=2, priority=2, title="Older")
    _job(state, kind="download-later", series_id=3, priority=0, title="Bring back")
    _job(state, kind="watchlist", series_id=0, priority=0, title="Watchlists")

    first = state.claim_next_job(NOW, limits=LIMITS)
    second = state.claim_next_job(NOW, limits=LIMITS)
    third = state.claim_next_job(NOW, limits=LIMITS)
    assert first["kind"] == "download-later"
    assert second["kind"] == "watchlist"
    assert third["kind"] == "delete-later"
    assert third["sonarr_id"] == 1


def test_a_restore_cancels_the_pending_delete_for_that_show(tmp_path):
    state = _state(tmp_path)
    _job(state, kind="delete-later", series_id=5, priority=2, title="The Office")
    _job(state, kind="download-later", series_id=5, priority=0, title="The Office")
    jobs = state.queue_view()["jobs"]
    deleted = [job for job in jobs if job["kind"] == "delete-later"]
    assert deleted[0]["status"] == "cancelled"
    assert "restore" in deleted[0]["detail"].lower()
    claimed = state.claim_next_job(NOW, limits=LIMITS)
    assert claimed["kind"] == "download-later"
    assert claimed["sonarr_id"] == 5


def test_delete_rate_limit_leaves_the_job_queued(tmp_path):
    state = _state(tmp_path)
    _job(state, kind="delete-later", series_id=4, priority=2, title="Slow", nbytes=GIB)
    state.record_deletion(at=NOW, sonarr_id=9, nbytes=100)
    assert state.claim_next_job(NOW + timedelta(seconds=10), limits=LIMITS) is None
    claimed = state.claim_next_job(NOW + timedelta(seconds=31), limits=LIMITS)
    assert claimed["kind"] == "delete-later"

    blocked = State(tmp_path / "hour.sqlite")
    _job(blocked, kind="delete-later", series_id=4, priority=2, title="Capped")
    for number in range(50):
        blocked.record_deletion(at=NOW, sonarr_id=number + 10, nbytes=1)
    assert blocked.claim_next_job(NOW + timedelta(seconds=31), limits=LIMITS) is None
    later = blocked.claim_next_job(NOW + timedelta(hours=1, seconds=5), limits=LIMITS)
    assert later["sonarr_id"] == 4


def test_eta_uses_the_slowest_cap():
    usage = {"last_at": None, "hour_series": 0, "hour_bytes": 0, "day_bytes": 0}
    nbytes = int(2.1 * TIB)
    text = delete_eta(120, nbytes, usage, LIMITS, NOW)
    assert text == "About 17 hours left for 120 deletes (2.1 TB)."
    allowed, reason = deletion_allowed(
        {"last_at": NOW.isoformat(), "hour_series": 50, "hour_bytes": 0, "day_bytes": 0},
        LIMITS,
        NOW + timedelta(seconds=31),
    )
    assert allowed is False
    assert "hour" in reason.lower()
    paused, _why = deletion_allowed(
        {"last_at": (NOW - timedelta(seconds=10)).isoformat(), "hour_series": 0, "hour_bytes": 0, "day_bytes": 0},
        LIMITS,
        NOW,
    )
    assert paused is False


def test_pause_stops_deletes_and_persists(tmp_path):
    state = _state(tmp_path)
    _job(state, kind="delete-later", series_id=1, priority=2, title="Delete me")
    _job(state, kind="download-later", series_id=2, priority=0, title="Restore me")
    state.set_deletes_paused(True)
    first = state.claim_next_job(NOW, block_deletes=True, limits=LIMITS)
    assert first["kind"] == "download-later"
    assert state.claim_next_job(NOW, block_deletes=True, limits=LIMITS) is None
    state.set_deletes_paused(False)
    nxt = state.claim_next_job(NOW, block_deletes=False, limits=LIMITS)
    assert nxt["kind"] == "delete-later"
    state.set_deletes_paused(True)
    state.close()
    again = State(tmp_path / "queue.sqlite")
    assert again.deletes_paused() is True


def test_restart_requeues_working_jobs_without_using_an_attempt(tmp_path):
    path = tmp_path / "queue.sqlite"
    state = State(path)
    job = _job(state, kind="delete-later", series_id=1, priority=2, title="Halfway")
    with state._lock:
        state._conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, attempts = 1 WHERE id = ?",
            (NOW.isoformat(), job["id"]),
        )
        state._conn.commit()
    state.close()
    again = State(path)
    view = again.queue_view()
    restored = view["jobs"][0]
    assert restored["status"] == "queued"
    assert restored["attempts"] == 1
    assert restored["started_at"] is None


def test_a_stuck_job_is_retried_and_then_failed(tmp_path):
    state = _state(tmp_path)
    job = _job(state, kind="download-later", series_id=1, priority=0, title="Stuck")
    started = (NOW - timedelta(seconds=400)).isoformat()
    with state._lock:
        state._conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, attempts = 0 WHERE id = ?",
            (started, job["id"]),
        )
        state._conn.commit()
    recovered = state.recover_stuck(NOW, timeout_seconds=300, max_attempts=3)
    assert recovered[0]["status"] == "queued"
    assert recovered[0]["attempts"] == 1
    assert "retry" in recovered[0]["error"].lower()

    with state._lock:
        state._conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, attempts = 2 WHERE id = ?",
            (started, job["id"]),
        )
        state._conn.commit()
    failed = state.recover_stuck(NOW, timeout_seconds=300, max_attempts=3)
    assert failed[0]["status"] == "failed"
    assert failed[0]["attempts"] == 3
    assert "several" in failed[0]["error"].lower()


def test_worker_keeps_going_after_a_crash_and_releases_the_lock(tmp_path, monkeypatch):
    stop = threading.Event()
    wake = threading.Event()
    calls = []

    def step():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        stop.set()
        return False

    serve(stop, wake, step)
    assert len(calls) >= 2

    from app.main import holder, process_next_job

    holder.state = _state(tmp_path)
    holder.settings = _connected(dry_run=False, require_delete_approval=False)
    holder.state.enqueue_job(
        created_at=NOW,
        kind="dismiss",
        label="Dismiss",
        title="The Office",
        dedupe_key="dismiss:7",
        sonarr_id=7,
        priority=1,
    )

    def explode(_job):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.main._run_claimed", explode)
    try:
        assert process_next_job() is True
        assert holder.mutation.acquire(blocking=False)
        holder.mutation.release()
        failed = holder.state.queue_view()["jobs"][0]
        assert failed["status"] == "failed"
        assert "boom" in failed["error"]
    finally:
        holder.state.close()
        holder.state = None
        holder.settings = None


def test_watchlist_poll_queues_behind_a_cycle_without_an_info_skip(tmp_path, caplog, monkeypatch):
    from app.main import cycle, holder, watchlist_poll

    holder.settings = _connected()
    holder.state = _state(tmp_path)
    holder.running = True
    try:
        with caplog.at_level(logging.DEBUG, logger="trimarr"):
            assert watchlist_poll() is True
            assert watchlist_poll() is True
        assert "Skipped the watchlist check" not in caplog.text
        assert "already queued" in caplog.text
        assert not any(
            record.levelno >= logging.INFO and "Skipped the watchlist" in record.message
            for record in caplog.records
        )
        scans = []
        holder.running = False
        holder.mutation.acquire()
        monkeypatch.setattr("app.main._scan", lambda: scans.append(True) or True)
        try:
            assert cycle() is True
        finally:
            holder.mutation.release()
        assert scans == [True]
        assert holder.mutation.locked() is False
    finally:
        holder.running = False
        if holder.state is not None:
            holder.state.close()
        holder.state = None
        holder.settings = None


def test_deferred_prune_does_not_delete_inline(tmp_path):
    sonarr = FakeSonarr()
    series = office()
    result = apply_decision(
        series,
        decide(series, watch(), NOW, policy()),
        watch=watch(),
        actions=sonarr,
        state=State(tmp_path / "defer.sqlite"),
        settings=policy(dry_run=False, require_delete_approval=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
        defer_deletes=True,
    )
    assert result.queued_delete is True
    assert result.applied is False
    assert sonarr.deleted == []
    assert result.reason.startswith("Queued for deletion. One show is removed at a time.")


def _library(series, *, tag_id: int = 99):
    class Library(FakeSonarr):
        def list_series(self):
            return [series.raw]

        def list_episodes(self, _series_id):
            rows = []
            for item in series.episodes:
                rows.append(
                    {
                        "id": item.id,
                        "seasonNumber": item.season_number,
                        "episodeNumber": item.episode_number,
                        "monitored": True,
                        "hasFile": item.has_file,
                        "episodeFileId": item.episode_file_id or 0,
                        "airDateUtc": "2020-01-01T00:00:00Z",
                        "episodeFile": {
                            "size": item.file_size,
                            "dateAdded": "2020-01-02T00:00:00Z",
                        },
                    }
                )
            return rows

        def ensure_tag(self, _label):
            return tag_id

    return Library()


def _settings(**overrides) -> Settings:
    values = dict(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="secret-key-value",
        plex_url="http://plex.local:32400",
        plex_token="plex-token-value",
        dry_run=False,
        require_delete_approval=False,
    )
    values.update(overrides)
    return Settings(**values)


def _report(state: State, **row):
    body = {
        "sonarr_id": 1,
        "title": "The Office",
        "plex_matched": True,
        "match_source": "tvdb",
        "last_viewed_at": None,
        "season1_last_viewed_at": None,
        "active_seasons": [],
        "action": "prune",
    }
    body.update(row)
    state._write({"series": [body]}, NOW.isoformat())


def test_recheck_skips_a_delete_after_a_recent_watch_or_a_safety_switch(tmp_path):
    series = office()
    state = _state(tmp_path)
    settings = _settings()
    _report(
        state,
        last_viewed_at=(NOW - timedelta(hours=2)).isoformat(),
        season1_last_viewed_at=(NOW - timedelta(hours=2)).isoformat(),
        active_seasons=[1],
    )
    sonarr = _library(series)
    try:
        execute_queued_delete(
            settings,
            state,
            sonarr,
            1,
            {"payload": {"source": "cycle"}},
            NOW,
        )
    except DeleteSkipped as exc:
        assert "playing" in str(exc).lower() or "watched" in str(exc).lower()
    else:
        raise AssertionError("a recent watch should skip the delete")
    assert sonarr.deleted == []

    state.set_deletes_paused(True)
    try:
        execute_queued_delete(settings, state, sonarr, 1, {"payload": {"source": "user"}}, NOW)
    except DeleteSkipped as exc:
        assert "paused" in str(exc).lower()
    else:
        raise AssertionError("pause should skip the delete")

    preview = _settings(dry_run=True)
    state.set_deletes_paused(False)
    try:
        execute_queued_delete(preview, state, sonarr, 1, {"payload": {"source": "user"}}, NOW)
    except DeleteSkipped as exc:
        assert "preview" in str(exc).lower()
    else:
        raise AssertionError("preview should skip the delete")

    approval = _settings(require_delete_approval=True)
    try:
        execute_queued_delete(approval, state, sonarr, 1, {"payload": {"source": "cycle"}}, NOW)
    except DeleteSkipped as exc:
        assert "approval" in str(exc).lower()
    else:
        raise AssertionError("turning approval on should skip a cycle delete")


def test_recheck_deletes_when_the_show_is_still_quiet(tmp_path):
    series = office()
    state = _state(tmp_path)
    _report(state, last_viewed_at=(NOW - timedelta(days=200)).isoformat())
    sonarr = _library(series)
    result = execute_queued_delete(
        _settings(),
        state,
        sonarr,
        1,
        {"payload": {"source": "cycle"}},
        NOW,
    )
    assert result.applied is True
    assert sonarr.deleted == [21, 22]
    assert 11 not in sonarr.deleted


def test_recheck_skips_the_keep_tag(tmp_path):
    series = office()
    state = _state(tmp_path)
    _report(state)
    sonarr = _library(series, tag_id=7)
    try:
        execute_queued_delete(
            _settings(),
            state,
            sonarr,
            1,
            {"payload": {"source": "cycle"}},
            NOW,
        )
    except DeleteSkipped as exc:
        assert "keep tag" in str(exc).lower()
    else:
        raise AssertionError("the keep tag should skip the delete")
    assert sonarr.deleted == []


def test_duplicate_pending_jobs_collapse(tmp_path):
    state = _state(tmp_path)
    first, created = state.enqueue_job(
        created_at=NOW,
        kind="delete-later",
        label="Queue season deletion",
        title="The Office",
        dedupe_key="delete-later:7",
        sonarr_id=7,
        payload={"source": "cycle"},
        priority=2,
        nbytes=100,
    )
    second, again = state.enqueue_job(
        created_at=NOW + timedelta(seconds=1),
        kind="delete-later",
        label="Approve deletion",
        title="The Office",
        dedupe_key="delete-later:7",
        sonarr_id=7,
        payload={"source": "user"},
        priority=2,
        nbytes=100,
    )
    assert created is True
    assert again is False
    assert second["id"] == first["id"]
    assert second["payload"]["source"] == "user"
    assert state.queue_view()["queued_count"] == 1


def test_delete_404_is_already_gone():
    class Client:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, **kwargs):
            self.calls += 1
            response = type("Response", (), {})()
            response.status_code = 404
            response.text = "missing"
            response.content = b""
            return response

    from app.sonarr import SonarrClient

    client = Client()
    sonarr = SonarrClient("http://sonarr.local:8989", "key", 5, client=client)
    try:
        sonarr.delete_episode_files([21])
    except SonarrError:
        raise AssertionError("a missing file should count as already deleted") from None
    assert client.calls == 1


def test_pages_expose_the_queue_and_delete_speed():
    root_status = __import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "status_page.html"
    admin = root_status.with_name("admin_page.html")
    status = root_status.read_text(encoding="utf-8")
    admin_text = admin.read_text(encoding="utf-8")
    assert "Pause queue" in status
    assert "actions queued" in status
    assert 'id="tasks"' in status
    assert "setInterval(pollQueue, 3000)" in status
    assert 'id="tasks-block"' in status
    assert 'id="tasks-caps"' in status
    assert 'id="tasks-next"' in status
    assert "paintCountdown" in status
    assert "Waiting on the " in status
    assert "Last error:" in status
    assert "Queue season deletion" in status
    assert "Download missing seasons" in status
    assert "Wait between deletes (seconds)" in admin_text
    assert "Most shows to delete per hour" in admin_text
    assert "Most data to delete per hour (GB)" in admin_text
    assert "Most data to delete per day (TB)" in admin_text


def test_health_reports_the_queue(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["queue"]["worker"] is True
        assert body["queue"]["queued"] == 0
        assert "deletes_paused" in body["queue"]
