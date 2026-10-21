"""Live scan progress shared by the cycle thread and the status page.

The cycle thread updates this object. GET /api/status reads a snapshot.
Messages stay free of URLs, tokens, and API keys.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressSnapshot:
    message: str
    done: int
    total: int | None

    def to_dict(self) -> dict:
        return {"message": self.message, "done": self.done, "total": self.total}


class Progress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: ProgressSnapshot | None = None

    def update(self, message: str, done: int = 0, total: int | None = None) -> None:
        snap = ProgressSnapshot(message=message, done=done, total=total)
        with self._lock:
            self._snapshot = snap

    def snapshot(self) -> dict | None:
        with self._lock:
            current = self._snapshot
        if current is None:
            return None
        return current.to_dict()

    def clear(self) -> None:
        with self._lock:
            self._snapshot = None


def report(progress: Progress | None, message: str, done: int = 0, total: int | None = None) -> None:
    if progress is not None:
        progress.update(message, done, total)


def count_phrase(count: int, singular: str) -> str:
    word = singular if count == 1 else f"{singular}s"
    return f"{count:,} {word}"
