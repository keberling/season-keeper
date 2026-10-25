import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.models import PlexShow
from app.plex import PlexClient, build_watch_index, extract_guids, tvdb_from_guids

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def test_tvdb_guids():
    assert tvdb_from_guids(["plex://show/abc", "tvdb://73244"]) == 73244
    assert tvdb_from_guids(["com.plexapp.agents.thetvdb://81189?lang=en"]) == 81189
    assert tvdb_from_guids(["imdb://tt123"]) is None


def test_extract_guids_from_plex_shapes():
    assert extract_guids({"guid": "plex://show/abc", "Guid": [{"id": "tvdb://7"}]}) == [
        "plex://show/abc",
        "tvdb://7",
    ]


def test_history_and_session_land_on_the_tvdb_show():
    shows = [PlexShow("50", "Example", 100)]
    history = [
        {
            "type": "episode",
            "grandparentRatingKey": "50",
            "parentIndex": 1,
            "viewedAt": int(NOW.timestamp()) - 3600,
            "grandparentTitle": "Example",
        }
    ]
    sessions = [
        {
            "type": "episode",
            "grandparentRatingKey": "50",
            "parentIndex": 2,
            "User": {"title": "Ada"},
            "grandparentTitle": "Example",
        }
    ]
    index = build_watch_index(
        shows,
        history,
        sessions,
        history_complete=True,
        overrides={},
        now=NOW,
    )

    activity = index.by_tvdb[100]
    assert activity.season1_last_viewed_at is not None
    assert activity.season_last_viewed[1] == activity.season1_last_viewed_at
    assert 2 in activity.season_last_viewed
    assert 2 in activity.active_seasons
    assert "Ada" in activity.active_users
    assert index.reliable_for_prune()


def test_a_play_with_no_show_id_does_not_block_prune():
    index = build_watch_index(
        [PlexShow("50", "Example", 100)],
        [{"type": "episode", "title": "Pilot", "viewedAt": int(NOW.timestamp())}],
        [],
        history_complete=True,
        overrides={},
        now=NOW,
    )
    assert index.ambiguous_plays == ["Pilot"]
    assert index.by_tvdb == {}
    assert index.reliable_for_prune() is True


def test_incomplete_history_blocks_prune():
    index = build_watch_index(
        [PlexShow("50", "Example", 100)],
        [],
        [],
        history_complete=False,
        overrides={},
        now=NOW,
    )
    assert index.reliable_for_prune() is False


def test_override_replaces_a_missing_guid():
    shows = [PlexShow("50", "Example", None)]
    index = build_watch_index(
        shows,
        [],
        [],
        history_complete=True,
        overrides={100: "50"},
        now=NOW,
    )
    assert 100 in index.matched_tvdb
    assert index.match_source[100] == "override"


def test_movies_are_ignored():
    index = build_watch_index(
        [PlexShow("50", "Example", 100)],
        [{"type": "movie", "title": "A Movie", "viewedAt": int(NOW.timestamp())}],
        [],
        history_complete=True,
        overrides={},
        now=NOW,
    )
    assert index.ambiguous_plays == []
    assert index.by_tvdb == {}


def _played(hours_ago: int = 1) -> int:
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


def test_exact_show_title_holds_that_show_only():
    index = build_watch_index(
        [PlexShow("50", "The Office (US)", 73244), PlexShow("51", "NCIS", 72108)],
        [{
            "type": "episode",
            "title": "Pilot",
            "grandparentTitle": "The Office (US)",
            "parentIndex": 1,
            "viewedAt": _played(),
        }],
        [],
        history_complete=True,
        overrides={},
        now=NOW,
    )

    assert index.history_complete is True
    assert index.reliable_for_prune() is True
    assert index.ambiguous_plays == ["The Office (US)"]
    office = index.by_tvdb[73244]
    assert office.last_viewed_at is not None
    assert office.season1_last_viewed_at is None
    assert office.active_seasons == set()
    assert office.unknown_season_at == office.last_viewed_at
    assert 72108 not in index.by_tvdb


def test_episode_title_is_not_used_as_a_show_title():
    index = build_watch_index(
        [PlexShow("51", "NCIS", 72108)],
        [{"type": "episode", "title": "NCIS", "viewedAt": _played()}],
        [],
        history_complete=True,
        overrides={},
        now=NOW,
    )
    assert index.ambiguous_plays == ["NCIS"]
    assert index.by_tvdb == {}
    assert index.reliable_for_prune() is True


def test_show_title_match_is_exact():
    index = build_watch_index(
        [PlexShow("51", "NCIS", 72108)],
        [{"type": "episode", "grandparentTitle": "ncis", "viewedAt": _played()}],
        [],
        history_complete=True,
        overrides={},
        now=NOW,
    )
    assert 72108 not in index.by_tvdb

    padded = build_watch_index(
        [PlexShow("51", "NCIS", 72108)],
        [{"type": "episode", "grandparentTitle": " NCIS ", "viewedAt": _played()}],
        [],
        history_complete=True,
        overrides={},
        now=NOW,
    )
    assert 72108 in padded.by_tvdb
    assert padded.by_tvdb[72108].season1_last_viewed_at is None


def test_duplicate_titles_hold_each_show():
    index = build_watch_index(
        [
            PlexShow("1", "The Office (US)", 73244),
            PlexShow("2", "The Office (US)", 84912),
            PlexShow("3", "NCIS", 72108),
        ],
        [{"type": "episode", "grandparentTitle": "The Office (US)", "viewedAt": _played()}],
        [],
        history_complete=True,
        overrides={},
        now=NOW,
    )
    assert 73244 in index.by_tvdb
    assert 84912 in index.by_tvdb
    assert index.by_tvdb[73244].season1_last_viewed_at is None
    assert index.by_tvdb[84912].season1_last_viewed_at is None
    assert 72108 not in index.by_tvdb


def test_title_hold_follows_an_override_when_the_guid_is_missing():
    index = build_watch_index(
        [PlexShow("50", "The Office (US)", None)],
        [{"type": "episode", "grandparentTitle": "The Office (US)", "parentIndex": 1, "viewedAt": _played()}],
        [],
        history_complete=True,
        overrides={73244: "50"},
        now=NOW,
    )
    assert index.by_tvdb[73244].season1_last_viewed_at is None
    assert index.by_tvdb[73244].unknown_season_at is not None


def test_grandparent_key_is_the_show_id_without_a_title_guess():
    index = build_watch_index(
        [PlexShow("50", "The Office (US)", 73244)],
        [{
            "type": "episode",
            "grandparentKey": "/library/metadata/50",
            "parentIndex": 1,
            "grandparentTitle": "The Office (US)",
            "viewedAt": _played(),
        }],
        [],
        history_complete=True,
        overrides={},
        now=NOW,
    )
    assert index.ambiguous_plays == []
    assert index.by_tvdb[73244].season1_last_viewed_at is not None


def _plex(handler, hydrate_limit: int = 1) -> tuple[PlexClient, httpx.Client, httpx.Client]:
    plex = PlexClient("http://plex.local:32400", "token", 5, 5, 50, 100, hydrate_limit)
    original = plex._client
    transport = httpx.Client(transport=httpx.MockTransport(handler))
    plex._client = transport
    return plex, transport, original


def test_history_requests_guids_and_resolves_every_episode_key(caplog):
    caplog.set_level(logging.DEBUG, logger="trimarr.plex")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/status/sessions/history/all":
            assert request.url.params.get("includeGuids") == "1"
            assert "includeFields" not in request.url.params
            rows = [
                {
                    "type": "episode",
                    "key": "/library/metadata/77",
                    "viewedAt": _played(),
                },
                {
                    "type": "episode",
                    "ratingKey": "5",
                    "grandparentTitle": "The Office (US)",
                    "title": "Pilot",
                    "viewedAt": _played(2),
                },
                {
                    "type": "episode",
                    "grandparentRatingKey": "50",
                    "parentIndex": 2,
                    "viewedAt": _played(3),
                },
            ]
            return httpx.Response(200, json={"MediaContainer": {"Metadata": rows, "totalSize": 3}})
        if request.url.path == "/library/metadata/77":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [{
                "grandparentRatingKey": "51",
                "parentIndex": 4,
                "grandparentTitle": "NCIS",
                "type": "episode",
            }]}})
        if request.url.path == "/library/metadata/5":
            return httpx.Response(404, text="missing")
        return httpx.Response(200, json={"MediaContainer": {}})

    plex, transport, original = _plex(handler, hydrate_limit=1)
    try:
        history, complete = plex.recent_history(NOW - timedelta(days=90), window_days=90)
    finally:
        transport.close()
        original.close()

    assert complete is True
    assert history[0]["grandparentRatingKey"] == "51"
    assert history[0]["ratingKey"] == "77"
    assert history[0]["parentIndex"] == 4
    assert history[1]["historyDeleted"] is True
    assert not history[1].get("grandparentRatingKey")
    assert "/library/metadata/77" in seen
    assert "/library/metadata/5" in seen
    assert "3 plays in the window, 1 already had a show id, 1 hydrated, 1 still unresolved" in caplog.text

    index = build_watch_index(
        [PlexShow("50", "The Office (US)", 73244), PlexShow("51", "NCIS", 72108)],
        history,
        [],
        history_complete=complete,
        overrides={},
        now=NOW,
    )
    assert index.reliable_for_prune() is True
    assert index.by_tvdb[73244].season1_last_viewed_at is None
    assert index.by_tvdb[73244].unknown_season_at is not None
    ncis = index.by_tvdb[72108]
    assert ncis.last_viewed_at is not None
    assert ncis.season1_last_viewed_at is None
    assert ncis.active_seasons == set()
