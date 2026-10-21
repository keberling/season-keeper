"""SQLite memory for search cooldowns and the last report shown in the GUI."""

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
