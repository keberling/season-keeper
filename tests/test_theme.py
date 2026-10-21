"""The three pages share one cream theme. Layout only; no behavior."""

from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1] / "app"


def test_pages_share_the_cream_theme():
    theme = (ROOT / "theme.css").read_text(encoding="utf-8")
    assert "--bg: #FAF6EE" in theme
    assert "--bg-raise: #FFFDF8" in theme
    assert "--text: #2C241C" in theme
    for name in ("status_page.html", "admin_page.html", "stats_page.html"):
        page = (ROOT / name).read_text(encoding="utf-8")
        assert 'href="/theme.css"' in page
        assert "color-scheme: dark" not in page


def test_theme_stylesheet_is_served(tmp_path, monkeypatch):
    from tests.test_admin import _quiet_env

    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/theme.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]
        assert "--bg: #FAF6EE" in response.text
