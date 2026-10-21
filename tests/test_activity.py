"""Activity log, cycle sentences, and the stats totals."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.engine import apply_decision, execute
from app.human import cycle_summary
from app.models import ShowActivity, WatchIndex
from app.policy import decide
from app.state import State
from tests.builders import NOW, FakeSonarr, episode, make_series, policy, watch
from tests.test_admin import _quiet_env
from tests.test_engine import office


def _index(*tvdb_ids: int) -> WatchIndex:
    return WatchIndex(
        by_tvdb={tvdb_id: ShowActivity() for tvdb_id in tvdb_ids},
        matched_tvdb=set(tvdb_ids),
        match_source={tvdb_id: "tvdb" for tvdb_id in tvdb_ids},
        history_complete=True,
        ambiguous_plays=[],
    )


def test_cycle_summary_is_one_readable_sentence():
    preview = cycle_summary(
        dry_run=True,
        prune=2,
        hold=1,
        restore=0,
        skip=0,
        pending=0,
        protected=1,
        searches=0,
        restored_seasons=0,
        freed_bytes=0,
        pending_bytes=0,
    )
    assert preview == (
        "Preview finished: 2 shows would lose later seasons, "
        "1 show is held, and 1 new season protected. Nothing was deleted."
    )
    live = cycle_summary(
        dry_run=False,
        prune=1,
        hold=0,
        restore=0,
        skip=0,
        pending=1,
        protected=0,
        searches=0,
        restored_seasons=0,
        freed_bytes=0,
        pending_bytes=8000,
    )
    assert live == (
        "Live run finished: 1 show waiting for approval (7.8 KB). Nothing was deleted."
    )


def test_a_live_delete_is_one_log_line_and_an_activity_row(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="trimarr")
    state = State(tmp_path / "state.sqlite")
    apply_decision(
        office(),
        decide(office(), watch(), NOW, policy()),
        watch=watch(),
        actions=FakeSonarr(),
        state=state,
        settings=policy(dry_run=False, require_delete_approval=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    message = "Deleted season 2 of The Office (2020), freeing 7.8 KB."
    assert message in caplog.text
    rows = state.list_activity()
    assert len(rows) == 1
    assert rows[0]["kind"] == "deleted"
    assert rows[0]["message"] == message
    assert rows[0]["bytes"] == 8000


def test_an_approved_delete_uses_the_approval_word(tmp_path):
    state = State(tmp_path / "state.sqlite")
    apply_decision(
        office(),
        decide(office(), watch(), NOW, policy()),
        watch=watch(),
        actions=FakeSonarr(),
        state=state,
        settings=policy(dry_run=False, require_delete_approval=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
        confirmed=True,
    )
    row = state.list_activity()[0]
    assert row["kind"] == "approved"
    assert row["message"].startswith("Approved deletion of season 2 of The Office (2020)")


def test_a_restore_says_someone_watched(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="trimarr")
    series = make_series(
        [episode(1, 1, 1, file_id=11), episode(2, 2, 1, aired=True)],
        season_monitored={1: True, 2: False},
        title="The Office",
    )
    viewing = watch(
        season1_last_viewed_at=NOW - timedelta(hours=1),
        last_viewed_at=NOW - timedelta(hours=1),
    )
    state = State(tmp_path / "state.sqlite")
    apply_decision(
        series,
        decide(series, viewing, NOW, policy()),
        watch=viewing,
        actions=FakeSonarr(),
        state=state,
        settings=policy(dry_run=False),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    message = "Restored season 2 of The Office (2020) because someone watched."
    assert message in caplog.text
    assert state.list_activity()[0]["kind"] == "restored"
    assert state.activity_totals() == {"restored_seasons": 1, "searches": 1, "watchlist_adds": 0}


def test_preview_writes_a_cycle_and_does_not_pretend_it_deleted(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="trimarr")
    state = State(tmp_path / "state.sqlite")
    execute(
        [office()],
        _index(100),
        FakeSonarr(),
        state,
        policy(),
        NOW,
        set(),
        set(),
    )
    assert state.list_activity() == []
    cycles = state.list_cycles()
    assert len(cycles) == 1
    assert cycles[0]["dry_run"] is True
    assert cycles[0]["summary"] == (
        "Preview finished: 1 show would lose later seasons. Nothing was deleted."
    )
    assert cycles[0]["summary"] in caplog.text
    assert "Deleted" not in caplog.text


def test_approval_is_requested_once_until_the_show_changes(tmp_path):
    state = State(tmp_path / "state.sqlite")
    settings = policy(dry_run=False, require_delete_approval=True)
    sonarr = FakeSonarr()
    first = execute([office()], _index(100), sonarr, state, settings, NOW, set(), set())
    state.save_report(first)
    execute([office()], _index(100), sonarr, state, settings, NOW, set(), set())
    approvals = state.list_activity(kind="approval")
    assert len(approvals) == 1
    assert approvals[0]["message"].startswith("Approval requested for The Office (2020)")
    assert "Nothing was deleted." in approvals[0]["message"]
    assert sonarr.deleted == []
    assert state.list_cycles()[0]["pending"] == 1


def test_stats_include_pending_space_cycles_and_a_filter(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    from fastapi.testclient import TestClient

    from app.main import app, holder

    with TestClient(app) as client:
        page = client.get("/stats").text
        assert "Possible" in page
        assert "hardlinked" in page
        assert "Waiting for approval" in page
        assert "Activity" in page
        assert "Each cycle" in page
        holder.state.record_activity(
            at=NOW,
            kind="deleted",
            title="The Office",
            message="Deleted season 2 of The Office, freeing 7.8 KB.",
            sonarr_id=1,
            nbytes=8000,
            seasons=1,
        )
        holder.state.record_activity(
            at=NOW,
            kind="restored",
            title="Parks and Recreation",
            message="Restored season 2 of Parks and Recreation because someone watched.",
            sonarr_id=2,
            seasons=1,
        )
        holder.state.record_freed(
            sonarr_id=1,
            title="The Office",
            file_count=1,
            nbytes=8000,
            when=datetime.now(timezone.utc),
        )
        holder.state.record_cycle(
            finished_at=NOW.isoformat(),
            dry_run=True,
            summary="Preview finished: nothing to change. Nothing was deleted.",
            prune=0,
            hold=0,
            restore=0,
            skip=1,
            pending=0,
            protected=3,
            searches=0,
            restored_seasons=0,
            freed_bytes=0,
            pending_bytes=0,
        )
        holder.state._write(
            {
                "finished_at": NOW.isoformat(),
                "series": [
                    {
                        "title": "The Office",
                        "sonarr_id": 1,
                        "action": "prune",
                        "applied": False,
                        "pending_approval": True,
                        "later_bytes": 8000,
                        "bytes_to_free": 8000,
                        "protected_seasons": [3],
                    },
                    {
                        "title": "Ignored",
                        "sonarr_id": 9,
                        "action": "ignore",
                        "later_bytes": 50,
                        "bytes_to_free": 50,
                    },
                ],
            },
            NOW.isoformat(),
        )
        body = client.get("/api/savings").json()
        assert body["pending_bytes"] == 8000
        assert body["freed_bytes"] == 8000
        assert body["recent_freed_bytes"] == 8000
        assert body["protected_seasons"] == 1
        assert body["restored_seasons"] == 1
        assert body["searches"] == 1
        assert body["top_shows"][0]["title"] == "The Office"
        assert body["cycles"][0]["protected"] == 3
        office_only = client.get("/api/activity", params={"kind": "deleted", "q": "Office"}).json()
        assert len(office_only["events"]) == 1
        assert office_only["events"][0]["title"] == "The Office"
        assert client.get("/api/activity", params={"kind": "restored"}).json()["events"][0]["title"] == (
            "Parks and Recreation"
        )


def test_dismiss_is_logged_in_words(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "sonarr_url": "http://sonarr.local:8989",
                "sonarr_api_key": "secret-key-value",
                "plex_url": "http://plex.local:32400",
                "plex_token": "plex-token-value",
                "dry_run": False,
            }
        ),
        encoding="utf-8",
    )
    from fastapi.testclient import TestClient

    from app.main import app, holder

    with TestClient(app) as client:
        holder.state._write(
            {
                "finished_at": NOW.isoformat(),
                "series": [
                    {
                        "sonarr_id": 7,
                        "title": "The Office",
                        "action": "prune",
                        "pending_approval": True,
                        "reason": "Approval required before any files are deleted.",
                    }
                ],
            },
            NOW.isoformat(),
        )
        dismissed = client.post("/api/series/7/dismiss")
        assert dismissed.status_code == 200
        rows = holder.state.list_activity(kind="dismissed")
        assert rows[0]["message"] == (
            "Dismissed the deletion request for The Office. The files stay on disk."
        )


def test_pages_use_plain_words():
    root = Path(__file__).resolve().parents[1] / "app"
    status = (root / "status_page.html").read_text(encoding="utf-8")
    stats = (root / "stats_page.html").read_text(encoding="utf-8")
    assert "Queue season deletion" in status
    assert "Download missing seasons" in status
    assert "Needs approval" in status
    assert "Would remove takes later seasons off disk" in status
    assert "Someone watched season 1" in status
    assert "Waiting for approval" in stats
    assert "New seasons protected" in stats
    assert "Shows holding the most space" in stats
