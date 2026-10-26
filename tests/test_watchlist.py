"""Plex watchlist intake and the Radarr client, with HTTP mocked."""

import json
import logging
from datetime import timedelta
from pathlib import Path

import httpx

from app.config import SettingsWrite, apply_update, clean_rss_url, load_settings, public_settings, save_settings
from app.engine import watch_for
from app.intake import apply_watchlist_marks, run_watchlist_intake, season_one_gap
from app.models import WatchIndex
from app.policy import decide
from app.radarr import RadarrClient
from app.sonarr import SonarrClient
from app.state import State
from app.watchlist import identity_key, parse_discover_metadata, parse_rss
from tests.builders import NOW, episode, make_series, policy, watch
from tests.test_admin import _connected, _quiet_env
from tests.test_policy import office

OWN = "https://rss.example.test/mine?token=owner-secret"
FRIENDS = "https://rss.example.test/friends?token=friend-secret"
OFFICE_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>The Office (2005)</title>
    <category>show</category>
    <guid>plex://show/abc123</guid>
    <author>Ada</author>
  </item>
  <item>
    <title>Dune (2021)</title>
    <category>movie</category>
    <guid>imdb://tt1160419</guid>
    <author>Grace</author>
  </item>
</channel></rss>
"""
FRIENDS_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Parks and Recreation</title>
    <category>show</category>
    <guid>tvdb://84912</guid>
    <author>Lin</author>
  </item>
</channel></rss>
"""
OFFICE_META = {
    "type": "show",
    "title": "The Office",
    "year": 2005,
    "guid": "plex://show/abc123",
    "Guid": [
        {"id": "imdb://tt0386676"},
        {"id": "tvdb://73244"},
        {"id": "tmdb://2316"},
    ],
}


def _xml(title, category, guid, author):
    return f"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>{title}</title><category>{category}</category><guid>{guid}</guid><author>{author}</author></item>
    </channel></rss>"""


class Api:
    def __init__(self):
        self.calls: list[tuple] = []
        self.series: list[dict] = []
        self.episodes: dict[str, list[dict]] = {}
        self.movies: list[dict] = []
        self.lookups: dict[tuple[str, str], dict] = {}
        self.tags: dict[str, list[dict]] = {"sonarr.local": [], "radarr.local": []}
        self.rss = {"/mine": OFFICE_RSS, "/friends": FRIENDS_RSS}
        self.metadata = {"abc123": OFFICE_META}
        self.discover: dict | None = None
        self._next_id = 20

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.host, request.url.path, dict(request.url.params), body))
        host = request.url.host
        path = request.url.path
        if host == "rss.example.test":
            xml = self.rss.get(path)
            if xml is None:
                return httpx.Response(404, text="missing feed")
            return httpx.Response(200, text=xml)
        if host in {"discover.provider.plex.tv", "metadata.provider.plex.tv"}:
            if path == "/library/sections/watchlist/all":
                return httpx.Response(200, json=self.discover or {"MediaContainer": {"Metadata": [], "totalSize": 0}})
            key = path.rsplit("/", 1)[-1]
            row = self.metadata.get(key)
            if row is None:
                return httpx.Response(404, json={"error": "missing"})
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [row]}})
        if host == "sonarr.local":
            return self._arr("sonarr.local", request, body, series=True)
        if host == "radarr.local":
            return self._arr("radarr.local", request, body, series=False)
        return httpx.Response(404, json={"error": f"unexpected {host}{path}"})

    def _arr(self, host: str, request: httpx.Request, body: dict | None, *, series: bool) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/api/v3/tag" and method == "GET":
            return httpx.Response(200, json=self.tags[host])
        if path == "/api/v3/tag" and method == "POST":
            created = {"id": len(self.tags[host]) + 1, "label": (body or {})["label"]}
            self.tags[host].append(created)
            return httpx.Response(200, json=created)
        if path == "/api/v3/rootfolder":
            folder = "/tv" if series else "/movies"
            return httpx.Response(200, json=[{"path": folder, "freeSpace": 5}])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 4 if series else 6, "name": "HD"}])
        if path == "/api/v3/system/status":
            return httpx.Response(200, json={"version": "5.0.0"})
        if series and path == "/api/v3/series" and method == "GET":
            return httpx.Response(200, json=self.series)
        if series and path == "/api/v3/series/lookup":
            found = self.lookups.get(("show", request.url.params.get("term", "")))
            return httpx.Response(200, json=[] if found is None else [found])
        if series and path == "/api/v3/series" and method == "POST":
            self._next_id += 1
            created = {"id": self._next_id, "title": (body or {}).get("title")}
            return httpx.Response(201, json=created)
        if series and path.startswith("/api/v3/series/") and method == "PUT":
            return httpx.Response(200, json=body or {})
        if series and path == "/api/v3/episode" and method == "GET":
            return httpx.Response(200, json=self.episodes.get(request.url.params.get("seriesId", ""), []))
        if series and path == "/api/v3/episode/monitor":
            return httpx.Response(200, json={})
        if path == "/api/v3/command" and method == "POST":
            return httpx.Response(201, json={"id": 1})
        if not series and path == "/api/v3/movie" and method == "GET":
            return httpx.Response(200, json=self.movies)
        if not series and path == "/api/v3/movie/lookup":
            found = self.lookups.get(("movie", request.url.params.get("term", "")))
            return httpx.Response(200, json=[] if found is None else [found])
        if not series and path == "/api/v3/movie" and method == "POST":
            self._next_id += 1
            return httpx.Response(201, json={"id": self._next_id, "title": (body or {}).get("title")})
        if not series and path.startswith("/api/v3/movie/") and method == "PUT":
            return httpx.Response(200, json=body or {})
        return httpx.Response(404, json={"error": f"unexpected {method} {path}"})


def _settings(**overrides) -> object:
    values = dict(
        dry_run=False,
        plex_watchlist_rss_url=OWN,
        plex_friends_watchlist_rss_url=FRIENDS,
        sonarr_root_folder="/tv",
        sonarr_quality_profile_id=4,
        sonarr_tag="season-keeper",
        radarr_url="http://radarr.local:7878",
        radarr_api_key="radarr-key-value",
        radarr_root_folder="/movies",
        radarr_quality_profile_id=6,
        radarr_tag="season-keeper",
        tv_intake_enabled=True,
        movie_intake_enabled=True,
    )
    values.update(overrides)
    return _connected(**values)


def _run(api: Api, settings, state: State, now=NOW):
    transport = httpx.MockTransport(api.handle)
    client = httpx.Client(transport=transport)
    sonarr = SonarrClient(str(settings.sonarr_url).rstrip("/"), settings.sonarr_api_key, 5, client=client)
    radarr = None
    if settings.radarr_configured:
        radarr = RadarrClient(str(settings.radarr_url).rstrip("/"), settings.radarr_api_key, 5, client=client)
    try:
        return run_watchlist_intake(settings, state, now, sonarr=sonarr, radarr=radarr, http=client)
    finally:
        client.close()


def _posts(api: Api, host: str, path: str) -> list[dict]:
    return [call[4] for call in api.calls if call[0] == "POST" and call[1] == host and call[2] == path]


def _commands(api: Api, host: str) -> list[dict]:
    return _posts(api, host, "/api/v3/command")


def test_rss_keeps_the_author_and_external_ids():
    own = parse_rss(OFFICE_RSS, default_author="You")
    friends = parse_rss(FRIENDS_RSS, default_author="A friend")
    assert [(item.added_by, item.media, item.title, item.tvdb_id, item.imdb_id) for item in own] == [
        ("Ada", "show", "The Office", None, None),
        ("Grace", "movie", "Dune", None, "tt1160419"),
    ]
    assert own[0].plex_guid == "plex://show/abc123"
    assert own[0].year == 2005
    assert friends[0].added_by == "Lin"
    assert friends[0].tvdb_id == 84912
    assert friends[0].media == "show"


def test_discover_metadata_reads_guids_and_a_missing_plex_key_is_resolved():
    items = parse_discover_metadata({"MediaContainer": {"Metadata": [OFFICE_META]}}, added_by="You")
    assert items[0].tvdb_id == 73244
    assert items[0].imdb_id == "tt0386676"
    assert items[0].tmdb_id == 2316
    assert identity_key(items[0]).startswith("show:imdb:")


def test_new_show_and_movie_are_added_and_a_second_poll_does_not_repeat(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="trimarr")
    api = Api()
    api.lookups[("show", "tvdb:73244")] = {
        "title": "The Office",
        "year": 2005,
        "tvdbId": 73244,
        "seasons": [
            {"seasonNumber": 0, "monitored": True},
            {"seasonNumber": 1, "monitored": False},
            {"seasonNumber": 2, "monitored": True},
        ],
    }
    api.lookups[("show", "tvdb:84912")] = {
        "title": "Parks and Recreation",
        "tvdbId": 84912,
        "seasons": [{"seasonNumber": 1, "monitored": False}],
    }
    api.lookups[("movie", "imdb:tt1160419")] = {
        "title": "Dune",
        "year": 2021,
        "tmdbId": 438631,
        "imdbId": "tt1160419",
    }
    state = State(tmp_path / "state.sqlite")
    results = _run(api, _settings(), state)
    by_title = {row["title"]: row for row in results}
    assert by_title["The Office (2005)"]["action"] == "added"
    assert by_title["The Office (2005)"]["added_by"] == "Ada"
    assert by_title["Parks and Recreation"]["added_by"] == "Lin"
    assert by_title["Dune (2021)"]["action"] == "added"

    show_adds = _posts(api, "sonarr.local", "/api/v3/series")
    assert len(show_adds) == 2
    office = next(body for body in show_adds if body["tvdbId"] == 73244)
    assert office["addOptions"] == {"monitor": "firstSeason", "searchForMissingEpisodes": True}
    assert office["rootFolderPath"] == "/tv"
    assert office["qualityProfileId"] == 4
    assert office["monitored"] is True
    assert office["tags"] == [1]
    seasons = {season["seasonNumber"]: season["monitored"] for season in office["seasons"]}
    assert seasons == {0: False, 1: True, 2: False}
    assert _commands(api, "sonarr.local") == []

    movie = _posts(api, "radarr.local", "/api/v3/movie")[0]
    assert movie["monitored"] is True
    assert movie["addOptions"] == {"searchForMovie": True}
    assert movie["rootFolderPath"] == "/movies"
    assert movie["qualityProfileId"] == 6
    assert movie["tags"] == [1]
    assert all(call[0] != "DELETE" for call in api.calls)

    assert "Ada added The Office (2005) to a Plex watchlist. Added it to Sonarr" in caplog.text
    assert "Grace added Dune (2021) to a Plex watchlist. Added it to Radarr and searched." in caplog.text
    assert "Lin added Parks and Recreation to a Plex watchlist." in caplog.text
    assert state.activity_totals()["watchlist_adds"] == 3
    assert {row["kind"] for row in state.list_activity()} == {"watchlist"}

    before = len(api.calls)
    again = _run(api, _settings(), state, NOW + timedelta(minutes=5))
    assert again == []
    assert not any(call[0] == "POST" and call[2] == "/api/v3/series" for call in api.calls[before:])
    assert state.watchlist_marks()[73244][1] == "Ada"
    assert state.watchlist_marks()[73244][0] == NOW


def test_discover_is_the_fallback_when_both_feeds_are_blank(tmp_path):
    api = Api()
    api.discover = {
        "MediaContainer": {
            "totalSize": 1,
            "Metadata": [OFFICE_META],
        }
    }
    api.lookups[("show", "tvdb:73244")] = {
        "title": "The Office",
        "tvdbId": 73244,
        "seasons": [{"seasonNumber": 1, "monitored": False}],
    }
    state = State(tmp_path / "state.sqlite")
    results = _run(api, _settings(plex_watchlist_rss_url="", plex_friends_watchlist_rss_url=""), state)
    assert results[0]["added_by"] == "You"
    assert results[0]["action"] == "added"
    assert not any(call[1] == "rss.example.test" for call in api.calls)
    assert any(call[2] == "/library/sections/watchlist/all" for call in api.calls)


def test_a_missing_season_one_is_monitored_and_searched_without_specials(tmp_path):
    api = Api()
    api.rss = {"/mine": _xml("The Office", "show", "tvdb://73244", "Ada"), "/friends": "<rss><channel></channel></rss>"}
    api.series = [
        {
            "id": 9,
            "title": "The Office",
            "year": 2005,
            "tvdbId": 73244,
            "path": "/tv/The Office",
            "monitored": True,
            "tags": [],
            "seasons": [
                {"seasonNumber": 0, "monitored": True},
                {"seasonNumber": 1, "monitored": False},
                {"seasonNumber": 2, "monitored": True},
            ],
        }
    ]
    api.episodes["9"] = [
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "monitored": False, "hasFile": False, "airDateUtc": "2020-01-01T00:00:00Z"},
        {
            "id": 2,
            "seasonNumber": 0,
            "episodeNumber": 1,
            "monitored": True,
            "hasFile": True,
            "episodeFileId": 50,
            "airDateUtc": "2020-01-01T00:00:00Z",
        },
    ]
    state = State(tmp_path / "state.sqlite")
    results = _run(api, _settings(), state)
    assert results[0]["action"] == "searched"
    assert _posts(api, "sonarr.local", "/api/v3/series") == []
    updated = next(call[4] for call in api.calls if call[0] == "PUT" and call[2] == "/api/v3/series/9")
    seasons = {season["seasonNumber"]: season["monitored"] for season in updated["seasons"]}
    assert seasons[0] is False
    assert seasons[1] is True
    assert seasons[2] is True
    assert updated["tags"] == [1]
    monitors = [call[4] for call in api.calls if call[2] == "/api/v3/episode/monitor"]
    assert {"episodeIds": [1], "monitored": True} in monitors
    assert {"episodeIds": [2], "monitored": False} in monitors
    assert _commands(api, "sonarr.local") == [{"name": "SeasonSearch", "seriesId": 9, "seasonNumber": 1}]


def test_a_complete_season_one_is_not_searched(tmp_path):
    api = Api()
    api.rss = {"/mine": _xml("The Office", "show", "tvdb://73244", "Ada"), "/friends": "<rss><channel></channel></rss>"}
    api.series = [
        {
            "id": 9,
            "title": "The Office",
            "year": 2005,
            "tvdbId": 73244,
            "path": "/tv/The Office",
            "monitored": True,
            "tags": [1],
            "seasons": [{"seasonNumber": 1, "monitored": True}],
        }
    ]
    api.episodes["9"] = [
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True, "episodeFileId": 11, "airDateUtc": "2020-01-01T00:00:00Z"}
    ]
    state = State(tmp_path / "state.sqlite")
    results = _run(api, _settings(), state)
    assert results[0]["action"] == "skipped"
    assert _commands(api, "sonarr.local") == []
    assert not any(call[0] == "PUT" for call in api.calls)
    assert state.activity_totals()["watchlist_adds"] == 0


def test_an_existing_movie_is_monitored_and_searched_until_it_has_a_file(tmp_path):
    api = Api()
    api.rss = {
        "/mine": _xml("Dune", "movie", "tmdb://438631", "Grace"),
        "/friends": "<rss><channel></channel></rss>",
    }
    api.movies = [{"id": 4, "title": "Dune", "tmdbId": 438631, "monitored": False, "hasFile": False, "tags": []}]
    state = State(tmp_path / "state.sqlite")
    results = _run(api, _settings(), state)
    assert results[0]["action"] == "searched"
    updated = next(call[4] for call in api.calls if call[0] == "PUT" and call[2] == "/api/v3/movie/4")
    assert updated["monitored"] is True
    assert updated["tags"] == [1]
    assert _commands(api, "radarr.local") == [{"name": "MoviesSearch", "movieIds": [4]}]
    assert _posts(api, "radarr.local", "/api/v3/movie") == []

    api.movies = [{"id": 4, "title": "Dune", "tmdbId": 438631, "monitored": True, "hasFile": True, "tags": [1]}]
    state_b = State(tmp_path / "done.sqlite")
    done = _run(api, _settings(), state_b)
    assert done[0]["action"] == "skipped"
    assert _commands(api, "radarr.local") == [{"name": "MoviesSearch", "movieIds": [4]}]


def test_preview_records_the_plan_and_restarts_the_clock_without_adding(tmp_path):
    api = Api()
    api.rss = {"/mine": _xml("The Office", "show", "tvdb://73244", "Ada"), "/friends": "<rss><channel></channel></rss>"}
    api.lookups[("show", "tvdb:73244")] = {"title": "The Office", "tvdbId": 73244, "seasons": [{"seasonNumber": 1}]}
    state = State(tmp_path / "state.sqlite")
    settings = _settings(dry_run=True)
    first = _run(api, settings, state)
    assert first[0]["action"] == "preview"
    assert first[0]["detail"].startswith("Would add The Office to Sonarr")
    assert _posts(api, "sonarr.local", "/api/v3/series") == []
    assert state.watchlist_marks()[73244][1] == "Ada"
    _run(api, settings, state, NOW + timedelta(days=1))
    assert len(state.list_activity(kind="watchlist")) == 1
    assert state.watchlist_marks()[73244][0] == NOW
    live = _run(api, settings.model_copy(update={"dry_run": False}), state, NOW + timedelta(days=1))
    assert live[0]["action"] == "added"
    assert len(_posts(api, "sonarr.local", "/api/v3/series")) == 1


def test_missing_add_settings_are_retried_without_a_second_log_line(tmp_path):
    api = Api()
    api.rss = {"/mine": _xml("The Office", "show", "tvdb://73244", "Ada"), "/friends": "<rss><channel></channel></rss>"}
    state = State(tmp_path / "state.sqlite")
    settings = _settings(sonarr_root_folder="", sonarr_quality_profile_id=None)
    first = _run(api, settings, state)
    assert first[0]["action"] == "needs-settings"
    _run(api, settings, state, NOW + timedelta(minutes=5))
    assert len(state.list_activity(kind="watchlist")) == 1
    assert _posts(api, "sonarr.local", "/api/v3/series") == []
    assert 73244 in state.watchlist_marks()


def test_watchlist_inside_the_window_holds_and_an_old_one_still_prunes(tmp_path):
    fresh = decide(
        office(),
        watch(watchlisted_at=NOW - timedelta(days=1), watchlisted_by="Ada"),
        NOW,
        policy(),
    )
    assert fresh.action == "hold"
    assert fresh.delete_file_ids == ()
    assert "Ada added this to a Plex watchlist" in fresh.reason
    assert "nothing was deleted" in fresh.reason

    stale = decide(
        office(),
        watch(watchlisted_at=NOW - timedelta(days=91), watchlisted_by="Ada"),
        NOW,
        policy(),
    )
    assert stale.action == "prune"

    playing = make_series(
        [episode(1, 1, 1, file_id=11), episode(2, 2, 1, aired=True)],
        season_monitored={1: True, 2: False},
    )
    restored = decide(
        playing,
        watch(
            season1_last_viewed_at=NOW,
            last_viewed_at=NOW,
            watchlisted_at=NOW,
            watchlisted_by="Ada",
        ),
        NOW,
        policy(),
    )
    assert restored.action == "restore"
    assert restored.search_seasons == (2,)

    state = State(tmp_path / "state.sqlite")
    state.remember_watchlist_mark(100, NOW - timedelta(days=2), "Ada")
    index = WatchIndex(
        by_tvdb={},
        matched_tvdb={100},
        match_source={100: "tvdb"},
        history_complete=True,
        ambiguous_plays=[],
    )
    apply_watchlist_marks(index, state.watchlist_marks())
    viewed = watch_for(office(), index)
    assert viewed.watchlisted_by == "Ada"
    assert decide(office(), viewed, NOW, policy()).action == "hold"
    assert season_one_gap(office()) is False


def test_settings_round_trip_and_the_pages_mention_the_new_controls(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    current = _connected()
    updated = apply_update(
        current,
        SettingsWrite(
            sonarr_url="http://sonarr.local:8989",
            plex_url="http://plex.local:32400",
            plex_watchlist_rss_url=OWN,
            plex_friends_watchlist_rss_url=FRIENDS,
            watchlist_interval_minutes=5,
            tv_intake_enabled=True,
            movie_intake_enabled=False,
            sonarr_root_folder="/tv",
            sonarr_quality_profile_id=4,
            radarr_url="http://radarr.local:7878",
            radarr_api_key="radarr-secret-key",
            radarr_root_folder="/movies",
            radarr_quality_profile_id=6,
            radarr_tag="season-keeper",
        ),
    )
    assert updated.watchlist_interval_minutes == 5
    assert updated.movie_intake_enabled is False
    assert updated.tv_intake_enabled is True
    assert updated.radarr_configured is True
    updated = updated.model_copy(update={"data_dir": tmp_path})
    save_settings(updated)
    loaded = load_settings()
    assert loaded.plex_watchlist_rss_url == OWN
    assert loaded.radarr_root_folder == "/movies"
    assert loaded.sonarr_quality_profile_id == 4
    public = public_settings(loaded)
    assert "radarr-secret-key" not in json.dumps(public)
    assert public["radarr_api_key_set"] is True
    assert public["plex_friends_watchlist_rss_url"] == FRIENDS
    assert clean_rss_url(OWN, "My Watchlist RSS") == OWN
    try:
        clean_rss_url("https://user:secret@rss.example.test/mine", "My Watchlist RSS")
    except ValueError as exc:
        assert "secret" not in str(exc)
    else:
        raise AssertionError("expected a userinfo rss url to be rejected")

    kept = apply_update(
        loaded,
        SettingsWrite(sonarr_url="http://sonarr.local:8989", plex_url="http://plex.local:32400"),
    )
    assert kept.plex_watchlist_rss_url == OWN
    assert str(kept.radarr_url).rstrip("/") == "http://radarr.local:7878"
    assert kept.radarr_api_key == "radarr-secret-key"
    assert kept.sonarr_root_folder == "/tv"
    assert kept.movie_intake_enabled is False

    root = Path(__file__).resolve().parents[1] / "app"
    admin = (root / "admin_page.html").read_text(encoding="utf-8")
    stats = (root / "stats_page.html").read_text(encoding="utf-8")
    assert "My Watchlist RSS" in admin
    assert "Friends' Watchlist RSS" in admin
    assert "Radarr URL" in admin
    assert "Test Radarr" in admin
    assert "Sonarr root folder" in admin
    assert "Sonarr quality profile" in admin
    assert 'id="tv-intake" type="checkbox" checked' in admin
    assert 'id="movie-intake" type="checkbox" checked' in admin
    assert "Watchlist adds" in stats
    assert 'data-kind="watchlist"' in stats


def test_errors_redact_the_feed_and_the_radarr_key():
    from app.main import _safe_detail

    settings = _connected(
        plex_watchlist_rss_url=OWN,
        radarr_api_key="radarr-secret-key",
    )
    detail = _safe_detail(RuntimeError(f"boom {OWN} radarr-secret-key"), settings)
    assert "owner-secret" not in detail
    assert "radarr-secret-key" not in detail


def test_the_watchlist_job_uses_its_own_interval(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    from fastapi.testclient import TestClient

    from app.main import app, holder

    with TestClient(app) as client:
        saved = client.post(
            "/api/settings",
            json={
                "sonarr_url": "http://sonarr.local:8989",
                "sonarr_api_key": "SonarrKeyValue9999",
                "plex_url": "http://plex.local:32400",
                "plex_token": "PlexTokenValue8888",
                "dry_run": True,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["watchlist_interval_minutes"] == 5
        assert saved.json()["tv_intake_enabled"] is True
        assert saved.json()["movie_intake_enabled"] is True
        job = holder.scheduler.get_job("watchlist")
        assert job is not None
        assert job.trigger.interval == timedelta(minutes=5)
        changed = client.post(
            "/api/settings",
            json={
                "sonarr_url": "http://sonarr.local:8989",
                "plex_url": "http://plex.local:32400",
                "watchlist_interval_minutes": 2,
            },
        )
        assert changed.status_code == 200
        assert holder.scheduler.get_job("watchlist").trigger.interval == timedelta(minutes=2)
        assert holder.scheduler.get_job("cycle") is not None


VISION_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>VisionQuest</title>
    <category>show</category>
    <guid>tvdb://426606</guid>
    <author>Ken</author>
  </item>
</channel></rss>
"""


def _vision_lookup() -> dict:
    return {
        "title": "VisionQuest",
        "tvdbId": 426606,
        "seasons": [{"seasonNumber": 1, "monitored": False}],
    }


class _RedirectFeeds(Api):
    """Both watchlist feeds answer with a 302. The client has to follow it."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "rss.example.test" and request.url.path == "/mine":
            self.calls.append((request.method, request.url.host, request.url.path, {}, None))
            return httpx.Response(302, headers={"Location": "https://rss.example.test/mine/final"})
        if request.url.host == "rss.example.test" and request.url.path == "/mine/final":
            self.calls.append((request.method, request.url.host, request.url.path, {}, None))
            return httpx.Response(200, text=OFFICE_RSS)
        if request.url.host == "rss.example.test" and request.url.path == "/friends":
            self.calls.append((request.method, request.url.host, request.url.path, {}, None))
            return httpx.Response(302, headers={"Location": "https://rss.example.test/friends/final"})
        if request.url.host == "rss.example.test" and request.url.path == "/friends/final":
            self.calls.append((request.method, request.url.host, request.url.path, {}, None))
            return httpx.Response(200, text=VISION_RSS)
        return super().handle(request)


class _FriendsBody(Api):
    def __init__(self):
        super().__init__()
        self.friends_status = 200
        self.friends_body = "<html><title>Found</title></html>"

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "rss.example.test" and request.url.path == "/friends":
            return httpx.Response(self.friends_status, text=self.friends_body)
        return super().handle(request)


def test_both_watchlist_feeds_follow_redirects(tmp_path):
    api = _RedirectFeeds()
    api.lookups[("show", "tvdb:73244")] = {
        "title": "The Office",
        "tvdbId": 73244,
        "seasons": [{"seasonNumber": 1, "monitored": False}],
    }
    api.lookups[("movie", "imdb:tt1160419")] = {
        "title": "Dune",
        "year": 2021,
        "tmdbId": 438631,
        "imdbId": "tt1160419",
    }
    api.lookups[("show", "tvdb:426606")] = _vision_lookup()
    state = State(tmp_path / "state.sqlite")
    results = _run(api, _settings(), state)
    titles = {row["title"] for row in results}
    assert "VisionQuest" in titles
    assert "The Office (2005)" in titles
    paths = [call[2] for call in api.calls if call[1] == "rss.example.test"]
    assert "/mine/final" in paths
    assert "/friends/final" in paths
    added = _posts(api, "sonarr.local", "/api/v3/series")
    assert any(body.get("tvdbId") == 426606 for body in added)
    status = state.watchlist_feed_status()
    assert status["error"] is None
    assert status["checked_at"]
    assert status["last_success_at"] == status["checked_at"]


def test_a_non_xml_feed_is_reported_and_a_later_good_fetch_adds_what_was_missed(tmp_path):
    api = _FriendsBody()
    api.lookups[("show", "tvdb:73244")] = {
        "title": "The Office",
        "tvdbId": 73244,
        "seasons": [{"seasonNumber": 1, "monitored": False}],
    }
    api.lookups[("movie", "imdb:tt1160419")] = {
        "title": "Dune",
        "year": 2021,
        "tmdbId": 438631,
        "imdbId": "tt1160419",
    }
    api.lookups[("show", "tvdb:426606")] = _vision_lookup()
    state = State(tmp_path / "state.sqlite")
    # A previous error row must not hide an item that is still in the feed.
    state.save_watchlist_item(
        item_key="show:tvdb:426606",
        media="show",
        title="VisionQuest",
        added_by="Ken",
        plex_guid=None,
        imdb_id=None,
        tvdb_id=426606,
        tmdb_id=None,
        first_seen_at=(NOW - timedelta(days=5)).isoformat(),
        action="error",
        detail="The watchlist item could not be processed.",
    )
    first = _run(api, _settings(), state)
    assert all(row["title"] != "VisionQuest" for row in first)
    failed = state.watchlist_feed_status()
    assert failed["error"] == "The Friends' Watchlist feed was not valid RSS."
    assert failed["checked_at"]
    assert failed["last_success_at"] is None
    assert state.get_watchlist_item("show:tvdb:426606")["action"] == "error"
    assert not any(body.get("tvdbId") == 426606 for body in _posts(api, "sonarr.local", "/api/v3/series"))

    api.friends_body = VISION_RSS
    second = _run(api, _settings(), state, NOW + timedelta(minutes=5))
    vision = next(row for row in second if row["title"] == "VisionQuest")
    assert vision["action"] == "added"
    assert any(body.get("tvdbId") == 426606 for body in _posts(api, "sonarr.local", "/api/v3/series"))
    recovered = state.watchlist_feed_status()
    assert recovered["error"] is None
    assert recovered["last_success_at"] == recovered["checked_at"]
    assert recovered["checked_at"] != failed["checked_at"]

    api.friends_body = "<html>moved</html>"
    _run(api, _settings(), state, NOW + timedelta(minutes=10))
    again = state.watchlist_feed_status()
    assert again["error"] == "The Friends' Watchlist feed was not valid RSS."
    assert again["last_success_at"] == recovered["last_success_at"]
    assert state.get_watchlist_item("show:tvdb:426606")["action"] == "added"


def test_health_and_the_page_show_the_watchlist_feed_error(tmp_path, monkeypatch):
    _quiet_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STARTUP_DELAY_SECONDS", "3600")
    monkeypatch.setenv("RUN_ON_STARTUP", "false")
    from fastapi.testclient import TestClient

    from app.main import app, holder

    page = Path(__file__).resolve().parents[1] / "app" / "status_page.html"
    text = page.read_text(encoding="utf-8")
    assert "status.watchlist" in text
    assert "Last good fetch" in text
    assert "No successful fetch yet." in text

    with TestClient(app) as client:
        assert holder.state is not None
        holder.state.note_watchlist_feed(
            error="The Friends' Watchlist feed was not valid RSS.",
            at=NOW,
        )
        health = client.get("/health").json()
        assert health["watchlist"]["error"] == "The Friends' Watchlist feed was not valid RSS."
        assert health["watchlist"]["checked_at"]
        assert health["watchlist"]["last_success_at"] is None
        status = client.get("/api/status").json()
        assert status["watchlist"]["error"] == health["watchlist"]["error"]
        holder.state.note_watchlist_feed(error=None, at=NOW + timedelta(minutes=5))
        cleared = client.get("/health").json()["watchlist"]
        assert cleared["error"] is None
        assert cleared["last_success_at"]
        assert cleared["last_success_at"] != health["watchlist"]["checked_at"]
