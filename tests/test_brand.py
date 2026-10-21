"""Trimarr is the product name. Saved settings and the old database still load."""

import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.main import database_path
from app.state import State
from tests.test_admin import _quiet_env

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def test_pages_and_the_image_use_the_trimarr_name():
    for name in ("status_page.html", "admin_page.html", "stats_page.html"):
        page = (ROOT / "app" / name).read_text(encoding="utf-8")
        assert "Trimarr" in page
        assert "Season Keeper" not in page
    assert "<h1>Trimarr</h1>" in (ROOT / "app" / "status_page.html").read_text(encoding="utf-8")
    assert "<title>Trimarr Admin</title>" in (ROOT / "app" / "admin_page.html").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert "ghcr.io/keberling/trimarr" in workflow
    assert "ghcr.io/keberling/season-keeper" in workflow
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'org.opencontainers.image.title="Trimarr"' in dockerfile
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Trimarr")
    assert "ghcr.io/keberling/trimarr:latest" in readme
    assert "ghcr.io/keberling/season-keeper:latest" in readme


def test_the_sonarr_tag_default_stays_season_keeper():
    assert Settings().sonarr_tag == "season-keeper"
    assert Settings().radarr_tag == "season-keeper"


def test_database_path_prefers_the_new_file_and_falls_back_to_the_old_one(tmp_path):
    assert database_path(tmp_path) == tmp_path / "trimarr.sqlite"
    legacy = tmp_path / "season-keeper.sqlite"
    legacy.write_text("", encoding="utf-8")
    assert database_path(tmp_path) == legacy
    (tmp_path / "trimarr.sqlite").write_text("", encoding="utf-8")
    assert database_path(tmp_path) == tmp_path / "trimarr.sqlite"


def test_an_existing_data_folder_loads_without_a_new_database(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    legacy = State(tmp_path / "season-keeper.sqlite")
    legacy.record_activity(
        at=NOW,
        kind="deleted",
        title="The Office",
        message="Deleted season 2 of The Office.",
    )
    legacy.close()
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "sonarr_url": "http://sonarr.local:8989",
                "sonarr_api_key": "secret-key-value",
                "plex_url": "http://plex.local:32400",
                "plex_token": "plex-token-value",
                "sonarr_tag": "season-keeper",
                "dry_run": True,
            }
        ),
        encoding="utf-8",
    )

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        events = client.get("/api/activity").json()["events"]
        assert events[0]["title"] == "The Office"
        assert events[0]["message"] == "Deleted season 2 of The Office."
        assert client.get("/api/settings").json()["sonarr_tag"] == "season-keeper"
        assert "<title>Trimarr</title>" in client.get("/").text
        assert "Season Keeper" not in client.get("/").text
    assert (tmp_path / "season-keeper.sqlite").exists()
    assert (tmp_path / "settings.json").exists()
    assert not (tmp_path / "trimarr.sqlite").exists()
