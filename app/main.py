"""Status page, admin page, and scheduler."""

from __future__ import annotations

import logging
import secrets
import threading

import httpx
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
from app.human import dismissed_line, queue_line
from app.intake import run_watchlist_intake
from app.jobs import start_worker
from app.models import SeriesResult
from app.queue_limit import (
    GIB,
    JOB_MAX_ATTEMPTS,
    JOB_TIMEOUT_SECONDS,
    PRIORITY_DELETE,
    PRIORITY_NORMAL,
    PRIORITY_SOON,
    RETRY_BACKOFF_SECONDS,
    TIB,
    delete_eta,
    queue_block,
    restore_line,
)
from app.radarr import RadarrClient, RadarrError
from app.plex import PlexClient
from app.progress import Progress
from app.service import (
    DeleteSkipped,
    SeriesNotFound,
    download_later_seasons,
    execute_queued_delete,
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
        self.mutation = threading.Lock()
        self.cycle_lock = threading.Lock()
        self.scheduler: BackgroundScheduler | None = None
        self.running = False
        self.progress = Progress()
        self.worker_stop = threading.Event()
        self.worker_wake = threading.Event()
        self.worker: threading.Thread | None = None


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


class PauseBody(BaseModel):
    paused: bool = True


class BulkBody(BaseModel):
    confirm: bool = False
    kind: str
    series_ids: list[int] = []

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        allowed = {"delete-later", "download-later", "dismiss"}
        if value not in allowed:
            raise ValueError("Choose deletion, download, or dismiss")
        return value

    @field_validator("series_ids")
    @classmethod
    def _series_ids(cls, value: list[int]) -> list[int]:
        cleaned: list[int] = []
        seen: set[int] = set()
        for item in value:
            if item <= 0:
                raise ValueError("Show ids must be positive")
            if item not in seen:
                seen.add(item)
                cleaned.append(item)
        return cleaned


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


def _wake() -> None:
    holder.worker_wake.set()


def _note(job: dict, phase: str, error: str | None = None) -> None:
    """Record queued, started, finished, or failed. Automatic watchlist checks stay out of the log."""
    state = holder.state
    if state is None or job.get("kind") == "watchlist":
        return
    message = queue_line(
        phase,
        str(job.get("label") or "Task"),
        str(job.get("title") or "Show"),
        error,
    )
    if phase in {"queued", "failed"}:
        logger.info("%s", message)
    else:
        logger.debug("%s", message)
    state.record_activity(
        at=datetime.now(timezone.utc),
        kind=phase,
        sonarr_id=job.get("sonarr_id"),
        title=str(job.get("title") or "Show"),
        message=message,
    )


def _enqueue(
    *,
    kind: str,
    label: str,
    title: str,
    dedupe_key: str,
    sonarr_id: int | None = None,
    payload: dict | None = None,
    priority: int = PRIORITY_NORMAL,
    nbytes: int = 0,
) -> dict:
    assert holder.state is not None
    job, created = holder.state.enqueue_job(
        created_at=datetime.now(timezone.utc),
        kind=kind,
        label=label,
        title=title,
        dedupe_key=dedupe_key,
        sonarr_id=sonarr_id,
        payload=payload,
        priority=priority,
        nbytes=nbytes,
    )
    job["deduped"] = not created
    if created:
        _note(job, "queued")
    _wake()
    return job


def _enqueue_run() -> dict:
    settings = holder.settings
    dry = True if settings is None else settings.dry_run
    label = "Refresh preview" if dry else "Run now"
    return _enqueue(
        kind="run",
        label=label,
        title="Library",
        dedupe_key="run",
        priority=PRIORITY_NORMAL,
    )


def _series_context(series_id: int) -> tuple[str, bool]:
    title = f"Show {series_id}"
    pending = False
    state = holder.state
    if state is None:
        return title, pending
    report = state.load_report() or {}
    for row in report.get("series") or []:
        if int(row.get("sonarr_id") or 0) == series_id:
            return str(row.get("title") or title), bool(row.get("pending_approval"))
    return title, pending


def _enqueue_series(kind: str, series_id: int, *, ignore: bool | None = None) -> dict:
    title, pending = _series_context(series_id)
    payload: dict = {}
    priority = PRIORITY_NORMAL
    nbytes = 0
    if kind == "delete-later":
        label = "Approve deletion" if pending else "Queue season deletion"
        key = f"delete-later:{series_id}"
        settings = holder.settings
        payload = {
            "source": "user",
            "override_protection": bool(settings and settings.manual_pick_overrides_protection),
        }
        priority = PRIORITY_DELETE
        if holder.state is not None:
            report = holder.state.load_report() or {}
            for row in report.get("series") or []:
                if int(row.get("sonarr_id") or 0) == series_id:
                    nbytes = int(row.get("bytes_to_free") or 0)
                    break
    elif kind == "download-later":
        label = "Download missing seasons"
        key = f"download-later:{series_id}"
        priority = PRIORITY_SOON
    elif kind == "dismiss":
        label = "Dismiss"
        key = f"dismiss:{series_id}"
    elif kind == "ignore":
        label = "Ignore" if ignore else "Include"
        key = f"ignore:{series_id}:{1 if ignore else 0}"
        payload = {"ignore": bool(ignore)}
    else:
        raise HTTPException(status_code=400, detail="Unknown task")
    return _enqueue(
        kind=kind,
        label=label,
        title=title,
        dedupe_key=key,
        sonarr_id=series_id,
        payload=payload,
        priority=priority,
        nbytes=nbytes,
    )


def _scan() -> bool:
    """Read the libraries. Returns False when a scan is already going.

    This does not take the worker lock, so a delete job cannot stall the scan.
    """
    settings = holder.settings
    state = holder.state
    if settings is None or state is None or not settings.configured:
        return False
    if not holder.cycle_lock.acquire(blocking=False):
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
        report = run_cycle(settings, state, sonarr, plex, progress=holder.progress)
        _queue_cycle_results(report)
    except Exception:
        logger.exception("The cycle crashed")
    finally:
        if sonarr is not None:
            sonarr.close()
        if plex is not None:
            plex.close()
        holder.progress.clear()
        holder.running = False
        holder.cycle_lock.release()
        _wake()
    return True


def _queue_cycle_results(report) -> None:
    state = holder.state
    if state is None or report is None:
        return
    now = datetime.now(timezone.utc)
    for result in report.results:
        if result.action == "restore":
            state.cancel_pending_deletes(
                result.sonarr_id,
                now,
                "A restore for this show was queued, so the delete was skipped.",
            )
        if result.queued_delete and not result.error:
            _enqueue(
                kind="delete-later",
                label="Queue season deletion",
                title=result.title,
                dedupe_key=f"delete-later:{result.sonarr_id}",
                sonarr_id=result.sonarr_id,
                payload={"source": "cycle"},
                priority=PRIORITY_DELETE,
                nbytes=int(result.bytes_to_free or 0),
            )


def _apply_dismiss(series_id: int) -> dict:
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


def _run_claimed(job: dict):
    settings = holder.settings
    state = holder.state
    if settings is None or state is None:
        raise RuntimeError("Trimarr is still starting")
    kind = str(job.get("kind") or "")
    if kind == "run":
        if not settings.configured:
            raise RuntimeError("Save Sonarr and Plex on the admin page first")
        if holder.running or not _scan():
            state.unclaim_job(int(job["id"]))
            return "wait"
        return None
    if kind == "watchlist":
        run_watchlist_intake(settings, state)
        return None
    series_id = int(job["sonarr_id"])
    if kind == "dismiss":
        _apply_dismiss(series_id)
        return None
    sonarr = SonarrClient(settings.sonarr_base, settings.sonarr_api_key, settings.http_timeout_seconds)
    try:
        if kind == "delete-later":
            return execute_queued_delete(settings, state, sonarr, series_id, job)
        elif kind == "download-later":
            result = download_later_seasons(settings, state, sonarr, series_id)
            if result.error:
                raise RuntimeError(result.error)
        elif kind == "ignore":
            set_keep_all(
                settings,
                state,
                sonarr,
                series_id,
                bool((job.get("payload") or {}).get("ignore")),
            )
        else:
            raise RuntimeError(f"Unknown task {kind}")
    finally:
        sonarr.close()
    return None


def _finish_job(job: dict, error: str | None = None, detail: str | None = None) -> None:
    assert holder.state is not None
    now = datetime.now(timezone.utc)
    if error:
        finished = holder.state.finish_job(int(job["id"]), "failed", now, error=error[:500])
        if finished:
            _note(job, "failed", error[:500])
        return
    finished = holder.state.finish_job(int(job["id"]), "done", now, detail=detail)
    if finished:
        _note(job, "finished")


def _retriable(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    if status in {502, 503, 504}:
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text or "connection" in text


def _delete_limits(settings: Settings) -> dict:
    return {
        "min_interval_seconds": settings.delete_min_interval_seconds,
        "hourly_series": settings.delete_hourly_series,
        "hourly_bytes": settings.delete_hourly_gigabytes * GIB,
        "daily_bytes": settings.delete_daily_terabytes * TIB,
    }


def _retry_later(job: dict, exc: Exception) -> bool:
    """Queue a transport failure again until it runs out of attempts."""
    assert holder.state is not None
    attempts = int(job.get("attempts") or 0) + 1
    if not _retriable(exc) or attempts >= JOB_MAX_ATTEMPTS:
        return False
    now = datetime.now(timezone.utc)
    holder.state.requeue_job(
        int(job["id"]),
        attempts=attempts,
        not_before=now + timedelta(seconds=RETRY_BACKOFF_SECONDS),
        error="Timed out. It will retry.",
    )
    logger.info("Timed out. It will retry. %s", job.get("title") or "Task")
    return True


def process_next_job() -> bool:
    """Claim one job. The worker lock is always released, including on failure."""
    state = holder.state
    settings = holder.settings
    if state is None:
        return False
    _note_cap_block()
    now = datetime.now(timezone.utc)
    recovered = state.recover_stuck(
        now,
        timeout_seconds=JOB_TIMEOUT_SECONDS,
        max_attempts=JOB_MAX_ATTEMPTS,
    )
    for job in recovered:
        if job.get("status") == "failed":
            _note(job, "failed", str(job.get("error") or "Timed out after several tries."))
    if not holder.mutation.acquire(blocking=False):
        return False
    try:
        paused = state.deletes_paused()
        dry = True if settings is None else settings.dry_run
        job = state.claim_next_job(
            datetime.now(timezone.utc),
            block_deletes=paused or dry,
            block_runs=holder.running,
            limits=_delete_limits(settings) if settings is not None else None,
        )
        if job is None:
            return bool(recovered)
        _note(job, "started")
        try:
            outcome = _run_claimed(job)
        except DeleteSkipped as exc:
            _finish_job(job, detail=str(exc))
        except Exception as exc:
            logger.exception("Queued task %s failed", job.get("id"))
            if not _retry_later(job, exc):
                _finish_job(job, str(exc).strip() or "The task failed")
        else:
            if outcome == "wait":
                return False
            if isinstance(outcome, SeriesResult) and outcome.applied and outcome.files_targeted:
                state.record_deletion(
                    at=datetime.now(timezone.utc),
                    sonarr_id=outcome.sonarr_id,
                    nbytes=outcome.bytes_to_free,
                )
            detail = None
            if isinstance(outcome, SeriesResult) and outcome.protection_result:
                detail = outcome.protection_result
            _finish_job(job, detail=detail)
        return True
    finally:
        holder.mutation.release()
        _wake()


def _recover_stuck_jobs() -> None:
    state = holder.state
    if state is None:
        return
    recovered = state.recover_stuck(
        datetime.now(timezone.utc),
        timeout_seconds=JOB_TIMEOUT_SECONDS,
        max_attempts=JOB_MAX_ATTEMPTS,
    )
    if not recovered:
        return
    for job in recovered:
        if job.get("status") == "failed":
            _note(job, "failed", str(job.get("error") or "Timed out after several tries."))
    _wake()


def cycle() -> bool:
    settings = holder.settings
    state = holder.state
    if settings is None or state is None:
        return False
    if not settings.configured:
        logger.info("Skipped this cycle because Sonarr and Plex are not saved on the admin page.")
        return False
    if holder.running:
        logger.debug("A library scan is already going, so the next one is queued.")
        _enqueue_run()
        return False
    if not _scan():
        logger.debug("A library scan is already going, so the next one is queued.")
        _enqueue_run()
        return False
    return True


def watchlist_poll() -> bool:
    """Queue one watchlist check. It runs ahead of deletes and never skips the interval."""
    settings = holder.settings
    state = holder.state
    if settings is None or state is None or not settings.configured:
        return False
    _job, created = state.enqueue_job(
        created_at=datetime.now(timezone.utc),
        kind="watchlist",
        label="Check watchlists",
        title="Watchlists",
        dedupe_key="watchlist",
        priority=PRIORITY_SOON,
    )
    if not created:
        logger.debug("Watchlist check is already queued behind the current run.")
    _wake()
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
    _announce_restored_queue(holder.state)
    holder.worker_stop.clear()
    holder.worker_wake.clear()
    holder.worker = start_worker(
        holder.worker_stop,
        holder.worker_wake,
        process_next_job,
        _recover_stuck_jobs,
    )
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
        holder.worker_stop.set()
        holder.worker_wake.set()
        if holder.worker is not None:
            holder.worker.join(timeout=2)
            holder.worker = None
        scheduler.shutdown(wait=False)
        holder.scheduler = None


app = FastAPI(title="Trimarr", lifespan=lifespan)


def _announce_restored_queue(state: State) -> None:
    """Log how many tasks were still waiting after a restart. An empty queue stays quiet."""
    summary = getattr(state, "restored_queue", None) or {}
    queued = int(summary.get("queued") or 0)
    in_progress = int(summary.get("in_progress") or 0)
    failed = int(summary.get("failed") or 0)
    now = datetime.now(timezone.utc)
    if queued or in_progress:
        message = restore_line(queued, in_progress)
        logger.info("%s", message)
        state.record_activity(at=now, kind="queued", title="Queue", message=message)
    if failed:
        noun = "task was" if failed == 1 else "tasks were"
        message = (
            f"{failed} in-progress {noun} not restored because they reached the retry limit."
            if failed != 1
            else "1 in-progress task was not restored because it reached the retry limit."
        )
        logger.info("%s", message)
        state.record_activity(at=now, kind="failed", title="Queue", message=message)


def _queue_payload() -> dict:
    assert holder.state is not None
    settings = holder.settings
    view = holder.state.queue_view()
    now = datetime.now(timezone.utc)
    backlog = view.get("delete_backlog") or {}
    limits = _delete_limits(settings) if settings is not None else {
        "min_interval_seconds": 30,
        "hourly_series": 50,
        "hourly_bytes": 500 * GIB,
        "daily_bytes": 3 * TIB,
    }
    usage = holder.state.deletion_usage(now)
    eta = delete_eta(
        int(backlog.get("count") or 0),
        int(backlog.get("bytes") or 0),
        usage,
        limits,
        now,
    ) or ""
    described = queue_block(
        events=holder.state.recent_delete_events(now),
        limits=limits,
        now=now,
        last_at=usage.get("last_at"),
        paused=bool(view.get("deletes_paused")),
        dry_run=bool(settings and settings.dry_run),
        approval_required=bool(settings and settings.require_delete_approval),
        jobs=view["jobs"],
        restores_ahead=holder.state.restores_ahead(),
    )
    view["delete_eta"] = eta
    view["block"] = described["block"]
    view["next_delete_at"] = described["next_delete_at"]
    view["caps"] = described["caps"]
    view["block_log"] = described["block_log"]
    held = bool(view.get("deletes_paused")) or bool(settings and settings.dry_run)
    view["deletes_held"] = held
    if settings is not None and settings.dry_run:
        view["pause_reason"] = "Preview is on, so deletes are paused."
    elif view.get("deletes_paused"):
        view["pause_reason"] = "Deletes are paused."
    else:
        view["pause_reason"] = ""
    return view


def _note_cap_block() -> None:
    """Write a cap or cooldown line when the reason changes, then at most every few minutes."""
    state = holder.state
    if state is None or holder.settings is None:
        return
    view = _queue_payload()
    message = str(view.get("block_log") or "")
    reason = str(view.get("block") or "")
    if not message:
        return
    now = datetime.now(timezone.utc)
    if not state.should_log_block(reason, now):
        return
    logger.info("%s", message)
    state.record_activity(at=now, kind="blocked", title="Queue", message=message)


@app.get("/health")
def health() -> dict:
    state = holder.state
    settings = holder.settings
    queued = 0
    running = 0
    paused = False
    if state is not None:
        view = state.queue_view(recent=0)
        queued = int(view["queued_count"])
        running = int(view["running_count"])
        paused = bool(view["deletes_paused"]) or bool(settings and settings.dry_run)
    alive = holder.worker is not None and holder.worker.is_alive()
    block = "none"
    next_delete_at = None
    caps: list[dict] = []
    if state is not None and settings is not None:
        view = _queue_payload()
        queued = int(view["queued_count"])
        running = int(view["running_count"])
        paused = bool(view["deletes_paused"]) or bool(settings.dry_run)
        block = view.get("block") or "none"
        next_delete_at = view.get("next_delete_at")
        caps = list(view.get("caps") or [])
    return {
        "status": "ok",
        "queue": {
            "queued": queued,
            "running": running,
            "deletes_paused": paused,
            "worker": alive,
            "block": block,
            "next_delete_at": next_delete_at,
            "caps": caps,
        },
    }


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
    allowed = {
        "deleted",
        "restored",
        "approval",
        "approved",
        "dismissed",
        "watchlist",
        "queued",
        "started",
        "finished",
        "failed",
        "blocked",
        "protected",
    }
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
        "keep_current_season": settings.keep_current_season,
        "tag": settings.ignore_tag,
        "ignore_tag": settings.ignore_tag,
        "require_delete_approval": settings.require_delete_approval,
        "live_series_ids": list(settings.live_series_ids),
        "running": holder.running,
        "progress": holder.progress.snapshot() if holder.running else None,
        "queue": _queue_payload(),
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
    job = _enqueue_run()
    body = _status_body()
    body["job"] = job
    return JSONResponse(body, headers=NO_STORE)


@app.get("/api/queue")
def queue_list(request: Request) -> JSONResponse:
    _require_ready()
    _require_token(request)
    return JSONResponse(_queue_payload(), headers=NO_STORE)


@app.post("/api/queue/pause")
def queue_pause(body: PauseBody, request: Request) -> JSONResponse:
    """Stop or resume deletes. Restores keep running. Preview also holds deletes."""
    _require_ready()
    _require_token(request)
    assert holder.state is not None
    holder.state.set_deletes_paused(body.paused)
    logger.info("Deletes are paused." if body.paused else "Deletes can run again.")
    _wake()
    return JSONResponse(_queue_payload(), headers=NO_STORE)


@app.post("/api/queue/bulk")
def queue_bulk(body: BulkBody, request: Request) -> dict:
    """Queue the same action for many shows. Each one is deduped and returns immediately."""
    settings = _require_ready()
    _require_token(request)
    _require_configured(settings)
    if body.kind != "dismiss" and not body.confirm:
        raise HTTPException(status_code=400, detail="Confirm the action in the page before it runs")
    if not body.series_ids:
        raise HTTPException(status_code=400, detail="Choose at least one show")
    jobs = [_enqueue_series(body.kind, series_id) for series_id in body.series_ids]
    return {"queued": True, "jobs": jobs}


@app.post("/api/queue/{job_id}/cancel")
def cancel_queued(job_id: int, request: Request) -> dict:
    _require_ready()
    _require_token(request)
    assert holder.state is not None
    job = holder.state.cancel_job(job_id, datetime.now(timezone.utc))
    if job is None:
        raise HTTPException(status_code=404, detail="That task is not in the queue")
    if not job.get("cancelled_now"):
        raise HTTPException(status_code=409, detail="That task already started")
    return job


@app.get("/api/series/{series_id}")
def series_preview(series_id: int, request: Request) -> dict:
    settings = _require_ready()
    _require_token(request)
    _require_configured(settings)
    assert holder.state is not None
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


def _queue_one(request: Request, series_id: int, kind: str, *, confirm: bool, ignore: bool | None = None) -> dict:
    settings = _require_ready()
    _require_token(request)
    _require_configured(settings)
    if kind in {"delete-later", "download-later"} and not confirm:
        raise HTTPException(status_code=400, detail="Confirm the action in the page before it runs")
    return {"queued": True, "job": _enqueue_series(kind, series_id, ignore=ignore)}


@app.post("/api/series/{series_id}/delete-later")
def delete_one(series_id: int, body: ConfirmBody, request: Request) -> dict:
    return _queue_one(request, series_id, "delete-later", confirm=body.confirm)


@app.post("/api/series/{series_id}/download-later")
def download_one(series_id: int, body: ConfirmBody, request: Request) -> dict:
    return _queue_one(request, series_id, "download-later", confirm=body.confirm)


@app.post("/api/series/{series_id}/dismiss")
def dismiss_one(series_id: int, request: Request) -> dict:
    """Queue a dismissal. The files stay until a later approved delete."""
    return _queue_one(request, series_id, "dismiss", confirm=True)


@app.post("/api/series/{series_id}/ignore")
def ignore_one(series_id: int, body: IgnoreBody, request: Request) -> dict:
    return _queue_one(request, series_id, "ignore", confirm=True, ignore=body.ignore)


def main() -> None:
    runtime = RuntimeSettings()
    configure_logging(runtime.log_level)
    uvicorn.run(app, host="0.0.0.0", port=runtime.port, log_level=runtime.log_level.lower())


if __name__ == "__main__":
    main()
