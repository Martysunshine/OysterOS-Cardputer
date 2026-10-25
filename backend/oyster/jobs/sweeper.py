"""Background expiry sweep: jobs whose deadline passed without a result become expired/unknown_outcome.

The store also sweeps lazily before each job delivery, so correctness never depends on this thread.
"""

from __future__ import annotations

import logging
import threading
import time

from oyster.store import Store

log = logging.getLogger("oyster.jobs")


class ExpirySweeper:
    def __init__(self, store: Store, interval_s: float = 5.0):
        self._store = store
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="oyster-expiry-sweep", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def sweep_once(self) -> int:
        clock = getattr(self._store, "now", time.time)
        n = self._store.expire_jobs(clock())
        if n:
            log.info("expiry sweep: %d job(s) passed their deadline", n)
        return n

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.sweep_once()
            except Exception:  # keep sweeping; the next poll also sweeps lazily
                log.exception("expiry sweep failed")
