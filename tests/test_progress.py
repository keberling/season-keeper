"""Live progress reported while a scan is still reading Sonarr and Plex."""

from datetime import timedelta

import httpx

from app.config import Settings
from app.plex import PlexClient
from app.progress import Progress, count_phrase
from app.service import _run_cycle
from app.state import State
from tests.builders import NOW
from tests.test_admin import _quiet_env

TOKEN = "PlexTokenValue8888"


def test_count_phrase_uses_thousands_separators_and_plural():
    assert count_phrase(1400, "play") == "1,400 plays"
    assert count_phrase(1, "show") == "1 show"
    assert count_phrase(18, "lookup") == "18 lookups"


def test_progress_snapshot_is_a_copy_and_clears():
    progress = Progress()
    assert progress.snapshot() is None
    progress.update("Sonarr episodes, 1 of 2: The Office.", done=1, total=2)
    first = progress.snapshot()
    first["message"] = "changed"
    progress.update("Checking shows, 2 of 2.", done=2, total=2)
    assert progress.snapshot() == {
        "message": "Checking shows, 2 of 2.",
        "done": 2,
        "total": 2,
    }
    progress.clear()
    assert progress.snapshot() is None


def test_cycle_reports_sonarr_and_plex_counts_as_work_proceeds(tmp_path):
    progress = Progress()
    seen: list[tuple[str, int, int | None]] = []
    original = progress.update

    def update(message, done=0, total=None):
        original(message, done, total)
        seen.append((message, done, total))

    progress.update = update
    episode_seen: list[tuple[int, dict | None]] = []

    class Sonarr:
        def status_version(self):
            return "4.0.0"

        def ensure_tag(self, label):
            assert label == "keep-all"
            return 9

        def list_series(self):
            return [
                {
                    "id": 1,
                    "title": "The Office",
                    "tags": [],
                    "tvdbId": 73244,
                    "monitored": True,
                    "seasons": [{"seasonNumber": 1, "monitored": True}],
                },
                {
                    "id": 2,
                    "title": "Parks and Recreation",
                    "tags": [],
                    "tvdbId": 111,
                    "monitored": True,
                    "seasons": [{"seasonNumber": 1, "monitored": True}],
                },
                {"id": 3, "title": "Untagged", "tags": [], "seasons": []},
                {"id": 4, "title": "Archived", "tags": [9], "seasons": []},
            ]

        def list_episodes(self, series_id):
            episode_seen.append((series_id, progress.snapshot()))
            return []

        def queued_seasons(self, series_id=None):
            return set()

        def running_searches(self):
            return set()

    recent = int((NOW - timedelta(days=1)).timestamp())
    older = int((NOW - timedelta(days=200)).timestamp())
    during: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        current = progress.snapshot()
        during.append((path, None if current is None else current["message"]))
        if path == "/library/sections":
            payload = {
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "movie", "title": "Movies"},
                        {"key": "2", "type": "show", "title": "TV Shows"},
                        {"key": "3", "type": "show", "title": "Anime"},
                    ]
                }
            }
        elif path == "/library/sections/2/all":
            payload = {
                "MediaContainer": {
                    "Metadata": [
                        {"ratingKey": "10", "title": "The Office", "type": "show", "Guid": [{"id": "tvdb://73244"}]},
                        {"ratingKey": "11", "title": "Parks and Recreation", "type": "show"},
                    ]
                }
            }
        elif path == "/library/sections/3/all":
            payload = {
                "MediaContainer": {
                    "Metadata": [{"ratingKey": "12", "title": "Example", "type": "show", "Guid": [{"id": "tvdb://1"}]}]
                }
            }
        elif path == "/status/sessions/history/all":
            start = int(request.url.params.get("X-Plex-Container-Start", "0"))
            if start == 0:
                rows = [
                    {"type": "episode", "viewedAt": recent, "grandparentRatingKey": "10", "parentIndex": 1},
                    {"type": "episode", "viewedAt": recent, "grandparentRatingKey": "10", "parentIndex": 2},
                ]
            else:
                rows = [
                    {"type": "episode", "viewedAt": recent, "ratingKey": "999"},
                    {"type": "episode", "viewedAt": older, "grandparentRatingKey": "10", "parentIndex": 1},
                ]
            payload = {"MediaContainer": {"Metadata": rows}}
        elif path == "/library/metadata/999":
            payload = {
                "MediaContainer": {
                    "Metadata": [{
                        "grandparentRatingKey": "10",
                        "parentIndex": 1,
                        "grandparentTitle": "The Office",
                        "type": "episode",
                    }]
                }
            }
        else:
            payload = {"MediaContainer": {}}
        return httpx.Response(200, json=payload)

    plex = PlexClient("http://plex.local:32400", TOKEN, 5, 5, 2, 100, 5)
    transport = httpx.Client(transport=httpx.MockTransport(handler))
    original_client = plex._client
    plex._client = transport
    settings = Settings(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="secret-key-value",
        plex_url="http://plex.local:32400",
        plex_token=TOKEN,
        data_dir=tmp_path,
        watch_window_days=90,
    )
    try:
        report = _run_cycle(settings, State(tmp_path / "season-keeper.sqlite"), Sonarr(), plex, NOW, progress)
        plex.list_shows()
        plex.recent_history(NOW - timedelta(days=90))
    finally:
        transport.close()
        original_client.close()

    assert [row[0] for row in episode_seen] == [1, 2, 3]
    assert episode_seen[0][1]["message"] == "Sonarr episodes, 1 of 3: The Office."
    assert episode_seen[0][1]["done"] == 1
    assert episode_seen[0][1]["total"] == 3
    assert episode_seen[1][1]["message"] == "Sonarr episodes, 2 of 3: Parks and Recreation."
    assert episode_seen[2][1]["message"] == "Sonarr episodes, 3 of 3: Untagged."
    messages = [item[0] for item in seen]
    assert messages[0] == "Checking Sonarr and Plex."
    assert "Skipping 1 show tagged keep-all." in messages
    assert 'Plex library "TV Shows", 0 shows so far.' in messages
    assert 'Plex library "TV Shows", 2 shows so far.' in messages
    assert 'Plex library "Anime", 2 shows so far.' in messages
    assert 'Plex library "Anime", 3 shows so far.' in messages
    assert "Plex history, 2 plays inside the 90-day window." in messages
    assert "Plex history, 3 plays inside the 90-day window." in messages
    assert "Filling in play details, 1 lookup." in messages
    assert messages[-2:] == ["Checking shows, 2 of 3.", "Checking shows, 3 of 3."]
    assert seen[-1] == ("Checking shows, 3 of 3.", 3, 3)
    assert ("/library/sections/2/all", 'Plex library "TV Shows", 0 shows so far.') in during
    assert ("/library/sections/3/all", 'Plex library "Anime", 2 shows so far.') in during
    history_hits = [message for path, message in during if path == "/status/sessions/history/all"]
    assert "Plex history, 2 plays inside the 90-day window." in history_hits
    assert all(TOKEN not in message for message in messages)
    assert report.error is None
    assert len(report.results) == 4
    ignored = [result for result in report.results if result.action == "ignore"]
    assert [result.title for result in ignored] == ["Archived"]
    assert ignored[0].bytes_to_free == 0
    assert ignored[0].later_bytes == 0


def test_status_hides_progress_when_idle(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    from fastapi.testclient import TestClient

    from app.main import app, holder

    with TestClient(app) as client:
        try:
            idle = client.get("/api/status").json()
            assert idle["running"] is False
            assert idle["progress"] is None
            holder.progress.update("stale", done=1, total=2)
            assert client.get("/api/status").json()["progress"] is None
            holder.running = True
            holder.progress.update("Sonarr episodes, 2 of 4: The Office.", done=2, total=4)
            live = client.get("/api/status").json()
            assert live["running"] is True
            assert live["progress"] == {
                "message": "Sonarr episodes, 2 of 4: The Office.",
                "done": 2,
                "total": 4,
            }
            holder.progress.update('Plex library "TV Shows", 120 shows so far.', done=120, total=None)
            scanned = client.get("/api/status").json()["progress"]
            assert scanned["total"] is None
            assert scanned["done"] == 120
        finally:
            holder.running = False
            holder.progress.clear()


def test_library_page_polls_progress_while_a_run_is_going():
    page = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "status_page.html"
    ).read_text(encoding="utf-8")
    assert 'id="progress-text"' in page
    assert 'id="progress-fill"' in page
    assert "setInterval(pollOnce, 1000)" in page
    assert "status.running" in page
    assert "/api/status" in page
    assert "Reading libraries…" in page
    assert "Preview mode" in page
