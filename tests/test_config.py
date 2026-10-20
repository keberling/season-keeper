from app.config import Settings


def test_season_one_is_always_kept():
    settings = Settings(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="key",
        plex_url="http://plex.local:32400",
        plex_token="token",
        keep_seasons="0",
    )
    assert settings.keep_season_numbers == frozenset({0, 1})


def test_dry_run_defaults_to_preview():
    settings = Settings(
        sonarr_url="http://sonarr.local:8989",
        sonarr_api_key="key",
        plex_url="http://plex.local:32400",
        plex_token="token",
    )
    assert settings.dry_run is True
    assert settings.watch_window_days == 90


def test_blank_settings_are_not_configured():
    settings = Settings()
    assert settings.configured is False
    assert settings.dry_run is True
    assert settings.keep_season_numbers == frozenset({1})
