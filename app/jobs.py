"""Background worker for the persistent task queue.

The worker claims one job at a time and always releases its lock. A crash
inside a job is caught. If the worker itself stops, it starts again. A second
thread marks a job that stays in Working past the timeout so it can be retried.
The library scan does not use this lock.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("trimarr")


def serve(stop: threading.Event, wake: threading.Event, step) -> None:
    """Call step until stop is set. step returns True when it ran a job."""
    while not stop.is_set():
        worked = False
        try:
            worked = bool(step())
        except Exception:
            logger.exception("The task queue hit an unexpected error")
        if stop.is_set():
            return
        if worked:
            continue
        wake.wait(0.3)
        wake.clear()


def supervise(stop: threading.Event, wake: threading.Event, step) -> None:
    """Run the worker until stop. Restart it if it ever returns or crashes."""
    while not stop.is_set():
        try:
            serve(stop, wake, step)
        except Exception:
            logger.exception("The task worker stopped and is restarting")
        if stop.is_set():
            return
        logger.error("The task worker stopped and is restarting")
        if stop.wait(0.5):
            return


def start_worker(stop: threading.Event, wake: threading.Event, step, recover=None) -> threading.Thread:
    def watch() -> None:
        while not stop.wait(5):
            if recover is None:
                continue
            try:
                recover()
            except Exception:
                logger.exception("Could not recover a stuck task")

    threading.Thread(target=watch, name="trimarr-jobs-watch", daemon=True).start()
    thread = threading.Thread(
        target=supervise,
        args=(stop, wake, step),
        name="trimarr-jobs",
        daemon=True,
    )
    thread.start()
    return thread
