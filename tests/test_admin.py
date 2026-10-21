import json
from datetime import timedelta
from pathlib import Path

from app.config import (
    Settings,
    SettingsWrite,
    apply_update,
    load_settings,
    public_settings,
)

SONARR_KEY = "SonarrKeyValue9999"
PLEX_TOKEN = "PlexTokenValue8888"
APP_TOKEN = "AccessTokenValue7777"
_ENV = [
    "SONARR_URL",
    "SONARR_API_KEY",
    "PLEX_URL",
    "PLEX_TOKEN",
    "DRY_RUN",
    "WATCH_WINDOW_DAYS",
    "KEEP_SEASONS",
    "SONARR_TAG",
    "IGNORE_TAG",
    "WATCH_INTERVAL_MINUTES",
    "SEARCH_COOLDOWN_HOURS",
    "MONITOR_NEW_ITEMS_WHEN_ACTIVE",
    "MONITOR_NEW_ITEMS_WHEN_IDLE",
    "APP_TOKEN",
    "DATA_DIR",
    "PORT",
    "LOG_LEVEL",
    "RUN_ON_STARTUP",
    "STARTUP_DELAY_SECONDS",
]


def _quiet_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _connected(**overrides) -> Settings:
    values = dict(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="secret-key-value",
        plex_url="http://plex.local:32400",
        plex_token="plex-token-value",
    )
    values.update(overrides)
    return Settings(**values)


def test_old_opt_in_tag_is_not_reused_as_the_ignore_list(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
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
    loaded = load_settings()
    assert loaded.sonarr_tag == "season-keeper"
    assert loaded.ignore_tag == "keep-all"
    assert public_settings(loaded)["ignore_tag"] == "keep-all"
    renamed = apply_update(
        loaded,
        SettingsWrite(
            sonarr_url="http://sonarr.local:8989",
            plex_url="http://plex.local:32400",
            ignore_tag="archive",
        ),
    )
    assert renamed.ignore_tag == "archive"
    assert renamed.sonarr_tag == "season-keeper"
    try:
        apply_update(
            renamed,
            SettingsWrite(
                sonarr_url="http://sonarr.local:8989",
                plex_url="http://plex.local:32400",
                ignore_tag="  ",
            ),
        )
    except ValueError as exc:
        assert "ignore tag" in str(exc)
    else:
        raise AssertionError("expected an empty ignore tag to be rejected")


def test_blank_install_stays_in_preview(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SONARR_URL", "http://sonarr.local:8989")
    loaded = load_settings()
    assert loaded.configured is False
    assert loaded.dry_run is True
    assert loaded.keep_season_numbers == frozenset({1})
    assert not (tmp_path / "settings.json").exists()


def test_saved_file_beats_a_later_environment(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SONARR_URL", "http://from-env:8989")
    monkeypatch.setenv("SONARR_API_KEY", "from-env-key-value")
    monkeypatch.setenv("PLEX_URL", "http://from-env:32400")
    monkeypatch.setenv("PLEX_TOKEN", "from-env-token-value")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("KEEP_SEASONS", "0")
    loaded = load_settings()
    assert loaded.configured is True
    assert loaded.dry_run is False
    assert loaded.keep_season_numbers == frozenset({1})
    assert (tmp_path / "settings.json").exists()

    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SONARR_API_KEY", "changed-env-key")
    monkeypatch.setenv("DRY_RUN", "true")
    again = load_settings()
    assert again.sonarr_api_key == "from-env-key-value"
    assert again.dry_run is False
    assert again.plex_token == "from-env-token-value"


def test_public_view_hides_secrets():
    settings = _connected(
        sonarr_api_key=SONARR_KEY,
        plex_token=PLEX_TOKEN,
        app_token=APP_TOKEN,
    )
    public = public_settings(settings)
    blob = json.dumps(public)
    assert SONARR_KEY not in blob
    assert PLEX_TOKEN not in blob
    assert APP_TOKEN not in blob
    assert public["sonarr_api_key_set"] is True
    assert public["sonarr_api_key_hint"] == "Ends with 9999"
    assert public["plex_token_hint"] == "Ends with 8888"
    assert public["app_token_set"] is True
    assert "sonarr_api_key" not in public
    assert "plex_token" not in public
    assert "app_token" not in public


def test_preview_stays_until_live_is_chosen_and_season_one_stays():
    current = _connected()
    try:
        apply_update(Settings(), SettingsWrite(sonarr_url="http://sonarr.local:8989", plex_url="http://plex.local:32400"))
    except ValueError as exc:
        assert "API key" in str(exc)
    else:
        raise AssertionError("expected a missing secret")

    kept = apply_update(
        current,
        SettingsWrite(sonarr_url="http://sonarr.local:8989", plex_url="http://plex.local:32400"),
    )
    assert kept.dry_run is True
    assert kept.sonarr_api_key == "secret-key-value"

    live = apply_update(
        kept,
        SettingsWrite(
            sonarr_url="http://sonarr.local:8989/",
            plex_url="http://plex.local:32400",
            sonarr_api_key="  ",
            plex_token="",
            dry_run=False,
            keep_seasons="2",
        ),
    )
    assert live.dry_run is False
    assert live.keep_season_numbers == frozenset({1, 2})
    assert live.sonarr_api_key == "secret-key-value"
    assert live.plex_token == "plex-token-value"
    assert live.sonarr_base == "http://sonarr.local:8989"

    still = apply_update(
        live,
        SettingsWrite(sonarr_url="http://sonarr.local:8989", plex_url="http://plex.local:32400"),
    )
    assert still.dry_run is False

    preview = apply_update(
        still,
        SettingsWrite(
            sonarr_url="http://sonarr.local:8989",
            plex_url="http://plex.local:32400",
            dry_run=True,
        ),
    )
    assert preview.dry_run is True


def test_url_query_is_rejected_without_echoing_the_secret():
    try:
        apply_update(
            _connected(),
            SettingsWrite(
                sonarr_url="http://sonarr.local:8989?apikey=super-secret",
                plex_url="http://plex.local:32400",
            ),
        )
    except ValueError as exc:
        assert "super-secret" not in str(exc)
    else:
        raise AssertionError("expected the query url to be rejected")


def test_unraid_template_pulls_a_public_image_and_skips_secrets():
    xml = (
        Path(__file__).resolve().parents[1] / "unraid" / "my-season-keeper.xml"
    ).read_text(encoding="utf-8")
    assert "ghcr.io/keberling/season-keeper:latest" in xml
    assert "my-season-keeper.xml" in xml
    assert "Admin" in xml
    assert "SONARR_" not in xml
    assert "PLEX_" not in xml
    assert 'Target="SONARR' not in xml
    assert 'Target="PLEX' not in xml


def test_pages_point_at_the_admin_form():
    root = Path(__file__).resolve().parents[1] / "app"
    status = (root / "status_page.html").read_text(encoding="utf-8")
    admin = (root / "admin_page.html").read_text(encoding="utf-8")
    assert 'href="/admin"' in status
    assert "Preview mode" in status
    assert "Open admin" in status
    assert "Sonarr URL" in admin
    assert "Plex token" in admin
    assert "Test connection" in admin
    assert SONARR_KEY not in admin


def test_admin_api_round_trip_masks_secrets_and_does_not_run(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    from fastapi.testclient import TestClient

    from app.main import app, holder

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        page = client.get("/")
        assert page.status_code == 200
        assert "Admin" in page.text
        assert "Test delete on this show" in page.text
        admin = client.get("/admin")
        assert admin.status_code == 200
        assert "Sonarr URL" in admin.text

        status = client.get("/api/status").json()
        assert status["configured"] is False
        assert status["dry_run"] is True
        assert status["report"] is None
        assert status["next_run_at"] is None
        assert holder.scheduler.get_job("cycle") is None

        assert client.post("/api/run").status_code == 400
        assert client.get("/api/series/1").status_code == 400
        assert client.post("/api/series/1/delete-later", json={"confirm": True}).status_code == 400
        assert client.get("/api/status").json()["report"] is None

        missing = client.post(
            "/api/settings",
            json={"sonarr_url": "http://sonarr.local:8989", "plex_url": "http://plex.local:32400"},
        )
        assert missing.status_code == 400
        assert "API key" in missing.json()["detail"]

        saved = client.post(
            "/api/settings",
            json={
                "sonarr_url": "http://sonarr.local:8989",
                "sonarr_api_key": SONARR_KEY,
                "plex_url": "http://plex.local:32400",
                "plex_token": PLEX_TOKEN,
                "dry_run": True,
                "keep_seasons": "0",
                "app_token": APP_TOKEN,
            },
        )
        assert saved.status_code == 200
        assert SONARR_KEY not in saved.text
        assert PLEX_TOKEN not in saved.text
        assert APP_TOKEN not in saved.text
        body = saved.json()
        assert body["configured"] is True
        assert body["dry_run"] is True
        assert body["keep_seasons_effective"] == [1]
        assert body["app_token_set"] is True
        assert client.get("/api/settings").status_code == 401

        headers = {"Authorization": "Bearer " + APP_TOKEN}
        again = client.get("/api/settings", headers=headers)
        assert again.status_code == 200
        assert SONARR_KEY not in again.text
        assert again.json()["sonarr_api_key_hint"] == "Ends with 9999"

        rejected = client.post(
            "/api/settings",
            headers=headers,
            json={
                "sonarr_url": "http://sonarr.local:8989?apikey=super-secret",
                "plex_url": "http://plex.local:32400",
            },
        )
        assert rejected.status_code == 400
        assert "super-secret" not in rejected.text

        assert holder.scheduler.get_job("cycle") is not None
        live = client.post(
            "/api/settings",
            headers=headers,
            json={
                "sonarr_url": "http://sonarr.local:8989",
                "sonarr_api_key": "",
                "plex_url": "http://plex.local:32400",
                "plex_token": "",
                "dry_run": False,
                "keep_seasons": "1",
                "watch_interval_minutes": 30,
            },
        )
        assert live.status_code == 200
        assert live.json()["dry_run"] is False
        assert live.json()["keep_seasons_effective"] == [1]
        assert SONARR_KEY not in live.text
        job = holder.scheduler.get_job("cycle")
        assert job.trigger.interval == timedelta(minutes=30)

        stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert stored["sonarr_api_key"] == SONARR_KEY
        assert stored["plex_token"] == PLEX_TOKEN
        assert stored["dry_run"] is False
        assert "port" not in stored
        assert "data_dir" not in stored

        library = client.get("/api/status").json()
        assert library["configured"] is True
        assert library["dry_run"] is False
        assert library["keep_seasons"] == [1]
        assert library["report"] is None

        cleared = client.post(
            "/api/settings",
            headers=headers,
            json={
                "sonarr_url": "http://sonarr.local:8989",
                "plex_url": "http://plex.local:32400",
                "dry_run": True,
                "clear_app_token": True,
            },
        )
        assert cleared.status_code == 200
        assert cleared.json()["app_token_set"] is False
        assert cleared.json()["dry_run"] is True
        assert APP_TOKEN not in cleared.text
        assert client.get("/api/settings").status_code == 200
        reread = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert reread["sonarr_api_key"] == SONARR_KEY
        assert reread["app_token"] == ""
        assert reread["dry_run"] is True
