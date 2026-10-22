"""Why the queue is waiting, and that a restart keeps the same SQLite file."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import RuntimeSettings, load_settings
from app.queue_limit import (
    BLOCK_APPROVAL,
    BLOCK_COOLDOWN,
    BLOCK_DAILY_TB,
    BLOCK_HOURLY_GB,
    BLOCK_HOURLY_SHOWS,
    BLOCK_NONE,
    BLOCK_PAUSED,
    BLOCK_RESTORES,
    BLOCK_RETRY,
    GIB,
    TIB,
    deletion_allowed,
    explain_caps,
    queue_block,
    restore_line,
)
from app.state import State
from tests.builders import NOW
from tests.test_admin import _quiet_env
from tests.test_queue import LIMITS, _job, _settings

ROOT = Path(__file__).resolve().parents[1]


def _state(tmp_path) -> State:
    return State(tmp_path / "queue.sqlite")


def _capture(callback) -> str:
    logger = logging.getLogger("trimarr")
    previous = logger.level
    logger.setLevel(logging.INFO)
    chunks: list[str] = []

    class _List(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            chunks.append(record.getMessage())

    attached = _List()
    attached.setLevel(logging.INFO)
    logger.addHandler(attached)
    try:
        callback()
    finally:
        logger.removeHandler(attached)
        logger.setLevel(previous)
    return "\n".join(chunks)


def _delete(title: str, *, source: str = "user", not_before=None) -> dict:
    return {
        "id": title,
        "kind": "delete-later",
        "status": "queued",
        "title": title,
        "payload": {"source": source},
        "not_before": not_before,
        "attempts": 2,
        "error": "Timed out. It will retry.",
    }


def test_hourly_data_cap_names_the_644_gigabyte_hour():
    event_at = NOW
    now = NOW + timedelta(minutes=1)
    explained = explain_caps(
        [{"at": event_at, "bytes": 644 * GIB}],
        LIMITS,
        now,
        event_at,
    )
    assert explained["reason"] == BLOCK_HOURLY_GB
    assert explained["next_delete_at"] == event_at + timedelta(hours=1)
    labels = {cap["id"]: cap["label"] for cap in explained["caps"]}
    assert labels["hour_bytes"] == "Used 644 GB of 500 GB this hour"
    assert labels["hour_series"] == "Used 1 of 50 shows this hour"
    assert labels["day_bytes"] == "Used 644 GB of 3 TB today"
    allowed, reason = deletion_allowed(
        {
            "last_at": event_at.isoformat(),
            "hour_series": 1,
            "hour_bytes": 644 * GIB,
            "day_bytes": 644 * GIB,
        },
        LIMITS,
        now,
    )
    assert allowed is False
    assert "hour" in reason.lower()


def test_show_cap_cooldown_and_daily_cap_pick_the_longest_wait():
    shows = [{"at": NOW, "bytes": 1} for _ in range(50)]
    explained = explain_caps(shows, LIMITS, NOW + timedelta(minutes=1))
    assert explained["reason"] == BLOCK_HOURLY_SHOWS
    assert explained["next_delete_at"] == NOW + timedelta(hours=1)
    assert explained["caps"][1]["label"] == "Used 50 of 50 shows this hour"

    both = explain_caps(
        [{"at": NOW, "bytes": 644 * GIB}],
        LIMITS,
        NOW + timedelta(seconds=10),
        NOW,
    )
    assert both["reason"] == BLOCK_HOURLY_GB
    assert both["next_delete_at"] == NOW + timedelta(hours=1)

    cooldown = explain_caps(
        [{"at": NOW, "bytes": 1}],
        LIMITS,
        NOW + timedelta(seconds=10),
        NOW,
    )
    assert cooldown["reason"] == BLOCK_COOLDOWN
    assert cooldown["next_delete_at"] == NOW + timedelta(seconds=30)

    older = NOW - timedelta(hours=2)
    daily = explain_caps([{"at": older, "bytes": 3 * TIB}], LIMITS, NOW)
    assert daily["reason"] == BLOCK_DAILY_TB
    assert daily["next_delete_at"] == older + timedelta(days=1)
    assert daily["caps"][2]["label"] == "Used 3 TB of 3 TB today"


def test_block_reason_prefers_pause_approval_and_restores_over_a_cap():
    now = NOW + timedelta(minutes=1)
    events = [{"at": NOW, "bytes": 644 * GIB}]
    waiting = [_delete("Shameless")]

    paused = queue_block(events=events, limits=LIMITS, now=now, paused=True, jobs=waiting)
    assert paused["block"] == BLOCK_PAUSED
    assert paused["next_delete_at"] is None
    assert paused["jobs"][0]["waiting_on"] == BLOCK_PAUSED
    assert paused["block_log"] == ""

    preview = queue_block(events=events, limits=LIMITS, now=now, dry_run=True, jobs=[_delete("Outback")])
    assert preview["block"] == BLOCK_PAUSED
    assert preview["next_delete_at"] is None

    approved = queue_block(
        events=events,
        limits=LIMITS,
        now=now,
        approval_required=True,
        jobs=[_delete("Shameless", source="user")],
    )
    assert approved["block"] == BLOCK_HOURLY_GB
    assert approved["jobs"][0]["waiting_on"] == BLOCK_HOURLY_GB
    assert "Used 644 GB of 500 GB this hour" in approved["block_log"]
    assert "2026-10-20 13:00 UTC" in approved["block_log"]

    cycle = queue_block(
        events=events,
        limits=LIMITS,
        now=now,
        approval_required=True,
        jobs=[_delete("Quiet", source="cycle")],
    )
    assert cycle["block"] == BLOCK_APPROVAL
    assert cycle["next_delete_at"] is None
    assert cycle["jobs"][0]["waiting_on"] == BLOCK_APPROVAL

    restores = queue_block(
        events=events,
        limits=LIMITS,
        now=now,
        restores_ahead=True,
        jobs=[_delete("Shameless")],
    )
    assert restores["block"] == BLOCK_RESTORES
    assert restores["next_delete_at"] is None

    idle = queue_block(events=events, limits=LIMITS, now=now, jobs=[])
    assert idle["block"] == BLOCK_NONE
    assert idle["next_delete_at"] is None
    assert idle["caps"][0]["label"] == "Used 644 GB of 500 GB this hour"


def test_a_later_retry_wins_and_each_job_says_what_it_is_waiting_on():
    now = NOW + timedelta(minutes=1)
    events = [{"at": NOW, "bytes": 644 * GIB}]
    later = NOW + timedelta(hours=2)
    soon = NOW + timedelta(minutes=2)
    search = {
        "kind": "download-later",
        "status": "queued",
        "not_before": later,
        "attempts": 1,
        "error": "Timed out. It will retry.",
    }
    finished = {"kind": "delete-later", "status": "failed", "attempts": 3, "error": "Timed out after several tries."}

    retry = queue_block(
        events=events,
        limits=LIMITS,
        now=now,
        jobs=[_delete("Shameless", not_before=later), search, finished],
    )
    assert retry["block"] == BLOCK_RETRY
    assert retry["next_delete_at"] == later.isoformat()
    assert retry["jobs"][0]["waiting_on"] == BLOCK_RETRY
    assert retry["jobs"][1]["waiting_on"] == BLOCK_RETRY
    assert retry["jobs"][2]["waiting_on"] == BLOCK_NONE

    capped = queue_block(
        events=events,
        limits=LIMITS,
        now=now,
        jobs=[_delete("Shameless", not_before=soon)],
    )
    assert capped["block"] == BLOCK_HOURLY_GB
    assert capped["jobs"][0]["waiting_on"] == BLOCK_HOURLY_GB
    assert datetime.fromisoformat(capped["next_delete_at"]) == NOW + timedelta(hours=1)


def test_the_same_cap_is_logged_when_it_changes_and_then_every_few_minutes(tmp_path):
    state = _state(tmp_path)
    assert state.should_log_block(BLOCK_PAUSED, NOW) is False
    assert state.should_log_block(BLOCK_HOURLY_GB, NOW) is True
    assert state.should_log_block(BLOCK_HOURLY_GB, NOW + timedelta(minutes=1)) is False
    assert state.should_log_block(BLOCK_HOURLY_GB, NOW + timedelta(minutes=4)) is True
    assert state.should_log_block(BLOCK_COOLDOWN, NOW + timedelta(minutes=4, seconds=1)) is True
    assert state.should_log_block(BLOCK_APPROVAL, NOW + timedelta(minutes=5)) is False
    assert state.should_log_block(BLOCK_COOLDOWN, NOW + timedelta(minutes=5)) is False


def test_a_blocked_delete_is_logged_and_left_queued(tmp_path):
    from app.main import holder, process_next_job

    state = _state(tmp_path)
    holder.state = state
    holder.settings = _settings(dry_run=False, require_delete_approval=False)
    holder.running = False
    moment = datetime.now(timezone.utc)
    state.record_deletion(at=moment, sonarr_id=9, nbytes=644 * GIB)
    _job(
        state,
        kind="delete-later",
        series_id=4,
        priority=2,
        title="Shameless",
        nbytes=GIB,
        payload={"source": "user"},
    )
    try:
        first = _capture(process_next_job)
        second = _capture(process_next_job)
        assert "Deletes are waiting on the hourly GB cap." in first
        assert "Used 644 GB of 500 GB this hour." in first
        assert "hourly GB cap" not in second
        queued = [job for job in state.queue_view()["jobs"] if job["status"] == "queued"]
        assert len(queued) == 1
        assert queued[0]["title"] == "Shameless"
        blocked = state.list_activity(kind="blocked")
        assert len(blocked) == 1
        assert "hourly GB cap" in blocked[0]["message"]
    finally:
        if holder.mutation.locked():
            holder.mutation.release()
        holder.running = False
        holder.state.close()
        holder.state = None
        holder.settings = None


def test_restart_requeues_in_flight_jobs_and_keeps_the_file(tmp_path):
    path = tmp_path / "trimarr.sqlite"
    state = State(path)
    kept = _job(state, kind="delete-later", series_id=1, priority=2, title="Kept")
    _job(state, kind="watchlist", series_id=0, priority=0, title="Watchlists")
    running = _job(state, kind="delete-later", series_id=2, priority=2, title="Halfway")
    exhausted = _job(state, kind="delete-later", series_id=3, priority=2, title="Exhausted")
    later = (NOW + timedelta(hours=1)).isoformat()
    with state._lock:
        state._conn.execute("UPDATE jobs SET not_before = ? WHERE id = ?", (later, kept["id"]))
        state._conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, attempts = 1 WHERE id = ?",
            (NOW.isoformat(), running["id"]),
        )
        state._conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, attempts = 3 WHERE id = ?",
            (NOW.isoformat(), exhausted["id"]),
        )
        state._conn.commit()
    state.close()
    assert path.stat().st_size > 0

    again = State(path)
    assert again.restored_queue == {"queued": 3, "in_progress": 1, "failed": 1}
    rows = {job["title"]: job for job in again.queue_view(recent=20)["jobs"]}
    assert rows["Kept"]["status"] == "queued"
    assert rows["Kept"]["not_before"] == later
    assert rows["Halfway"]["status"] == "queued"
    assert rows["Halfway"]["attempts"] == 1
    assert rows["Halfway"]["started_at"] is None
    assert rows["Halfway"]["not_before"] is None
    assert rows["Exhausted"]["status"] == "failed"
    assert rows["Exhausted"]["attempts"] == 3
    assert "retry limit" in rows["Exhausted"]["error"]
    hidden = again._conn.execute(
        "SELECT status FROM jobs WHERE kind = 'watchlist'"
    ).fetchone()
    assert hidden[0] == "queued"

    from app.main import _announce_restored_queue

    logged = _capture(lambda: _announce_restored_queue(again))
    assert restore_line(3, 1) in logged
    assert "Restored 3 queued tasks (1 were in progress)." in logged
    assert "1 in-progress task was not restored because it reached the retry limit." in logged
    messages = [row["message"] for row in again.list_activity()]
    assert "Restored 3 queued tasks (1 were in progress)." in messages
    assert "1 in-progress task was not restored because it reached the retry limit." in messages

    again.restored_queue = {"queued": 0, "in_progress": 0, "failed": 2}
    plural = _capture(lambda: _announce_restored_queue(again))
    assert "2 in-progress tasks were not restored because they reached the retry limit." in plural
    again.close()

    third = State(path)
    assert third.restored_queue == {"queued": 3, "in_progress": 0, "failed": 0}
    assert path.stat().st_size > 0
    still = {job["title"]: job["status"] for job in third.queue_view(recent=20)["jobs"]}
    assert still["Kept"] == "queued"
    assert still["Halfway"] == "queued"
    assert still["Exhausted"] == "failed"
    quiet = _capture(lambda: _announce_restored_queue(third))
    assert "Restored 3 queued tasks (0 were in progress)." in quiet
    third.close()

    empty = State(tmp_path / "empty.sqlite")
    assert empty.restored_queue == {"queued": 0, "in_progress": 0, "failed": 0}
    assert _capture(lambda: _announce_restored_queue(empty)) == ""
    assert empty.list_activity() == []
    empty.close()


def test_sqlite_path_comes_from_data_dir_on_the_data_volume(tmp_path, monkeypatch):
    assert RuntimeSettings.model_fields["data_dir"].default == Path("/data")
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    loaded = load_settings()
    from app.main import database_path

    assert loaded.data_dir == tmp_path
    assert database_path(loaded.data_dir) == tmp_path / "trimarr.sqlite"
    (tmp_path / "season-keeper.sqlite").write_bytes(b"legacy")
    assert database_path(loaded.data_dir) == tmp_path / "season-keeper.sqlite"

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    template = (ROOT / "unraid" / "my-season-keeper.xml").read_text(encoding="utf-8")
    assert "DATA_DIR=/data" in dockerfile
    assert "VOLUME /data" in dockerfile
    assert "mkdir -p /data" in entrypoint
    assert "sqlite" not in entrypoint.lower()
    assert "rm " not in entrypoint
    assert "${HOST_DATA_DIR:-./data}:/data" in compose
    assert "DATA_DIR: /data" in compose
    assert ":/config" not in compose
    assert 'Target="/data"' in template
    assert 'Target="/config"' not in template
    stats = (ROOT / "app" / "stats_page.html").read_text(encoding="utf-8")
    assert 'data-kind="blocked"' in stats


def test_health_and_queue_report_the_hourly_cap(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    monkeypatch.setenv("RUN_ON_STARTUP", "false")
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "sonarr_url": "http://sonarr.local:8989",
                "sonarr_api_key": "secret-key-value",
                "plex_url": "http://plex.local:32400",
                "plex_token": "plex-token-value",
                "dry_run": False,
                "require_delete_approval": False,
            }
        ),
        encoding="utf-8",
    )
    from fastapi.testclient import TestClient

    from app.main import app, holder

    with TestClient(app) as client:
        assert holder.state is not None
        moment = datetime.now(timezone.utc)
        holder.state.record_deletion(at=moment, sonarr_id=9, nbytes=644 * GIB)
        holder.state.enqueue_job(
            created_at=moment,
            kind="delete-later",
            label="Delete season 2 and later",
            title="Shameless",
            dedupe_key="delete-later:4",
            sonarr_id=4,
            payload={"source": "user"},
            priority=2,
            nbytes=GIB,
        )
        holder.worker_wake.set()
        deadline = time.time() + 4
        health = client.get("/health").json()
        while time.time() < deadline and health["queue"]["block"] != BLOCK_HOURLY_GB:
            time.sleep(0.2)
            health = client.get("/health").json()
        queue = client.get("/api/queue").json()
        activity = client.get("/api/activity?kind=blocked").json()
        assert health["status"] == "ok"
        assert health["queue"]["block"] == BLOCK_HOURLY_GB
        assert health["queue"]["next_delete_at"]
        labels = [cap["label"] for cap in health["queue"]["caps"]]
        assert "Used 644 GB of 500 GB this hour" in labels
        assert queue["block"] == BLOCK_HOURLY_GB
        waiting = [job for job in queue["jobs"] if job["title"] == "Shameless"]
        assert waiting[0]["status"] == "queued"
        assert waiting[0]["waiting_on"] == BLOCK_HOURLY_GB
        assert any("hourly GB cap" in event["message"] for event in activity["events"])
    if holder.state is not None:
        holder.state.close()
        holder.state = None
    holder.settings = None
