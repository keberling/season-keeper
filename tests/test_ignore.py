"""The keep-all tag is the only show the schedule skips."""

from app.config import Settings
from app.models import RunReport, SeriesResult
from app.service import SeriesNotFound, set_keep_all
from app.state import State


def _office_result() -> SeriesResult:
    return SeriesResult(
        sonarr_id=5,
        title="The Office",
        year=2005,
        tvdb_id=73244,
        path="/tv/The Office",
        plex_matched=True,
        match_source="tvdb",
        action="prune",
        reason="No play in the window.",
        last_viewed_at=None,
        season1_last_viewed_at=None,
        active_seasons=[],
        delete_seasons=[2],
        search_seasons=[],
        searches_ran=[],
        searches_waiting=[],
        bytes_to_free=8000,
        dry_run=True,
        error=None,
        later_bytes=8000,
    )


class Sonarr:
    def __init__(self):
        self.payload = None
        self.rows = [
            {
                "id": 5,
                "title": "The Office",
                "year": 2005,
                "tvdbId": 73244,
                "path": "/tv/The Office",
                "monitored": True,
                "tags": [1],
                "seasons": [
                    {
                        "seasonNumber": 2,
                        "monitored": True,
                        "statistics": {"sizeOnDisk": 8000},
                        "images": [{"url": "/cover.jpg"}],
                    }
                ],
            }
        ]

    def ensure_tag(self, label):
        assert label == "keep-all"
        return 4

    def list_series(self):
        return self.rows

    def update_series(self, payload):
        self.payload = payload
        for row in self.rows:
            if int(row["id"]) == int(payload["id"]):
                row["tags"] = list(payload["tags"])
                row["seasons"] = payload["seasons"]


def _settings(tmp_path) -> Settings:
    return Settings(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="secret-key-value",
        plex_url="http://plex.local:32400",
        plex_token="plex-token-value",
        data_dir=tmp_path,
    )


def test_ignore_adds_keep_all_without_changing_monitoring(tmp_path):
    state = State(tmp_path / "season-keeper.sqlite")
    state.save_report(
        RunReport(
            started_at="t",
            finished_at="t",
            dry_run=True,
            history_complete=True,
            ambiguous_plays=[],
            results=[_office_result()],
            error=None,
        )
    )
    sonarr = Sonarr()
    ignored = set_keep_all(_settings(tmp_path), state, sonarr, 5, True)
    assert sonarr.payload["tags"] == [1, 4]
    assert sonarr.payload["monitored"] is True
    assert "statistics" not in sonarr.payload
    season = sonarr.payload["seasons"][0]
    assert season["monitored"] is True
    assert "statistics" not in season
    assert "images" not in season
    assert ignored["action"] == "ignore"
    assert ignored["bytes_to_free"] == 0
    assert ignored["later_bytes"] == 0
    assert "keep-all" in ignored["reason"]
    saved = state.load_report()
    assert saved["series"][0]["action"] == "ignore"
    assert saved["bytes_to_free"] == 0
    assert saved["later_bytes"] == 0

    included = set_keep_all(_settings(tmp_path), state, sonarr, 5, False)
    assert sonarr.payload["tags"] == [1]
    assert included["action"] == "skip"
    assert "Included again" in included["reason"]
    assert state.load_report()["series"][0]["action"] == "skip"


def test_ignore_missing_series_does_not_write(tmp_path):
    sonarr = Sonarr()
    try:
        set_keep_all(_settings(tmp_path), State(tmp_path / "season-keeper.sqlite"), sonarr, 99, True)
    except SeriesNotFound:
        pass
    else:
        raise AssertionError("expected a missing series")
    assert sonarr.payload is None
