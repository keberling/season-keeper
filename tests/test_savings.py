"""Savings totals and the ledger of live deletes."""

from app.engine import apply_decision
from app.policy import decide
from app.state import State
from tests.builders import NOW, FakeSonarr, policy, watch
from tests.test_admin import _quiet_env
from tests.test_engine import office


def _apply(tmp_path, *, dry_run: bool):
    state = State(tmp_path / ("live.sqlite" if not dry_run else "preview.sqlite"))
    apply_decision(
        office(),
        decide(office(), watch(), NOW, policy()),
        watch=watch(),
        actions=FakeSonarr(),
        state=state,
        settings=policy(dry_run=dry_run),
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    return state


def test_live_delete_is_remembered_and_preview_is_not(tmp_path):
    live = _apply(tmp_path, dry_run=False)
    events = live.freed_events()
    assert len(events) == 1
    assert events[0]["title"] == "The Office"
    assert events[0]["bytes"] == 8_000
    assert events[0]["file_count"] == 2
    assert _apply(tmp_path, dry_run=True).freed_events() == []


def test_savings_page_splits_possible_ready_and_removed(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    from fastapi.testclient import TestClient

    from app.main import app, holder

    with TestClient(app) as client:
        page = client.get("/stats")
        assert page.status_code == 200
        assert "Possible" in page.text
        assert "hardlinked" in page.text
        empty = client.get("/api/savings").json()
        assert empty["possible_bytes"] == 0
        assert empty["ready_bytes"] == 0
        assert empty["freed_bytes"] == 0
        assert empty["events"] == []

        holder.state.record_freed(
            sonarr_id=1,
            title="The Office",
            file_count=2,
            nbytes=8_000,
            when=NOW,
        )
        holder.state._write(
            {
                "finished_at": NOW.isoformat(),
                "series": [
                    {"action": "prune", "applied": False, "later_bytes": 8000, "bytes_to_free": 8000},
                    {"action": "hold", "applied": False, "later_bytes": 1000, "bytes_to_free": 1000},
                    {"action": "prune", "applied": True, "later_bytes": 500, "bytes_to_free": 500},
                ],
            },
            NOW.isoformat(),
        )
        body = client.get("/api/savings").json()
        assert body["possible_bytes"] == 9500
        assert body["ready_bytes"] == 8000
        assert body["freed_bytes"] == 8000
        assert body["events"][0]["title"] == "The Office"
        assert "Savings" in client.get("/").text
