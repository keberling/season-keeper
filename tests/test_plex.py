from datetime import datetime, timezone

from app.models import PlexShow
from app.plex import build_watch_index, extract_guids, tvdb_from_guids

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
    assert 2 in activity.active_seasons
    assert "Ada" in activity.active_users
    assert index.reliable_for_prune()


def test_a_play_with_no_show_id_blocks_prune():
    index = build_watch_index(
        [PlexShow("50", "Example", 100)],
        [{"type": "episode", "title": "Pilot", "viewedAt": int(NOW.timestamp())}],
        [],
        history_complete=True,
        overrides={},
        now=NOW,
    )
    assert index.ambiguous_plays == ["Pilot"]
    assert index.reliable_for_prune() is False


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
