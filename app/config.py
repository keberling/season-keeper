"""Container runtime settings, plus the Sonarr and Plex settings saved from the admin page."""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import PolicySettings

logger = logging.getLogger("trimarr")

STORE_NAME = "settings.json"
STORED_FIELDS = (
    "sonarr_url",
    "sonarr_api_key",
    "plex_url",
    "plex_token",
    "dry_run",
    "watch_window_days",
    "keep_seasons",
    "sonarr_tag",
    "ignore_tag",
    "watch_interval_minutes",
    "search_cooldown_hours",
    "monitor_new_items_when_active",
    "monitor_new_items_when_idle",
    "app_token",
    "require_delete_approval",
    "live_series_ids",
    "new_season_grace_days",
    "plex_watchlist_rss_url",
    "plex_friends_watchlist_rss_url",
    "watchlist_interval_minutes",
    "tv_intake_enabled",
    "movie_intake_enabled",
    "sonarr_root_folder",
    "sonarr_quality_profile_id",
    "radarr_url",
    "radarr_api_key",
    "radarr_root_folder",
    "radarr_quality_profile_id",
    "radarr_tag",
)
_CONNECTION_ENV = (
    ("SONARR_URL", "sonarr_url"),
    ("SONARR_API_KEY", "sonarr_api_key"),
    ("PLEX_URL", "plex_url"),
    ("PLEX_TOKEN", "plex_token"),
)
_MONITOR_CHOICES = {"all", "new", "none", "keep"}


class RuntimeSettings(BaseSettings):
    """Process environment for the container. Sonarr and Plex are not read here."""

    model_config = SettingsConfigDict(extra="ignore")

    port: int = Field(default=8787, ge=1, le=65535)
    data_dir: Path = Path("/data")
    log_level: str = "INFO"
    http_timeout_seconds: float = Field(default=60, gt=0, le=300)
    library_timeout_seconds: float = Field(default=180, gt=0, le=600)
    history_page_size: int = Field(default=200, ge=20, le=1000)
    history_max_items: int = Field(default=20000, ge=100, le=200000)
    hydrate_limit: int = Field(default=200, ge=0, le=5000)
    run_on_startup: bool = True
    startup_delay_seconds: int = Field(default=10, ge=0, le=3600)

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"must be one of {', '.join(sorted(allowed))}")
        return upper


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sonarr_url: AnyHttpUrl | None = None
    sonarr_api_key: str = ""
    plex_url: AnyHttpUrl | None = None
    plex_token: str = ""

    dry_run: bool = True
    watch_window_days: int = Field(default=90, ge=1, le=3650)
    keep_seasons: str = "1"
    sonarr_tag: str = Field(default="season-keeper", min_length=1)
    ignore_tag: str = Field(default="keep-all", min_length=1)
    watch_interval_minutes: int = Field(default=15, ge=5, le=1440)
    search_cooldown_hours: int = Field(default=6, ge=1, le=168)
    monitor_new_items_when_active: str = "all"
    monitor_new_items_when_idle: str = "none"
    require_delete_approval: bool = True
    live_series_ids: list[int] = Field(default_factory=list)
    new_season_grace_days: int | None = None
    plex_watchlist_rss_url: str = ""
    plex_friends_watchlist_rss_url: str = ""
    watchlist_interval_minutes: int = Field(default=5, ge=1, le=1440)
    tv_intake_enabled: bool = True
    movie_intake_enabled: bool = True
    sonarr_root_folder: str = ""
    sonarr_quality_profile_id: int | None = None
    radarr_url: AnyHttpUrl | None = None
    radarr_api_key: str = ""
    radarr_root_folder: str = ""
    radarr_quality_profile_id: int | None = None
    radarr_tag: str = Field(default="season-keeper", min_length=1)

    port: int = Field(default=8787, ge=1, le=65535)
    data_dir: Path = Path("/data")
    log_level: str = "INFO"
    app_token: str = ""
    http_timeout_seconds: float = Field(default=60, gt=0, le=300)
    library_timeout_seconds: float = Field(default=180, gt=0, le=600)
    history_page_size: int = Field(default=200, ge=20, le=1000)
    history_max_items: int = Field(default=20000, ge=100, le=200000)
    hydrate_limit: int = Field(default=200, ge=0, le=5000)
    run_on_startup: bool = True
    startup_delay_seconds: int = Field(default=10, ge=0, le=3600)

    @field_validator("sonarr_quality_profile_id", "radarr_quality_profile_id", mode="before")
    @classmethod
    def _profile_id(cls, value: object) -> object:
        if value is None or value == "":
            return None
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("Choose a quality profile") from exc
        if number <= 0:
            raise ValueError("Choose a quality profile")
        return number

    @field_validator("new_season_grace_days", mode="before")
    @classmethod
    def _grace_days(cls, value: object) -> object:
        if value is None or value == "":
            return None
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("New-season grace must be a number of days") from exc
        if number < 0 or number > 3650:
            raise ValueError("New-season grace must be between 0 and 3650 days")
        return number

    @field_validator("live_series_ids", mode="before")
    @classmethod
    def _live_series_ids(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("Live shows must be a list of Sonarr series ids")
        numbers: list[int] = []
        for item in value:
            try:
                number = int(item)
            except (TypeError, ValueError) as exc:
                raise ValueError("Live show ids must be integers") from exc
            if number <= 0:
                raise ValueError("Live show ids must be positive")
            if number not in numbers:
                numbers.append(number)
        return numbers

    @field_validator("monitor_new_items_when_active", "monitor_new_items_when_idle")
    @classmethod
    def _monitor_choice(cls, value: str) -> str:
        if value not in _MONITOR_CHOICES:
            raise ValueError(f"must be one of {', '.join(sorted(_MONITOR_CHOICES))}")
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"must be one of {', '.join(sorted(allowed))}")
        return upper

    @model_validator(mode="after")
    def _check_keep_seasons(self) -> Settings:
        self.keep_season_numbers
        return self

    @property
    def configured(self) -> bool:
        return bool(
            self.sonarr_url
            and self.sonarr_api_key.strip()
            and self.plex_url
            and self.plex_token.strip()
        )

    @property
    def keep_season_numbers(self) -> frozenset[int]:
        parts = [part.strip() for part in self.keep_seasons.split(",") if part.strip()]
        if not parts:
            raise ValueError("Enter at least one season number")
        try:
            numbers = frozenset(int(part) for part in parts)
        except ValueError as exc:
            raise ValueError("Season numbers must be integers separated by commas") from exc
        if any(number < 0 for number in numbers):
            raise ValueError("Season numbers cannot be negative")
        # Season 1 is always kept. Season 0 is specials and is never kept.
        return (numbers - frozenset({0})) | frozenset({1})

    @property
    def sonarr_base(self) -> str:
        if self.sonarr_url is None:
            raise ValueError("Sonarr URL is not set")
        return str(self.sonarr_url).rstrip("/")

    @property
    def plex_base(self) -> str:
        if self.plex_url is None:
            raise ValueError("Plex URL is not set")
        return str(self.plex_url).rstrip("/")

    @property
    def radarr_configured(self) -> bool:
        return bool(self.radarr_url and self.radarr_api_key.strip())

    @property
    def radarr_base(self) -> str:
        if self.radarr_url is None:
            raise ValueError("Radarr URL is not set")
        return str(self.radarr_url).rstrip("/")

    @property
    def watchlist_rss_urls(self) -> tuple[tuple[str, str], ...]:
        feeds: list[tuple[str, str]] = []
        if self.plex_watchlist_rss_url.strip():
            feeds.append(("My Watchlist", self.plex_watchlist_rss_url.strip()))
        if self.plex_friends_watchlist_rss_url.strip():
            feeds.append(("Friends' Watchlist", self.plex_friends_watchlist_rss_url.strip()))
        return tuple(feeds)

    def policy(self) -> PolicySettings:
        return PolicySettings(
            watch_window=timedelta(days=self.watch_window_days),
            keep_seasons=self.keep_season_numbers,
            dry_run=self.dry_run,
            search_cooldown=timedelta(hours=self.search_cooldown_hours),
            monitor_new_when_active=self.monitor_new_items_when_active,
            monitor_new_when_idle=self.monitor_new_items_when_idle,
            require_delete_approval=self.require_delete_approval,
            live_series_ids=frozenset(self.live_series_ids),
            new_season_grace=timedelta(
                days=self.watch_window_days
                if self.new_season_grace_days is None
                else self.new_season_grace_days
            ),
        )


class SettingsWrite(BaseModel):
    """Fields posted by the admin page. Blank secrets keep the saved value."""

    model_config = ConfigDict(extra="ignore")

    sonarr_url: str
    plex_url: str
    sonarr_api_key: str = ""
    plex_token: str = ""
    dry_run: bool | None = None
    watch_window_days: int | None = Field(default=None, ge=1, le=3650)
    keep_seasons: str | None = None
    sonarr_tag: str | None = None
    ignore_tag: str | None = None
    watch_interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    search_cooldown_hours: int | None = Field(default=None, ge=1, le=168)
    monitor_new_items_when_active: str | None = None
    monitor_new_items_when_idle: str | None = None
    require_delete_approval: bool | None = None
    new_season_grace_days: int | None = None
    plex_watchlist_rss_url: str | None = None
    plex_friends_watchlist_rss_url: str | None = None
    watchlist_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    tv_intake_enabled: bool | None = None
    movie_intake_enabled: bool | None = None
    sonarr_root_folder: str | None = None
    sonarr_quality_profile_id: int | None = None
    radarr_url: str | None = None
    radarr_api_key: str = ""
    radarr_root_folder: str | None = None
    radarr_quality_profile_id: int | None = None
    radarr_tag: str | None = None
    app_token: str = ""
    clear_app_token: bool = False

    @field_validator(
        "sonarr_url",
        "plex_url",
        "sonarr_api_key",
        "plex_token",
        "keep_seasons",
        "sonarr_tag",
        "ignore_tag",
        "app_token",
        "monitor_new_items_when_active",
        "monitor_new_items_when_idle",
        "plex_watchlist_rss_url",
        "plex_friends_watchlist_rss_url",
        "sonarr_root_folder",
        "radarr_url",
        "radarr_api_key",
        "radarr_root_folder",
        "radarr_tag",
        mode="before",
    )
    @classmethod
    def _strip(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


def settings_path(data_dir: Path) -> Path:
    return data_dir / STORE_NAME


def mask_secret(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if len(text) < 12:
        return "Saved"
    return "Ends with " + text[-4:]


def clean_base_url(value: str, label: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"Enter an http or https {label} URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(
            f"The {label} URL should be the server address only. Put the key in its own field."
        )
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{netloc}{path}"


def clean_rss_url(value: str, label: str) -> str:
    """Allow the token query Plex puts on a watchlist feed. Reject embedded passwords."""
    text = value.strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"Enter an http or https {label} URL")
    if parts.username or parts.password:
        raise ValueError(f"The {label} URL cannot include a username or password")
    return text


def _picked(update: SettingsWrite, name: str, current: object) -> object:
    if name not in update.model_fields_set:
        return current
    return getattr(update, name)


def apply_update(current: Settings, update: SettingsWrite) -> Settings:
    api_key = update.sonarr_api_key.strip() or current.sonarr_api_key.strip()
    plex_token = update.plex_token.strip() or current.plex_token.strip()
    if not update.sonarr_url or not update.plex_url:
        raise ValueError("Enter the Sonarr URL and the Plex URL")
    if not api_key or not plex_token:
        raise ValueError("Enter the Sonarr API key and the Plex token")
    if update.clear_app_token:
        app_token = ""
    elif update.app_token.strip():
        app_token = update.app_token.strip()
    else:
        app_token = current.app_token
    tag = current.sonarr_tag if update.sonarr_tag is None else update.sonarr_tag.strip()
    if not tag:
        raise ValueError("Enter a Sonarr tag")
    ignore = current.ignore_tag if update.ignore_tag is None else update.ignore_tag.strip()
    if not ignore:
        raise ValueError("Enter an ignore tag")
    keep = current.keep_seasons if update.keep_seasons is None else update.keep_seasons.strip()
    own_rss = clean_rss_url(
        str(_picked(update, "plex_watchlist_rss_url", current.plex_watchlist_rss_url) or ""),
        "My Watchlist RSS",
    )
    friends_rss = clean_rss_url(
        str(_picked(update, "plex_friends_watchlist_rss_url", current.plex_friends_watchlist_rss_url) or ""),
        "Friends' Watchlist RSS",
    )
    radarr_raw = _picked(update, "radarr_url", None if current.radarr_url is None else str(current.radarr_url))
    radarr_text = "" if radarr_raw is None else str(radarr_raw).strip()
    radarr_url = clean_base_url(radarr_text, "Radarr") if radarr_text else None
    radarr_key = update.radarr_api_key.strip() or current.radarr_api_key.strip()
    radarr_tag = _picked(update, "radarr_tag", current.radarr_tag)
    radarr_tag = current.radarr_tag if radarr_tag is None else str(radarr_tag).strip()
    if not radarr_tag:
        raise ValueError("Enter a Radarr tag")
    sonarr_root = str(_picked(update, "sonarr_root_folder", current.sonarr_root_folder) or "").strip()
    radarr_root = str(_picked(update, "radarr_root_folder", current.radarr_root_folder) or "").strip()
    data = current.model_dump()
    data.update(
        sonarr_url=clean_base_url(update.sonarr_url, "Sonarr"),
        sonarr_api_key=api_key,
        plex_url=clean_base_url(update.plex_url, "Plex"),
        plex_token=plex_token,
        dry_run=current.dry_run if update.dry_run is None else update.dry_run,
        watch_window_days=current.watch_window_days
        if update.watch_window_days is None
        else update.watch_window_days,
        keep_seasons=keep,
        sonarr_tag=tag,
        ignore_tag=ignore,
        watch_interval_minutes=current.watch_interval_minutes
        if update.watch_interval_minutes is None
        else update.watch_interval_minutes,
        search_cooldown_hours=current.search_cooldown_hours
        if update.search_cooldown_hours is None
        else update.search_cooldown_hours,
        monitor_new_items_when_active=current.monitor_new_items_when_active
        if update.monitor_new_items_when_active is None
        else update.monitor_new_items_when_active,
        monitor_new_items_when_idle=current.monitor_new_items_when_idle
        if update.monitor_new_items_when_idle is None
        else update.monitor_new_items_when_idle,
        require_delete_approval=current.require_delete_approval
        if update.require_delete_approval is None
        else update.require_delete_approval,
        new_season_grace_days=current.new_season_grace_days
        if "new_season_grace_days" not in update.model_fields_set
        else update.new_season_grace_days,
        plex_watchlist_rss_url=own_rss,
        plex_friends_watchlist_rss_url=friends_rss,
        watchlist_interval_minutes=current.watchlist_interval_minutes
        if update.watchlist_interval_minutes is None
        else update.watchlist_interval_minutes,
        tv_intake_enabled=current.tv_intake_enabled
        if update.tv_intake_enabled is None
        else update.tv_intake_enabled,
        movie_intake_enabled=current.movie_intake_enabled
        if update.movie_intake_enabled is None
        else update.movie_intake_enabled,
        sonarr_root_folder=sonarr_root,
        sonarr_quality_profile_id=_picked(
            update, "sonarr_quality_profile_id", current.sonarr_quality_profile_id
        ),
        radarr_url=radarr_url,
        radarr_api_key=radarr_key,
        radarr_root_folder=radarr_root,
        radarr_quality_profile_id=_picked(
            update, "radarr_quality_profile_id", current.radarr_quality_profile_id
        ),
        radarr_tag=radarr_tag,
        app_token=app_token,
    )
    return Settings.model_validate(data)


def dump_stored(settings: Settings) -> dict:
    raw = settings.model_dump(mode="json")
    payload: dict = {}
    for key in STORED_FIELDS:
        value = raw.get(key)
        if key in {"sonarr_url", "plex_url", "radarr_url"}:
            payload[key] = "" if not value else str(value).rstrip("/")
        else:
            payload[key] = value
    return payload


def save_settings(settings: Settings) -> None:
    path = settings_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(dump_stored(settings), indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def public_settings(settings: Settings) -> dict:
    return {
        "configured": settings.configured,
        "sonarr_url": "" if settings.sonarr_url is None else str(settings.sonarr_url).rstrip("/"),
        "sonarr_api_key_set": bool(settings.sonarr_api_key.strip()),
        "sonarr_api_key_hint": mask_secret(settings.sonarr_api_key),
        "plex_url": "" if settings.plex_url is None else str(settings.plex_url).rstrip("/"),
        "plex_token_set": bool(settings.plex_token.strip()),
        "plex_token_hint": mask_secret(settings.plex_token),
        "dry_run": settings.dry_run,
        "watch_window_days": settings.watch_window_days,
        "keep_seasons": settings.keep_seasons,
        "keep_seasons_effective": sorted(settings.keep_season_numbers),
        "sonarr_tag": settings.sonarr_tag,
        "ignore_tag": settings.ignore_tag,
        "watch_interval_minutes": settings.watch_interval_minutes,
        "search_cooldown_hours": settings.search_cooldown_hours,
        "monitor_new_items_when_active": settings.monitor_new_items_when_active,
        "monitor_new_items_when_idle": settings.monitor_new_items_when_idle,
        "require_delete_approval": settings.require_delete_approval,
        "live_series_ids": list(settings.live_series_ids),
        "new_season_grace_days": settings.new_season_grace_days,
        "new_season_grace_days_effective": settings.policy().new_season_grace.days,
        "plex_watchlist_rss_url": settings.plex_watchlist_rss_url,
        "plex_friends_watchlist_rss_url": settings.plex_friends_watchlist_rss_url,
        "watchlist_interval_minutes": settings.watchlist_interval_minutes,
        "tv_intake_enabled": settings.tv_intake_enabled,
        "movie_intake_enabled": settings.movie_intake_enabled,
        "sonarr_root_folder": settings.sonarr_root_folder,
        "sonarr_quality_profile_id": settings.sonarr_quality_profile_id,
        "radarr_url": "" if settings.radarr_url is None else str(settings.radarr_url).rstrip("/"),
        "radarr_configured": settings.radarr_configured,
        "radarr_api_key_set": bool(settings.radarr_api_key.strip()),
        "radarr_api_key_hint": mask_secret(settings.radarr_api_key),
        "radarr_root_folder": settings.radarr_root_folder,
        "radarr_quality_profile_id": settings.radarr_quality_profile_id,
        "radarr_tag": settings.radarr_tag,
        "app_token_set": bool(settings.app_token),
    }


def _runtime_settings() -> Settings:
    runtime = RuntimeSettings()
    return Settings(
        port=runtime.port,
        data_dir=runtime.data_dir,
        log_level=runtime.log_level,
        http_timeout_seconds=runtime.http_timeout_seconds,
        library_timeout_seconds=runtime.library_timeout_seconds,
        history_page_size=runtime.history_page_size,
        history_max_items=runtime.history_max_items,
        hydrate_limit=runtime.hydrate_limit,
        run_on_startup=runtime.run_on_startup,
        startup_delay_seconds=runtime.startup_delay_seconds,
    )


def _from_stored(base: Settings, stored: dict) -> Settings:
    data = base.model_dump()
    for key in STORED_FIELDS:
        if key not in stored or stored[key] is None:
            continue
        if key in {"sonarr_url", "plex_url", "radarr_url"} and str(stored[key]).strip() == "":
            continue
        data[key] = stored[key]
    return Settings.model_validate(data)


def _take_int(payload: dict, env_name: str, field: str) -> None:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return
    try:
        payload[field] = int(raw)
    except ValueError:
        logger.warning("Ignored %s because it is not an integer", env_name)


def _bootstrap_from_env() -> dict | None:
    values = {field: os.environ.get(env_name, "").strip() for env_name, field in _CONNECTION_ENV}
    if not all(values.values()):
        return None
    try:
        payload: dict = {
            "sonarr_url": clean_base_url(values["sonarr_url"], "Sonarr"),
            "sonarr_api_key": values["sonarr_api_key"],
            "plex_url": clean_base_url(values["plex_url"], "Plex"),
            "plex_token": values["plex_token"],
        }
    except ValueError:
        logger.exception("Environment URLs could not be imported. Open /admin and enter them there.")
        return None
    dry = os.environ.get("DRY_RUN", "").strip().lower()
    if dry:
        payload["dry_run"] = dry in {"1", "true", "yes", "on"}
    approval = os.environ.get("REQUIRE_DELETE_APPROVAL", "").strip().lower()
    if approval:
        payload["require_delete_approval"] = approval in {"1", "true", "yes", "on"}
    _take_int(payload, "WATCH_WINDOW_DAYS", "watch_window_days")
    _take_int(payload, "NEW_SEASON_GRACE_DAYS", "new_season_grace_days")
    _take_int(payload, "WATCH_INTERVAL_MINUTES", "watch_interval_minutes")
    _take_int(payload, "SEARCH_COOLDOWN_HOURS", "search_cooldown_hours")
    for env_name, field in (
        ("KEEP_SEASONS", "keep_seasons"),
        ("SONARR_TAG", "sonarr_tag"),
        ("IGNORE_TAG", "ignore_tag"),
        ("MONITOR_NEW_ITEMS_WHEN_ACTIVE", "monitor_new_items_when_active"),
        ("MONITOR_NEW_ITEMS_WHEN_IDLE", "monitor_new_items_when_idle"),
        ("APP_TOKEN", "app_token"),
        ("LIVE_SERIES_IDS", "live_series_ids"),
    ):
        if env_name in os.environ and os.environ[env_name].strip():
            payload[field] = os.environ[env_name].strip()
    return payload


def load_settings() -> Settings:
    settings = _runtime_settings()
    path = settings_path(settings.data_dir)
    imported = False
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read %s. Open /admin and save the settings again.", path)
            return settings
        if not isinstance(raw, dict):
            logger.error("%s must contain an object. Open /admin and save the settings again.", path.name)
            return settings
        stored: dict | None = raw
    else:
        stored = _bootstrap_from_env()
        imported = stored is not None
        if stored is None:
            return settings
    try:
        loaded = _from_stored(settings, stored)
    except ValidationError:
        logger.exception("Saved settings were rejected. Open /admin and save them again.")
        return settings
    if imported:
        try:
            save_settings(loaded)
        except OSError:
            logger.exception("Could not write imported settings to %s", path)
            return loaded
        logger.info("Imported Sonarr and Plex settings into %s. Change them on the admin page.", path)
    return loaded
