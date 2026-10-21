"""Live deletes wait for approval, and a non-empty allowlist leaves everyone else in preview."""

import json
import time
from datetime import timedelta
from pathlib import Path

from app.config import Settings, SettingsWrite, apply_update, load_settings
from app.engine import apply_decision, execute
from app.models import ShowActivity, WatchIndex
from app.policy import decide
from app.service import delete_later_seasons
from app.state import State
from tests.builders import NOW, FakeSonarr, episode, make_series, policy, watch
from tests.test_admin import _quiet_env
from tests.test_engine import office


def _index(*tvdb_ids: int, season1: set[int] | None = None) -> WatchIndex:
    played = season1 or set()
    by_tvdb = {}
    for tvdb_id in tvdb_ids:
        activity = ShowActivity()
        if tvdb_id in played:
            activity.last_viewed_at = NOW - timedelta(hours=1)
            activity.season1_last_viewed_at = NOW - timedelta(hours=1)
            activity.active_seasons.add(1)
        by_tvdb[tvdb_id] = activity
    return WatchIndex(
        by_tvdb=by_tvdb,
        matched_tvdb=set(tvdb_ids),
        match_source={tvdb_id: "tvdb" for tvdb_id in tvdb_ids},
        history_complete=True,
        ambiguous_plays=[],
    )


def test_approval_defaults_on_when_the_saved_file_has_no_key(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
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
    loaded = load_settings()
    assert loaded.dry_run is False
    assert loaded.require_delete_approval is True
    assert loaded.live_series_ids == []
    assert loaded.policy().require_delete_approval is True
    assert loaded.policy().live_series_ids == frozenset()

    explicit = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    explicit["require_delete_approval"] = False
    explicit["live_series_ids"] = [9]
    (tmp_path / "settings.json").write_text(json.dumps(explicit), encoding="utf-8")
    again = load_settings()
    assert again.require_delete_approval is False
    assert again.live_series_ids == [9]


def test_saving_other_settings_keeps_the_allowlist_and_approval_flag():
    current = Settings(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="secret-key-value",
        plex_url="http://plex.local:32400",
        plex_token="plex-token-value",
        dry_run=True,
        require_delete_approval=True,
        live_series_ids=[4, 8],
    )
    updated = apply_update(
        current,
        SettingsWrite(
            sonarr_url="http://sonarr.local:8989",
            plex_url="http://plex.local:32400",
            dry_run=False,
        ),
    )
    assert updated.dry_run is False
    assert updated.require_delete_approval is True
    assert updated.live_series_ids == [4, 8]


def test_live_with_approval_on_does_not_delete_or_unmonitor(tmp_path):
    series = office()
    sonarr = FakeSonarr()
    state = State(tmp_path / "state.sqlite")
    settings = policy(dry_run=False)
    assert settings.require_delete_approval is True
    report = execute(
        [series],
        _index(series.tvdb_id),
        sonarr,
        state,
        settings,
        NOW,
        set(),
        set(),
    )
    result = report.results[0]
    assert result.action == "prune"
    assert result.applied is False
    assert result.pending_approval is True
    assert result.in_live_scope is True
    assert result.reason.startswith("Approval required before any files are deleted.")
    assert sonarr.deleted == []
    assert sonarr.updated == []
    assert sonarr.monitored == []
    assert sonarr.searches == []

    direct = apply_decision(
        series,
        decide(series, watch(), NOW, settings),
        watch=watch(),
        actions=sonarr,
        state=state,
        settings=settings,
        now=NOW,
        queue=set(),
        running_searches=set(),
    )
    assert direct.pending_approval is True
    assert sonarr.deleted == []
    assert sonarr.updated == []
    assert sonarr.monitored == []


def test_allowlisted_season_one_play_restores_and_other_shows_are_not_written(tmp_path):
    office_show = make_series(
        [episode(1, 1, 1, file_id=11), episode(2, 2, 1, aired=True)],
        sonarr_id=1,
        title="The Office (US)",
        tvdb_id=73244,
        season_monitored={1: True, 2: False},
    )
    sunny = make_series(
        [episode(31, 1, 1, file_id=311), episode(32, 2, 1, aired=True)],
        sonarr_id=3,
        title="It's Always Sunny",
        tvdb_id=999,
        season_monitored={1: True, 2: False},
    )
    ncis = make_series(
        [
            episode(21, 1, 1, file_id=201),
            episode(22, 2, 1, file_id=202),
        ],
        sonarr_id=2,
        title="NCIS",
        tvdb_id=72108,
        season_sizes={1: 100, 2: 500},
    )
    sonarr = FakeSonarr()
    report = execute(
        [office_show, sunny, ncis],
        _index(73244, 999, 72108, season1={73244, 999}),
        sonarr,
        State(tmp_path / "state.sqlite"),
        policy(dry_run=False, require_delete_approval=True, live_series_ids=frozenset({1})),
        NOW,
        set(),
        set(),
    )
    by_id = {result.sonarr_id: result for result in report.results}
    assert by_id[1].action == "restore"
    assert by_id[1].in_live_scope is True
    assert by_id[1].pending_approval is False
    assert by_id[1].searches_ran == [2]
    assert sonarr.searches == [(1, 2)]

    assert by_id[3].action == "restore"
    assert by_id[3].in_live_scope is False
    assert by_id[3].searches_ran == []
    assert "Outside the live allowlist" in by_id[3].reason

    assert by_id[2].action == "prune"
    assert by_id[2].in_live_scope is False
    assert by_id[2].pending_approval is False
    assert "Outside the live allowlist" in by_id[2].reason

    assert sonarr.deleted == []
    assert all(payload.get("id") != 2 for payload in sonarr.updated)
    assert all(payload.get("id") != 3 for payload in sonarr.updated)
    touched = {episode_id for episode_ids, _monitored in sonarr.monitored for episode_id in episode_ids}
    assert touched.isdisjoint({21, 22, 31, 32})


def test_confirmed_delete_still_removes_files_when_approval_is_on(tmp_path, monkeypatch):
    series = office()
    monkeypatch.setattr("app.service._load_one", lambda *_args, **_kwargs: series)
    settings = Settings(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="secret-key-value",
        plex_url="http://plex.local:32400",
        plex_token="plex-token-value",
        dry_run=False,
        require_delete_approval=True,
    )
    state = State(tmp_path / "manual.sqlite")
    state.dismiss(series.id, NOW)
    sonarr = FakeSonarr()
    result = delete_later_seasons(settings, state, sonarr, series.id, NOW)
    assert result.applied is True
    assert result.pending_approval is False
    assert result.error is None
    assert sonarr.deleted == [21, 22]
    assert 11 not in sonarr.deleted
    assert state.is_dismissed(series.id) is False


def test_dismissed_show_is_not_pending_and_still_is_not_deleted(tmp_path):
    series = office()
    state = State(tmp_path / "state.sqlite")
    state.dismiss(series.id, NOW)
    sonarr = FakeSonarr()
    report = execute(
        [series],
        _index(series.tvdb_id),
        sonarr,
        state,
        policy(dry_run=False),
        NOW,
        set(),
        set(),
    )
    result = report.results[0]
    assert result.action == "prune"
    assert result.pending_approval is False
    assert result.dismissed is True
    assert result.reason.startswith("Dismissed.")
    assert sonarr.deleted == []
    assert sonarr.updated == []


def test_status_scope_and_dismiss_follow_the_saved_settings(tmp_path, monkeypatch):
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
        status = client.get("/api/status").json()
        assert status["dry_run"] is False
        assert status["require_delete_approval"] is True
        assert status["live_series_ids"] == []

        scoped = client.post("/api/live-scope", json={"series_ids": [7, 7, 3]})
        assert scoped.status_code == 200
        assert scoped.json()["live_series_ids"] == [7, 3]
        assert scoped.json()["require_delete_approval"] is True

        saved = client.post(
            "/api/settings",
            json={
                "sonarr_url": "http://sonarr.local:8989",
                "plex_url": "http://plex.local:32400",
                "dry_run": False,
                "require_delete_approval": True,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["live_series_ids"] == [7, 3]

        holder.state._write(
            {
                "finished_at": NOW.isoformat(),
                "series": [
                    {
                        "sonarr_id": 7,
                        "title": "The Office",
                        "action": "prune",
                        "pending_approval": True,
                        "dismissed": False,
                        "reason": "Approval required before any files are deleted.",
                    }
                ],
            },
            NOW.isoformat(),
        )
        dismissed = client.post("/api/series/7/dismiss")
        assert dismissed.status_code == 200
        assert dismissed.json()["queued"] is True
        deadline = time.time() + 5
        while time.time() < deadline and not holder.state.is_dismissed(7):
            time.sleep(0.02)
        stored = client.get("/api/status").json()["report"]["series"][0]
        assert stored["reason"].startswith("Dismissed.")
        assert stored["dismissed"] is True
        assert stored["pending_approval"] is False
        assert holder.state.is_dismissed(7) is True
        page = client.get("/").text
        assert "Live · approval" in page
        admin = client.get("/admin").text
        assert "Require approval before deleting" in admin


def test_pages_explain_approval_and_live_scope():
    root = Path(__file__).resolve().parents[1] / "app"
    status = (root / "status_page.html").read_text(encoding="utf-8")
    admin = (root / "admin_page.html").read_text(encoding="utf-8")
    assert "Require approval before deleting" in admin
    assert 'id="approval" type="checkbox" checked' in admin
    assert "Deletes wait for approval" in admin
    assert "Live for this show only" in status
    assert "Needs approval" in status
    assert "Approval required." in status
    assert "/api/live-scope" in status
    assert "/dismiss" in status
