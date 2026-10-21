"""SQLite memory for search cooldowns, the last report, and the activity log."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import RunReport, summarize_dicts


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
                SELECT COALESCE(SUM(CASE WHEN kind = 'restored' THEN seasons ELSE 0 END), 0)
                FROM activity
                """
            ).fetchone()
        restored = int(row[0])
        return {"restored_seasons": restored, "searches": restored}

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
