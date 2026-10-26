"""SQLite memory for search cooldowns, the last report, and the activity log."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import RunReport, summarize_dicts
from app.queue_limit import (
    BLOCK_LOG_INTERVAL_SECONDS,
    CAP_BLOCKS,
    JOB_MAX_ATTEMPTS,
    PRIORITY_SOON,
    RESTORE_CANCEL,
    deletion_allowed,
)

logger = logging.getLogger("trimarr")


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS searches (
                sonarr_id INTEGER NOT NULL,
                season_number INTEGER NOT NULL,
                searched_at TEXT NOT NULL,
                PRIMARY KEY (sonarr_id, season_number)
            );
            CREATE TABLE IF NOT EXISTS latest_report (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS freed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                freed_at TEXT NOT NULL,
                sonarr_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                file_count INTEGER NOT NULL,
                bytes INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dismissed (
                sonarr_id INTEGER PRIMARY KEY,
                dismissed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                sonarr_id INTEGER,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                bytes INTEGER NOT NULL DEFAULT 0,
                seasons INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS watchlist_seen (
                item_key TEXT PRIMARY KEY,
                media TEXT NOT NULL,
                title TEXT NOT NULL,
                added_by TEXT NOT NULL,
                plex_guid TEXT,
                imdb_id TEXT,
                tvdb_id INTEGER,
                tmdb_id INTEGER,
                first_seen_at TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                sonarr_id INTEGER,
                radarr_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS watchlist_marks (
                tvdb_id INTEGER PRIMARY KEY,
                watchlisted_at TEXT NOT NULL,
                watchlisted_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                status TEXT NOT NULL,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                sonarr_id INTEGER,
                title TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                dedupe_key TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 1,
                nbytes INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                not_before TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, id);
            CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, priority, id);
            CREATE TABLE IF NOT EXISTS delete_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                sonarr_id INTEGER NOT NULL,
                bytes INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS queue_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS season_arrivals (
                sonarr_id INTEGER NOT NULL,
                season_number INTEGER NOT NULL,
                first_seen TEXT NOT NULL,
                PRIMARY KEY (sonarr_id, season_number)
            );
            CREATE TABLE IF NOT EXISTS arrival_seeds (
                sonarr_id INTEGER PRIMARY KEY,
                seeded_at TEXT,
                requested_at TEXT,
                not_before TEXT
            );
            CREATE TABLE IF NOT EXISTS cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                finished_at TEXT NOT NULL,
                dry_run INTEGER NOT NULL,
                summary TEXT NOT NULL,
                prune INTEGER NOT NULL,
                hold INTEGER NOT NULL,
                restore INTEGER NOT NULL,
                skip INTEGER NOT NULL,
                pending INTEGER NOT NULL,
                protected INTEGER NOT NULL,
                searches INTEGER NOT NULL,
                restored_seasons INTEGER NOT NULL,
                freed_bytes INTEGER NOT NULL,
                pending_bytes INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()
        self._migrate_jobs()
        self.restored_queue = self.requeue_interrupted(max_attempts=JOB_MAX_ATTEMPTS)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def in_cooldown(
        self,
        series_id: int,
        season: int,
        now: datetime,
        cooldown: timedelta,
    ) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT searched_at FROM searches WHERE sonarr_id = ? AND season_number = ?",
                (series_id, season),
            ).fetchone()
        if row is None:
            return False
        searched = datetime.fromisoformat(row[0])
        if searched.tzinfo is None:
            searched = searched.replace(tzinfo=timezone.utc)
        return now - searched < cooldown

    def mark_search(self, series_id: int, season: int, now: datetime) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO searches (sonarr_id, season_number, searched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(sonarr_id, season_number)
                DO UPDATE SET searched_at = excluded.searched_at
                """,
                (series_id, season, now.isoformat()),
            )
            self._conn.commit()

    def record_freed(
        self,
        *,
        sonarr_id: int,
        title: str,
        file_count: int,
        nbytes: int,
        when: datetime,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO freed (freed_at, sonarr_id, title, file_count, bytes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (when.isoformat(), sonarr_id, title, file_count, nbytes),
            )
            self._conn.commit()

    def freed_total_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM freed").fetchone()
        return int(row[0])

    def freed_bytes_since(self, when: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM freed WHERE freed_at >= ?",
                (when.isoformat(),),
            ).fetchone()
        return int(row[0])

    def record_activity(
        self,
        *,
        at: datetime,
        kind: str,
        title: str,
        message: str,
        sonarr_id: int | None = None,
        nbytes: int = 0,
        seasons: int = 0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activity (at, kind, sonarr_id, title, message, bytes, seasons)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (at.isoformat(), kind, sonarr_id, title, message, nbytes, seasons),
            )
            self._conn.commit()

    def last_activity_message(self, sonarr_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT message FROM activity
                WHERE sonarr_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(sonarr_id),),
            ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def list_activity(
        self,
        *,
        kind: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if query and query.strip():
            clauses.append("(title LIKE ? OR message LIKE ?)")
            needle = f"%{query.strip()}%"
            params.extend((needle, needle))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT id, at, kind, sonarr_id, title, message, bytes, seasons
                FROM activity
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "id": row[0],
                "at": row[1],
                "kind": row[2],
                "sonarr_id": row[3],
                "title": row[4],
                "message": row[5],
                "bytes": row[6],
                "seasons": row[7],
            }
            for row in rows
        ]

    def activity_totals(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN kind = 'restored' THEN seasons ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN kind = 'watchlist' AND seasons > 0 THEN 1 ELSE 0 END), 0)
                FROM activity
                """
            ).fetchone()
        restored = int(row[0])
        return {
            "restored_seasons": restored,
            "searches": restored,
            "watchlist_adds": int(row[1]),
        }

    def record_cycle(
        self,
        *,
        finished_at: str,
        dry_run: bool,
        summary: str,
        prune: int,
        hold: int,
        restore: int,
        skip: int,
        pending: int,
        protected: int,
        searches: int,
        restored_seasons: int,
        freed_bytes: int,
        pending_bytes: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cycles (
                    finished_at, dry_run, summary, prune, hold, restore, skip,
                    pending, protected, searches, restored_seasons, freed_bytes, pending_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finished_at,
                    1 if dry_run else 0,
                    summary,
                    prune,
                    hold,
                    restore,
                    skip,
                    pending,
                    protected,
                    searches,
                    restored_seasons,
                    freed_bytes,
                    pending_bytes,
                ),
            )
            self._conn.commit()

    def list_cycles(self, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT finished_at, dry_run, summary, prune, hold, restore, skip,
                       pending, protected, searches, restored_seasons, freed_bytes, pending_bytes
                FROM cycles
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        keys = (
            "finished_at",
            "dry_run",
            "summary",
            "prune",
            "hold",
            "restore",
            "skip",
            "pending",
            "protected",
            "searches",
            "restored_seasons",
            "freed_bytes",
            "pending_bytes",
        )
        return [
            {
                **dict(zip(keys, row, strict=True)),
                "dry_run": bool(row[1]),
            }
            for row in rows
        ]

    def freed_events(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT freed_at, sonarr_id, title, file_count, bytes
                FROM freed
                ORDER BY id DESC
                LIMIT 200
                """
            ).fetchall()
        return [
            {
                "freed_at": freed_at,
                "sonarr_id": sonarr_id,
                "title": title,
                "file_count": file_count,
                "bytes": nbytes,
            }
            for freed_at, sonarr_id, title, file_count, nbytes in rows
        ]

    def get_watchlist_item(self, item_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT item_key, media, title, added_by, plex_guid, imdb_id, tvdb_id, tmdb_id,
                       first_seen_at, action, detail, sonarr_id, radarr_id
                FROM watchlist_seen
                WHERE item_key = ?
                """,
                (item_key,),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "item_key",
            "media",
            "title",
            "added_by",
            "plex_guid",
            "imdb_id",
            "tvdb_id",
            "tmdb_id",
            "first_seen_at",
            "action",
            "detail",
            "sonarr_id",
            "radarr_id",
        )
        return dict(zip(keys, row, strict=True))

    def save_watchlist_item(
        self,
        *,
        item_key: str,
        media: str,
        title: str,
        added_by: str,
        plex_guid: str | None,
        imdb_id: str | None,
        tvdb_id: int | None,
        tmdb_id: int | None,
        first_seen_at: str,
        action: str,
        detail: str,
        sonarr_id: int | None = None,
        radarr_id: int | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO watchlist_seen (
                    item_key, media, title, added_by, plex_guid, imdb_id, tvdb_id, tmdb_id,
                    first_seen_at, action, detail, sonarr_id, radarr_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    media = excluded.media,
                    title = excluded.title,
                    added_by = excluded.added_by,
                    plex_guid = excluded.plex_guid,
                    imdb_id = excluded.imdb_id,
                    tvdb_id = excluded.tvdb_id,
                    tmdb_id = excluded.tmdb_id,
                    action = excluded.action,
                    detail = excluded.detail,
                    sonarr_id = excluded.sonarr_id,
                    radarr_id = excluded.radarr_id
                """,
                (
                    item_key,
                    media,
                    title,
                    added_by,
                    plex_guid,
                    imdb_id,
                    tvdb_id,
                    tmdb_id,
                    first_seen_at,
                    action,
                    detail,
                    sonarr_id,
                    radarr_id,
                ),
            )
            self._conn.commit()

    def remember_watchlist_mark(self, tvdb_id: int, when: datetime, who: str) -> None:
        """First observation wins. Later polls do not move the quiet-day clock."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO watchlist_marks (tvdb_id, watchlisted_at, watchlisted_by)
                VALUES (?, ?, ?)
                ON CONFLICT(tvdb_id) DO NOTHING
                """,
                (int(tvdb_id), when.isoformat(), who),
            )
            self._conn.commit()

    def watchlist_marks(self) -> dict[int, tuple[datetime, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tvdb_id, watchlisted_at, watchlisted_by FROM watchlist_marks"
            ).fetchall()
        marks: dict[int, tuple[datetime, str]] = {}
        for tvdb_id, raw_when, who in rows:
            when = datetime.fromisoformat(raw_when)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            marks[int(tvdb_id)] = (when, str(who or "Someone"))
        return marks

    def watchlist_feed_status(self) -> dict:
        """Last watchlist fetch. error is empty after a good read."""
        with self._lock:
            raw = self._meta_locked("watchlist_feed")
        if not raw:
            return {"error": None, "checked_at": None, "last_success_at": None}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"error": None, "checked_at": None, "last_success_at": None}
        if not isinstance(data, dict):
            return {"error": None, "checked_at": None, "last_success_at": None}
        error = data.get("error") or None
        return {
            "error": None if error is None else str(error),
            "checked_at": data.get("checked_at") or None,
            "last_success_at": data.get("last_success_at") or None,
        }

    def note_watchlist_feed(self, *, error: str | None, at: datetime) -> dict:
        """Record this attempt. A good fetch clears the error and moves the last success."""
        current = self.watchlist_feed_status()
        checked = at.astimezone(timezone.utc).isoformat()
        last_success = current.get("last_success_at")
        message = (error or "").strip() or None
        if message is None:
            last_success = checked
        payload = json.dumps(
            {"error": message, "checked_at": checked, "last_success_at": last_success}
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO queue_meta (key, value) VALUES ('watchlist_feed', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (payload,),
            )
            self._conn.commit()
        return {"error": message, "checked_at": checked, "last_success_at": last_success}

    def dismiss(self, sonarr_id: int, when: datetime) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO dismissed (sonarr_id, dismissed_at)
                VALUES (?, ?)
                ON CONFLICT(sonarr_id) DO UPDATE SET dismissed_at = excluded.dismissed_at
                """,
                (sonarr_id, when.isoformat()),
            )
            self._conn.commit()

    def clear_dismissed(self, sonarr_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM dismissed WHERE sonarr_id = ?", (sonarr_id,))
            self._conn.commit()

    def is_dismissed(self, sonarr_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM dismissed WHERE sonarr_id = ?",
                (sonarr_id,),
            ).fetchone()
        return row is not None

    def dismissed_ids(self) -> set[int]:
        with self._lock:
            rows = self._conn.execute("SELECT sonarr_id FROM dismissed").fetchall()
        return {int(row[0]) for row in rows}

    def save_report(self, report: RunReport) -> None:
        self._write(report.to_dict(), report.finished_at)

    def load_report(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM latest_report WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def replace_series(self, series: dict) -> None:
        current = self.load_report()
        if not current:
            return
        rows = list(current.get("series") or [])
        found = False
        for index, row in enumerate(rows):
            if row.get("sonarr_id") == series.get("sonarr_id"):
                rows[index] = series
                found = True
                break
        if not found:
            rows.append(series)
        current["series"] = rows
        current.update(summarize_dicts(rows))
        self._write(current, datetime.now(timezone.utc).isoformat())

    def _write(self, payload: dict, updated_at: str) -> None:
        encoded = json.dumps(payload)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO latest_report (id, payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (encoded, updated_at),
            )
            self._conn.commit()

    def _migrate_jobs(self) -> None:
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        additions = {
            "priority": "INTEGER NOT NULL DEFAULT 1",
            "nbytes": "INTEGER NOT NULL DEFAULT 0",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "not_before": "TEXT",
            "detail": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS delete_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                sonarr_id INTEGER NOT NULL,
                bytes INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def requeue_interrupted(self, max_attempts: int = JOB_MAX_ATTEMPTS) -> dict:
        """Put interrupted jobs back. A job that already used its attempts is failed.

        Queued jobs are left as they are. The attempt count is not increased:
        the process stopped before that try finished, and a later try still
        counts against the limit.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status = 'running'"
            ).fetchall()
            resumed = 0
            failed = 0
            for row in rows:
                job = _job_dict(row)
                if int(job["attempts"]) >= int(max_attempts):
                    self._conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed', finished_at = ?, error = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (
                            now,
                            "It reached the retry limit before the restart.",
                            job["id"],
                        ),
                    )
                    failed += 1
                else:
                    self._conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'queued', started_at = NULL, not_before = NULL
                        WHERE id = ? AND status = 'running'
                        """,
                        (job["id"],),
                    )
                    resumed += 1
            queued = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
            ).fetchone()
            self._conn.commit()
        return {
            "queued": int(queued[0]),
            "in_progress": resumed,
            "failed": failed,
        }

    def enqueue_job(
        self,
        *,
        created_at: datetime,
        kind: str,
        label: str,
        title: str,
        dedupe_key: str,
        sonarr_id: int | None = None,
        payload: dict | None = None,
        priority: int = 1,
        nbytes: int = 0,
    ) -> tuple[dict, bool]:
        """Insert a queued job, or return the identical one that is already waiting.

        The second value is True when a new row was inserted. A download cancels
        any pending delete for the same show.
        """
        body = json.dumps(payload or {})
        with self._lock:
            if kind == "download-later" and sonarr_id is not None:
                self._cancel_deletes_locked(int(sonarr_id), created_at, RESTORE_CANCEL)
            existing = self._conn.execute(
                f"""
                SELECT {_JOB_COLUMNS}
                FROM jobs
                WHERE dedupe_key = ? AND status = 'queued'
                ORDER BY id
                LIMIT 1
                """,
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                if kind == "delete-later" and (payload or {}).get("source") == "user":
                    self._conn.execute(
                        "UPDATE jobs SET payload = ?, label = ?, nbytes = ? WHERE id = ?",
                        (body, label, int(nbytes), int(existing[0])),
                    )
                self._conn.commit()
                reloaded = self._conn.execute(
                    f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?",
                    (int(existing[0]),),
                ).fetchone()
                return _job_dict(reloaded), False
            cursor = self._conn.execute(
                """
                INSERT INTO jobs (
                    created_at, status, kind, label, sonarr_id, title, payload,
                    dedupe_key, priority, nbytes, attempts
                )
                VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    created_at.isoformat(),
                    kind,
                    label,
                    sonarr_id,
                    title,
                    body,
                    dedupe_key,
                    int(priority),
                    int(nbytes),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return _job_dict(row), True

    def claim_next_job(
        self,
        now: datetime,
        *,
        block_deletes: bool = False,
        block_runs: bool = False,
        limits: dict | None = None,
    ) -> dict | None:
        """Claim the next job. Restores come before deletes. Deletes can be left waiting."""
        moment = now.isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, kind, nbytes FROM jobs
                WHERE status = 'queued' AND (not_before IS NULL OR not_before <= ?)
                ORDER BY priority ASC, id ASC
                """,
                (moment,),
            ).fetchall()
            usage = self._deletion_usage_locked(now) if limits else None
            chosen: int | None = None
            alone = ""
            for row in rows:
                kind = str(row[1])
                if kind == "run" and block_runs:
                    continue
                if kind == "delete-later":
                    if block_deletes:
                        continue
                    if limits is not None and usage is not None:
                        allowed, reason = deletion_allowed(
                            usage, limits, now, pending_bytes=int(row[2] or 0)
                        )
                        if not allowed:
                            continue
                        alone = reason
                chosen = int(row[0])
                break
            if chosen is not None and alone:
                logger.info("%s", alone)
            if chosen is None:
                return None
            cursor = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (moment, chosen),
            )
            self._conn.commit()
            if cursor.rowcount != 1:
                return None
            loaded = self._conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?",
                (chosen,),
            ).fetchone()
        return _job_dict(loaded)

    def finish_job(
        self,
        job_id: int,
        status: str,
        now: datetime,
        error: str | None = None,
        detail: str | None = None,
        attempts: int | None = None,
    ) -> dict | None:
        """Finish a job that is still running. A recovered job is left alone."""
        with self._lock:
            assignments = ["status = ?", "finished_at = ?", "error = ?"]
            values: list[object] = [status, now.isoformat(), error]
            if detail is not None:
                assignments.append("detail = ?")
                values.append(detail)
            if attempts is not None:
                assignments.append("attempts = ?")
                values.append(int(attempts))
            values.append(job_id)
            cursor = self._conn.execute(
                f"""
                UPDATE jobs
                SET {", ".join(assignments)}
                WHERE id = ? AND status = 'running'
                """,
                values,
            )
            self._conn.commit()
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return _job_dict(row)

    def requeue_job(
        self,
        job_id: int,
        *,
        attempts: int,
        not_before: datetime,
        error: str | None,
    ) -> dict | None:
        """Put a running job back on the queue. Used for timeouts, not restarts."""
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', started_at = NULL, attempts = ?, not_before = ?, error = ?
                WHERE id = ? AND status = 'running'
                """,
                (int(attempts), not_before.isoformat(), error, job_id),
            )
            self._conn.commit()
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return _job_dict(row) if row else None

    def unclaim_job(self, job_id: int) -> None:
        """A scan is already going. Put this run back without counting an attempt."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', started_at = NULL
                WHERE id = ? AND status = 'running'
                """,
                (job_id,),
            )
            self._conn.commit()

    def recover_stuck(self, now: datetime, *, timeout_seconds: int, max_attempts: int) -> list[dict]:
        """Mark a Working job that outlived its timeout as failed, or queue it again."""
        cutoff = (now - timedelta(seconds=timeout_seconds)).isoformat()
        recovered: list[dict] = []
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT {_JOB_COLUMNS} FROM jobs
                WHERE status = 'running' AND started_at IS NOT NULL AND started_at <= ?
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                job = _job_dict(row)
                attempts = int(job["attempts"]) + 1
                if attempts >= max_attempts:
                    self._conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed', finished_at = ?, attempts = ?, error = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (
                            now.isoformat(),
                            attempts,
                            "Timed out after several tries.",
                            job["id"],
                        ),
                    )
                    job["status"] = "failed"
                    job["attempts"] = attempts
                    job["error"] = "Timed out after several tries."
                else:
                    self._conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'queued', started_at = NULL, attempts = ?,
                            not_before = ?, error = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (
                            attempts,
                            now.isoformat(),
                            "Timed out. It will retry.",
                            job["id"],
                        ),
                    )
                    job["status"] = "queued"
                    job["attempts"] = attempts
                    job["error"] = "Timed out. It will retry."
                recovered.append(job)
            if recovered:
                self._conn.commit()
        return recovered

    def cancel_job(self, job_id: int, now: datetime) -> dict | None:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', finished_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now.isoformat(), job_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            cancelled = cursor.rowcount == 1
        if row is None:
            return None
        job = _job_dict(row)
        job["cancelled_now"] = cancelled
        return job

    def cancel_pending_deletes(self, sonarr_id: int, now: datetime, reason: str) -> int:
        with self._lock:
            count = self._cancel_deletes_locked(int(sonarr_id), now, reason)
            self._conn.commit()
            return count

    def restore_pending(self, sonarr_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM jobs
                WHERE sonarr_id = ? AND kind = 'download-later'
                  AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (int(sonarr_id),),
            ).fetchone()
        return row is not None

    def run_pending(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM jobs
                WHERE kind = 'run' AND status IN ('queued', 'running')
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def deletes_paused(self) -> bool:
        with self._lock:
            return self._meta_locked("deletes_paused") == "1"

    def set_deletes_paused(self, paused: bool) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO queue_meta (key, value) VALUES ('deletes_paused', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("1" if paused else "0",),
            )
            self._conn.commit()

    def season_arrivals(self, series_id: int) -> dict[int, datetime]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT season_number, first_seen FROM season_arrivals
                WHERE sonarr_id = ?
                """,
                (int(series_id),),
            ).fetchall()
        found: dict[int, datetime] = {}
        for season, raw in rows:
            try:
                parsed = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            found[int(season)] = parsed.astimezone(timezone.utc)
        return found

    def remember_arrivals(self, series_id: int, arrivals: dict[int, datetime]) -> None:
        """Save a first-seen date. A later re-download does not move it forward."""
        if not arrivals:
            return
        with self._lock:
            for season, when in arrivals.items():
                if int(season) == 0 or when is None:
                    continue
                moment = when.astimezone(timezone.utc)
                row = self._conn.execute(
                    """
                    SELECT first_seen FROM season_arrivals
                    WHERE sonarr_id = ? AND season_number = ?
                    """,
                    (int(series_id), int(season)),
                ).fetchone()
                if row is not None:
                    try:
                        current = datetime.fromisoformat(str(row[0]))
                    except ValueError:
                        current = None
                    if current is not None:
                        if current.tzinfo is None:
                            current = current.replace(tzinfo=timezone.utc)
                        if moment >= current.astimezone(timezone.utc):
                            continue
                self._conn.execute(
                    """
                    INSERT INTO season_arrivals (sonarr_id, season_number, first_seen)
                    VALUES (?, ?, ?)
                    ON CONFLICT(sonarr_id, season_number) DO UPDATE SET
                        first_seen = excluded.first_seen
                    """,
                    (int(series_id), int(season), moment.isoformat()),
                )
            self._conn.commit()

    def arrival_seeded(self, series_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT seeded_at FROM arrival_seeds WHERE sonarr_id = ?",
                (int(series_id),),
            ).fetchone()
        return bool(row and row[0])

    def note_arrival_seed(self, series_id: int, now: datetime) -> None:
        """Ask the background seeder to read this show once. A finished seed stays finished."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO arrival_seeds (sonarr_id, requested_at)
                VALUES (?, ?)
                ON CONFLICT(sonarr_id) DO UPDATE SET
                    requested_at = excluded.requested_at
                WHERE arrival_seeds.seeded_at IS NULL
                """,
                (int(series_id), now.astimezone(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def next_arrival_seed(self, now: datetime) -> int | None:
        moment = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT sonarr_id FROM arrival_seeds
                WHERE seeded_at IS NULL AND (not_before IS NULL OR not_before <= ?)
                ORDER BY requested_at DESC, sonarr_id ASC
                LIMIT 1
                """,
                (moment,),
            ).fetchone()
        if row is None:
            return None
        return int(row[0])

    def mark_arrival_seeded(self, series_id: int, now: datetime) -> None:
        moment = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO arrival_seeds (sonarr_id, seeded_at, requested_at)
                VALUES (?, ?, ?)
                ON CONFLICT(sonarr_id) DO UPDATE SET
                    seeded_at = excluded.seeded_at,
                    not_before = NULL
                """,
                (int(series_id), moment, moment),
            )
            self._conn.commit()

    def defer_arrival_seed(self, series_id: int, not_before: datetime) -> None:
        moment = not_before.astimezone(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO arrival_seeds (sonarr_id, requested_at, not_before)
                VALUES (?, ?, ?)
                ON CONFLICT(sonarr_id) DO UPDATE SET
                    not_before = excluded.not_before
                WHERE arrival_seeds.seeded_at IS NULL
                """,
                (int(series_id), moment, moment),
            )
            self._conn.commit()

    def record_deletion(self, *, at: datetime, sonarr_id: int, nbytes: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO delete_events (at, sonarr_id, bytes) VALUES (?, ?, ?)
                """,
                (at.isoformat(), int(sonarr_id), int(nbytes)),
            )
            self._conn.commit()

    def deletion_usage(self, now: datetime) -> dict:
        with self._lock:
            return self._deletion_usage_locked(now)

    def recent_delete_events(self, now: datetime) -> list[dict]:
        """Deletes in the last day, which is enough for the hour and day caps."""
        day = (now - timedelta(days=1)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, bytes FROM delete_events WHERE at >= ? ORDER BY at, id",
                (day,),
            ).fetchall()
        return [{"at": row[0], "bytes": int(row[1])} for row in rows]

    def restores_ahead(self) -> bool:
        """A restore, search, or watchlist check is queued or running ahead of deletes."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM jobs
                WHERE status IN ('queued', 'running') AND priority = ?
                LIMIT 1
                """,
                (PRIORITY_SOON,),
            ).fetchone()
        return row is not None

    def should_log_block(self, reason: str, now: datetime, interval_seconds: int = BLOCK_LOG_INTERVAL_SECONDS) -> bool:
        """True when a cap or cooldown should be written again.

        A new reason is logged immediately. The same reason is logged at most
        once per interval.
        """
        if reason not in CAP_BLOCKS:
            return False
        with self._lock:
            previous = self._meta_locked("block_reason")
            logged = self._meta_locked("block_logged_at")
            moment = None
            if logged:
                try:
                    moment = datetime.fromisoformat(logged)
                except ValueError:
                    moment = None
                if moment is not None and moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
            changed = previous != reason
            stale = moment is None or (now - moment).total_seconds() >= interval_seconds
            if not changed and not stale:
                return False
            self._conn.execute(
                """
                INSERT INTO queue_meta (key, value) VALUES ('block_reason', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (reason,),
            )
            self._conn.execute(
                """
                INSERT INTO queue_meta (key, value) VALUES ('block_logged_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (now.isoformat(),),
            )
            self._conn.commit()
        return True

    def queue_view(self, recent: int = 20) -> dict:
        """Pending and running tasks, plus the latest finished ones. Watchlist checks stay internal."""
        hidden = "kind != 'watchlist'"
        with self._lock:
            active = self._conn.execute(
                f"""
                SELECT {_JOB_COLUMNS} FROM jobs
                WHERE status IN ('queued', 'running') AND {hidden}
                ORDER BY priority ASC, id ASC
                """
            ).fetchall()
            finished = self._conn.execute(
                f"""
                SELECT {_JOB_COLUMNS} FROM jobs
                WHERE status IN ('done', 'failed', 'cancelled') AND {hidden}
                ORDER BY id DESC
                LIMIT ?
                """,
                (recent,),
            ).fetchall()
            counts = self._conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0)
                FROM jobs
                WHERE {hidden}
                """
            ).fetchone()
            backlog = self._conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(nbytes), 0) FROM jobs
                WHERE kind = 'delete-later' AND status = 'queued'
                """
            ).fetchone()
            paused = self._meta_locked("deletes_paused") == "1"
        jobs = [_job_dict(row) for row in active]
        jobs.extend(_job_dict(row) for row in finished)
        return {
            "queued_count": int(counts[0]),
            "running_count": int(counts[1]),
            "deletes_paused": paused,
            "delete_backlog": {"count": int(backlog[0]), "bytes": int(backlog[1])},
            "jobs": jobs,
        }

    def _cancel_deletes_locked(self, sonarr_id: int, now: datetime, reason: str) -> int:
        cursor = self._conn.execute(
            """
            UPDATE jobs
            SET status = 'cancelled', finished_at = ?, detail = ?
            WHERE status = 'queued' AND kind = 'delete-later' AND sonarr_id = ?
            """,
            (now.isoformat(), reason, sonarr_id),
        )
        return int(cursor.rowcount or 0)

    def _meta_locked(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM queue_meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def _deletion_usage_locked(self, now: datetime) -> dict:
        hour = (now - timedelta(hours=1)).isoformat()
        day = (now - timedelta(days=1)).isoformat()
        last = self._conn.execute(
            "SELECT at FROM delete_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        hour_row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM delete_events WHERE at >= ?",
            (hour,),
        ).fetchone()
        day_row = self._conn.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM delete_events WHERE at >= ?",
            (day,),
        ).fetchone()
        return {
            "last_at": None if last is None else last[0],
            "hour_series": int(hour_row[0]),
            "hour_bytes": int(hour_row[1]),
            "day_bytes": int(day_row[0]),
        }


_JOB_COLUMNS = (
    "id, created_at, started_at, finished_at, status, kind, label, "
    "sonarr_id, title, payload, error, dedupe_key, priority, nbytes, "
    "attempts, not_before, detail"
)


def _job_dict(row: tuple) -> dict:
    payload = {}
    try:
        payload = json.loads(row[9] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "id": int(row[0]),
        "created_at": row[1],
        "started_at": row[2],
        "finished_at": row[3],
        "status": row[4],
        "kind": row[5],
        "label": row[6],
        "sonarr_id": None if row[7] is None else int(row[7]),
        "title": row[8],
        "payload": payload,
        "error": row[10],
        "dedupe_key": row[11],
        "priority": int(row[12] or 0),
        "nbytes": int(row[13] or 0),
        "attempts": int(row[14] or 0),
        "not_before": row[15],
        "detail": row[16],
    }
