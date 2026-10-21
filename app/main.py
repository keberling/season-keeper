"""Status page, admin page, and scheduler."""

from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ValidationError, field_validator

from app.config import (
    RuntimeSettings,
    Settings,
    SettingsWrite,
    apply_update,
    clean_base_url,
    load_settings,
    public_settings,
    save_settings,
)
from app.intake import run_watchlist_intake
from app.radarr import RadarrClient, RadarrError
from app.human import dismissed_line
from app.plex import PlexClient
from app.progress import Progress
from app.service import (
    SeriesNotFound,
    delete_later_seasons,
    download_later_seasons,
    preview_series,
    run_cycle,
    set_keep_all,
)
from app.sonarr import SonarrClient, SonarrError
from app.state import State

logger = logging.getLogger("trimarr")
PAGE = Path(__file__).with_name("status_page.html").read_text(encoding="utf-8")
ADMIN_PAGE = Path(__file__).with_name("admin_page.html").read_text(encoding="utf-8")
STATS_PAGE = Path(__file__).with_name("stats_page.html").read_text(encoding="utf-8")
THEME = Path(__file__).with_name("theme.css").read_text(encoding="utf-8")
NO_STORE = {"Cache-Control": "no-store"}
_SETUP_REQUIRED = "Save Sonarr and Plex on the admin page first"
_DB_NAME = "trimarr.sqlite"
_LEGACY_DB_NAME = "season-keeper.sqlite"


def database_path(data_dir: Path) -> Path:
    """Open an existing database in place. New installs create trimarr.sqlite.

    A data folder that already has season-keeper.sqlite keeps using that file,
    including its WAL files. settings.json is not renamed.
    """
    current = data_dir / _DB_NAME
    legacy = data_dir / _LEGACY_DB_NAME
    if current.exists():
        return current
    if legacy.exists():
        return legacy
    return current


class Holder:
    def __init__(self) -> None:
        self.settings: Settings | None = None
        self.state: State | None = None
        self.lock = threading.Lock()
        self.scheduler: BackgroundScheduler | None = None
        self.running = False
        self.progress = Progress()


holder = Holder()


class ConfirmBody(BaseModel):
    confirm: bool = False


class IgnoreBody(BaseModel):
    ignore: bool = True


class RadarrTestBody(BaseModel):
    radarr_url: str = ""
    radarr_api_key: str = ""

    @field_validator("radarr_url", "radarr_api_key", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class LiveScopeBody(BaseModel):
    series_ids: list[int] = []

    @field_validator("series_ids")
    @classmethod
    def _series_ids(cls, value: list[int]) -> list[int]:
        cleaned: list[int] = []
        seen: set[int] = set()
        for item in value:
            if item <= 0:
                raise ValueError("Live show ids must be positive")
            if item not in seen:
                seen.add(item)
                cleaned.append(item)
        return cleaned


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _clients(settings: Settings) -> tuple[SonarrClient, PlexClient]:
    sonarr = SonarrClient(settings.sonarr_base, settings.sonarr_api_key, settings.http_timeout_seconds)
    plex = PlexClient(
        settings.plex_base,
        settings.plex_token,
        settings.http_timeout_seconds,
        settings.library_timeout_seconds,
        settings.history_page_size,
        settings.history_max_items,
        settings.hydrate_limit,
    )
    return sonarr, plex


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error.get("loc", ()) if item != "body")
        message = error.get("msg") or "Invalid settings"
        parts.append(f"{loc}: {message}" if loc else message)
    return "; ".join(parts) or "Invalid settings"


def _schedule_interval(
    scheduler: BackgroundScheduler,
    *,
    job_id: str,
    func,
    minutes: int,
    first: bool,
    startup_delay: int,
) -> None:
    if scheduler.get_job(job_id):
        scheduler.reschedule_job(job_id, trigger="interval", minutes=minutes)
        return
    delay = startup_delay if first else minutes * 60
    first_run = datetime.now(timezone.utc) + timedelta(seconds=delay)
    scheduler.add_job(
        func,
        "interval",
        minutes=minutes,
        next_run_time=first_run,
        id=job_id,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )


def _arm_schedule(settings: Settings, *, first: bool) -> None:
    scheduler = holder.scheduler
    if scheduler is None or not settings.configured:
        return
    cycle_delay = (
        settings.startup_delay_seconds
        if settings.run_on_startup
        else settings.watch_interval_minutes * 60
    )
    _schedule_interval(
        scheduler,
        job_id="cycle",
        func=cycle,
        minutes=settings.watch_interval_minutes,
        first=first,
        startup_delay=cycle_delay,
    )
    # The library cycle also reads the watchlist. This shorter job covers the gap.
    _schedule_interval(
        scheduler,
        job_id="watchlist",
        func=watchlist_poll,
        minutes=settings.watchlist_interval_minutes,
        first=first,
        startup_delay=settings.startup_delay_seconds + 60,
    )


def cycle() -> bool:
    settings = holder.settings
    state = holder.state
    if settings is None or state is None:
        return False
    if not settings.configured:
        logger.info("Skipped this cycle because Sonarr and Plex are not saved on the admin page.")
        return False
    if not holder.lock.acquire(blocking=False):
        logger.info("Skipped this cycle because another run is still going.")
        return False
    holder.running = True
    holder.progress.update("Checking Sonarr and Plex.")
    sonarr = None
    plex = None
    try:
        sonarr, plex = _clients(settings)
        holder.progress.update("Reading Plex watchlists.")
        try:
            run_watchlist_intake(settings, state, sonarr=sonarr)
        except Exception:
            logger.exception("The watchlist check failed")
        run_cycle(settings, state, sonarr, plex, progress=holder.progress)
    except Exception:
        logger.exception("The cycle crashed")
    finally:
        if sonarr is not None:
            sonarr.close()
        if plex is not None:
            plex.close()
        holder.progress.clear()
        holder.running = False
        holder.lock.release()
    return True


def watchlist_poll() -> bool:
    settings = holder.settings
    state = holder.state
    if settings is None or state is None or not settings.configured:
        return False
    if not holder.lock.acquire(blocking=False):
        logger.info("Skipped the watchlist check because another run is still going.")
        return False
    try:
        run_watchlist_intake(settings, state)
    except Exception:
        logger.exception("The watchlist check failed")
    finally:
        holder.lock.release()
    return True


def _require_token(request: Request) -> None:
    settings = holder.settings
    if settings is None or not settings.app_token:
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not secrets.compare_digest(supplied, settings.app_token):
        raise HTTPException(status_code=401, detail="Missing or wrong token")


def _require_ready() -> Settings:
    if holder.settings is None or holder.state is None:
        raise HTTPException(status_code=503, detail="Trimarr is still starting")
    return holder.settings


def _require_configured(settings: Settings) -> None:
    if not settings.configured:
        raise HTTPException(status_code=400, detail=_SETUP_REQUIRED)


def _accept_write(current: Settings, body: SettingsWrite) -> Settings:
    try:
        return apply_update(current, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_validation_message(exc)) from exc


def _safe_detail(exc: Exception, settings: Settings, extra: tuple[str, ...] = ()) -> str:
    text = str(exc)
    for secret in (
        settings.sonarr_api_key,
        settings.plex_token,
        settings.app_token,
        settings.radarr_api_key,
        settings.plex_watchlist_rss_url,
        settings.plex_friends_watchlist_rss_url,
        *extra,
    ):
        if secret:
            text = text.replace(secret, "******")
    return text[:500]


def _library_options(client: SonarrClient | RadarrClient) -> dict:
    folders = []
    for row in client.root_folders():
        path = str(row.get("path") or "").strip()
        if path:
            folders.append({"path": path, "free_space": row.get("freeSpace")})
    profiles = []
    for row in client.quality_profiles():
        if row.get("id") is None:
            continue
        profiles.append({"id": int(row["id"]), "name": str(row.get("name") or row["id"])})
    return {"root_folders": folders, "quality_profiles": profiles}


def probe_connections(settings: Settings) -> dict:
    sonarr_result = {"ok": False, "detail": ""}
    plex_result = {"ok": False, "detail": ""}
    sonarr_options: dict = {"root_folders": [], "quality_profiles": []}
    sonarr = None
    plex = None
    try:
        sonarr = SonarrClient(settings.sonarr_base, settings.sonarr_api_key, 15)
        sonarr_result = {"ok": True, "detail": "Sonarr " + sonarr.status_version()}
        try:
            sonarr_options = _library_options(sonarr)
        except Exception:
            sonarr_options = {"root_folders": [], "quality_profiles": []}
    except Exception as exc:
        sonarr_result = {"ok": False, "detail": _safe_detail(exc, settings)}
    finally:
        if sonarr is not None:
            sonarr.close()
    try:
        plex = PlexClient(settings.plex_base, settings.plex_token, 15, 15, 50, 50, 0)
        plex_result = {"ok": True, "detail": "Plex " + plex.server_name()}
    except Exception as exc:
        plex_result = {"ok": False, "detail": _safe_detail(exc, settings)}
    finally:
        if plex is not None:
            plex.close()
    return {"sonarr": sonarr_result, "plex": plex_result, "sonarr_options": sonarr_options}


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = RuntimeSettings()
    configure_logging(runtime.log_level)
    settings = load_settings()
    holder.settings = settings
    holder.state = State(database_path(settings.data_dir))
    if settings.configured:
        mode = "preview only" if settings.dry_run else "live"
        logger.info(
            "Trimarr is %s. Ignore tag=%s window=%s days interval=%s minutes",
            mode,
            settings.ignore_tag,
            settings.watch_window_days,
            settings.watch_interval_minutes,
        )
    else:
        logger.info("No Sonarr or Plex settings saved yet. Open /admin to enter them. The schedule is off.")
    scheduler = BackgroundScheduler(timezone="UTC")
    holder.scheduler = scheduler
    if settings.configured:
        _arm_schedule(settings, first=True)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        holder.scheduler = None


app = FastAPI(title="Trimarr", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(PAGE, headers=NO_STORE)


@app.get("/admin", response_class=HTMLResponse)
def admin() -> HTMLResponse:
    return HTMLResponse(ADMIN_PAGE, headers=NO_STORE)


@app.get("/stats", response_class=HTMLResponse)
def stats_page() -> HTMLResponse:
    return HTMLResponse(STATS_PAGE, headers=NO_STORE)


@app.get("/theme.css")
def theme_css() -> Response:
    return Response(THEME, media_type="text/css", headers=NO_STORE)


@app.get("/api/savings")
def savings() -> JSONResponse:
    settings = _require_ready()
    assert holder.state is not None
    report = holder.state.load_report()
    series = (report or {}).get("series") or []
    possible = sum(
        int(row.get("later_bytes") or 0)
        for row in series
        if row.get("action") != "ignore"
    )
    ready = sum(
        int(row.get("bytes_to_free") or 0)
        for row in series
        if row.get("action") == "prune" and not row.get("applied")
    )
    events = holder.state.freed_events()
    pending_bytes = sum(
        int(row.get("bytes_to_free") or 0)
        for row in series
        if row.get("pending_approval")
    )
    protected = sum(len(row.get("protected_seasons") or []) for row in series)
    totals = holder.state.activity_totals()
    top = sorted(
        (
            {
                "title": row.get("title") or "Show",
                "sonarr_id": row.get("sonarr_id"),
                "bytes": int(row.get("bytes_to_free") or row.get("later_bytes") or 0),
                "action": row.get("action"),
                "pending_approval": bool(row.get("pending_approval")),
            }
            for row in series
            if row.get("action") != "ignore"
            and int(row.get("bytes_to_free") or row.get("later_bytes") or 0) > 0
        ),
        key=lambda row: int(row["bytes"]),
        reverse=True,
    )[:8]
    recent_since = datetime.now(timezone.utc) - timedelta(days=7)
    return JSONResponse(
        {
            "possible_bytes": possible,
            "ready_bytes": ready,
            "freed_bytes": holder.state.freed_total_bytes(),
            "recent_freed_bytes": holder.state.freed_bytes_since(recent_since),
            "pending_bytes": pending_bytes,
            "protected_seasons": protected,
            "restored_seasons": totals["restored_seasons"],
            "searches": totals["searches"],
            "watchlist_adds": totals["watchlist_adds"],
            "series_count": len(series),
            "scanned_at": None if report is None else report.get("finished_at"),
            "events": events,
            "cycles": holder.state.list_cycles(),
            "top_shows": top,
            "configured": settings.configured,
        },
        headers=NO_STORE,
    )


@app.get("/api/activity")
def activity(kind: str | None = None, q: str | None = None) -> JSONResponse:
    _require_ready()
    assert holder.state is not None
    allowed = {"deleted", "restored", "approval", "approved", "dismissed", "watchlist"}
    selected = kind if kind in allowed else None
    return JSONResponse(
        {"events": holder.state.list_activity(kind=selected, query=q)},
        headers=NO_STORE,
    )


def _status_body() -> dict:
    settings = _require_ready()
    assert holder.state is not None
    next_run = None
    if holder.scheduler is not None and settings.configured:
        job = holder.scheduler.get_job("cycle")
        if job is not None and job.next_run_time is not None:
            next_run = job.next_run_time.isoformat()
    return {
        "configured": settings.configured,
        "dry_run": settings.dry_run,
        "watch_window_days": settings.watch_window_days,
        "watch_interval_minutes": settings.watch_interval_minutes,
        "keep_seasons": sorted(settings.keep_season_numbers),
        "tag": settings.ignore_tag,
        "ignore_tag": settings.ignore_tag,
        "require_delete_approval": settings.require_delete_approval,
        "live_series_ids": list(settings.live_series_ids),
        "running": holder.running,
        "progress": holder.progress.snapshot() if holder.running else None,
        "next_run_at": next_run,
        "report": holder.state.load_report(),
    }


@app.get("/api/status")
def status() -> JSONResponse:
    return JSONResponse(_status_body(), headers=NO_STORE)


@app.get("/api/settings")
def read_settings(request: Request) -> JSONResponse:
    settings = _require_ready()
    _require_token(request)
    return JSONResponse(public_settings(settings), headers=NO_STORE)


@app.post("/api/settings")
def write_settings(body: SettingsWrite, request: Request) -> JSONResponse:
    current = _require_ready()
    _require_token(request)
    updated = _accept_write(current, body)
    was_configured = current.configured
    try:
        save_settings(updated)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not save settings") from exc
    holder.settings = updated
    _arm_schedule(updated, first=not was_configured)
    mode = "preview" if updated.dry_run else "live"
    approval = "Deletes wait for approval." if updated.require_delete_approval else "Deletes run on their own."
    scope = (
        "Live scope is every show."
        if not updated.live_series_ids
        else f"Live scope is {len(updated.live_series_ids)} shows."
    )
    logger.info(
        "Saved settings. Mode is %s. %s %s Ignore tag is %s. Window is %s days, every %s minutes.",
        mode,
        approval,
        scope,
        updated.ignore_tag,
        updated.watch_window_days,
        updated.watch_interval_minutes,
    )
    return JSONResponse(public_settings(updated), headers=NO_STORE)


@app.get("/api/sonarr/options")
def sonarr_options(request: Request) -> dict:
    settings = _require_ready()
    _require_token(request)
    _require_configured(settings)
    sonarr = SonarrClient(settings.sonarr_base, settings.sonarr_api_key, 15)
    try:
        return _library_options(sonarr)
    except SonarrError as exc:
        raise HTTPException(status_code=502, detail=_safe_detail(exc, settings)) from exc
    finally:
        sonarr.close()


@app.get("/api/radarr/options")
def radarr_options(request: Request) -> dict:
    settings = _require_ready()
    _require_token(request)
    if not settings.radarr_configured:
        raise HTTPException(status_code=400, detail="Save the Radarr URL and API key first")
    radarr = RadarrClient(settings.radarr_base, settings.radarr_api_key, 15)
    try:
        return _library_options(radarr)
    except RadarrError as exc:
        raise HTTPException(status_code=502, detail=_safe_detail(exc, settings)) from exc
    finally:
        radarr.close()


@app.post("/api/radarr/test")
def test_radarr(body: RadarrTestBody, request: Request) -> dict:
    settings = _require_ready()
    _require_token(request)
    url_text = body.radarr_url or ("" if settings.radarr_url is None else str(settings.radarr_url))
    key = body.radarr_api_key or settings.radarr_api_key
    if not url_text.strip() or not key.strip():
        raise HTTPException(status_code=400, detail="Enter the Radarr URL and API key")
    try:
        url = clean_base_url(url_text, "Radarr")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    radarr = RadarrClient(url, key.strip(), 15)
    try:
        version = radarr.status_version()
        options = _library_options(radarr)
        return {"ok": True, "detail": "Radarr " + version, **options}
    except Exception as exc:
        return {
            "ok": False,
            "detail": _safe_detail(exc, settings, extra=(key.strip(),)),
            "root_folders": [],
            "quality_profiles": [],
        }
    finally:
        radarr.close()


@app.post("/api/settings/test")
def test_settings(body: SettingsWrite, request: Request) -> dict:
    current = _require_ready()
    _require_token(request)
    candidate = _accept_write(current, body)
    return probe_connections(candidate)


@app.post("/api/live-scope")
def write_live_scope(body: LiveScopeBody, request: Request) -> JSONResponse:
    current = _require_ready()
    _require_token(request)
    updated = current.model_copy(update={"live_series_ids": body.series_ids})
    try:
        save_settings(updated)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not save settings") from exc
    holder.settings = updated
    logger.info("Live scope is now %s", body.series_ids or "every show")
    return JSONResponse(public_settings(updated), headers=NO_STORE)


@app.post("/api/run")
def run_now(request: Request) -> JSONResponse:
    settings = _require_ready()
    _require_token(request)
    _require_configured(settings)
    if not cycle():
        raise HTTPException(status_code=409, detail="A run is already in progress")
    return JSONResponse(_status_body(), headers=NO_STORE)


@app.get("/api/series/{series_id}")
def series_preview(series_id: int, request: Request) -> dict:
    settings = _require_ready()
    _require_token(request)
    _require_configured(settings)
    assert holder.state is not None
    if not holder.lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A run is already in progress")
    holder.running = True
    holder.progress.update("Reading this show from Sonarr.")
    sonarr = None
    try:
        sonarr = SonarrClient(
            settings.sonarr_base, settings.sonarr_api_key, settings.http_timeout_seconds
        )
        return preview_series(settings, holder.state, sonarr, series_id)
    except SeriesNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SonarrError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if sonarr is not None:
            sonarr.close()
        holder.progress.clear()
        holder.running = False
        holder.lock.release()


def _manual(request: Request, series_id: int, body: ConfirmBody, action) -> dict:
    settings = _require_ready()
    _require_token(request)
    _require_configured(settings)
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Confirm the action in the page before it runs")
    assert holder.state is not None
    if not holder.lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A run is already in progress")
    holder.running = True
    holder.progress.update("Reading this show from Sonarr.")
    sonarr = None
    try:
        sonarr = SonarrClient(
            settings.sonarr_base, settings.sonarr_api_key, settings.http_timeout_seconds
        )
        result = action(settings, holder.state, sonarr, series_id)
        if result.error:
            raise HTTPException(status_code=409, detail=result.error)
        return result.to_dict()
    except SeriesNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SonarrError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if sonarr is not None:
            sonarr.close()
        holder.progress.clear()
        holder.running = False
        holder.lock.release()


@app.post("/api/series/{series_id}/delete-later")
def delete_one(series_id: int, body: ConfirmBody, request: Request) -> dict:
    return _manual(request, series_id, body, delete_later_seasons)


@app.post("/api/series/{series_id}/download-later")
def download_one(series_id: int, body: ConfirmBody, request: Request) -> dict:
    return _manual(request, series_id, body, download_later_seasons)


@app.post("/api/series/{series_id}/dismiss")
def dismiss_one(series_id: int, request: Request) -> dict:
    """Hide a pending delete. The next cycle still will not delete the files."""
    settings = _require_ready()
    _require_token(request)
    _require_configured(settings)
    assert holder.state is not None
    moment = datetime.now(timezone.utc)
    holder.state.dismiss(series_id, moment)
    report = holder.state.load_report() or {}
    row = None
    for item in report.get("series") or []:
        if int(item.get("sonarr_id") or 0) == series_id:
            row = dict(item)
            break
    title = str(row.get("title") or f"Show {series_id}") if row else f"Show {series_id}"
    message = dismissed_line(title)
    logger.info("%s", message)
    holder.state.record_activity(
        at=moment,
        kind="dismissed",
        sonarr_id=series_id,
        title=title,
        message=message,
    )
    if row is None:
        return {"sonarr_id": series_id, "dismissed": True, "pending_approval": False}
    row["dismissed"] = True
    row["pending_approval"] = False
    reason = str(row.get("reason") or "")
    if not reason.startswith("Dismissed."):
        row["reason"] = "Dismissed. Files stay until you approve a delete. " + reason
    holder.state.replace_series(row)
    return row


@app.post("/api/series/{series_id}/ignore")
def ignore_one(series_id: int, body: IgnoreBody, request: Request) -> dict:
    settings = _require_ready()
    _require_token(request)
    _require_configured(settings)
    assert holder.state is not None
    if not holder.lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A run is already in progress")
    holder.running = True
    holder.progress.update("Reading this show from Sonarr.")
    sonarr = None
    try:
        sonarr = SonarrClient(
            settings.sonarr_base, settings.sonarr_api_key, settings.http_timeout_seconds
        )
        return set_keep_all(settings, holder.state, sonarr, series_id, body.ignore)
    except SeriesNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SonarrError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if sonarr is not None:
            sonarr.close()
        holder.progress.clear()
        holder.running = False
        holder.lock.release()


def main() -> None:
    runtime = RuntimeSettings()
    configure_logging(runtime.log_level)
    uvicorn.run(app, host="0.0.0.0", port=runtime.port, log_level=runtime.log_level.lower())


if __name__ == "__main__":
    main()
