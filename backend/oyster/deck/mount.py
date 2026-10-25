"""Wiring: `install(app)` adds the Control Deck to a FastAPI app.

    from oyster.deck.mount import install
    install(app)            # after the app's error handlers are installed

- Routes: the device router on every app; the owner router only when `app.state.audience == "loopback"`
  (the LAN-facing listener never serves owner functions, ADR-004).
- State: one `DeckRuntime` (deck.db, service, runner, rate limiters) per data directory per process, so the
  loopback and device apps of `oyster serve` share it. It is started and stopped with the app lifespan
  (reference counted); the runner also starts itself on the first submitted job, because `oyster serve` runs
  its apps with the lifespan off. Call `app.state.deck.stop()` at shutdown for a clean worker stop.
- Idempotent: installing twice on one app changes nothing.
- Executor: only the mock executor ships. Nothing is executed on any host; the wire format labels jobs with
  `"executor": "mock"` and the runtime logs a warning at start-up.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from oyster.deck.actions import ActionExecutor, MockExecutor
from oyster.deck.api import device_router, owner_router
from oyster.deck.jobs import DeckConfig, DeckService, RateLimiter
from oyster.deck.runner import DeckRunner
from oyster.deck.store import DB_NAME, DeckStore

log = logging.getLogger("oyster.deck")


class DeckRuntime:
    """The deck's long-lived objects. Construction opens deck.db and settles jobs a previous process left
    behind (`recover_after_restart`: running -> unknown_outcome or failed, queued -> failed, no re-run)."""

    def __init__(
        self,
        data_dir: Path,
        *,
        executor: ActionExecutor | None = None,
        clock: Callable[[], float] = time.time,
        config: DeckConfig | None = None,
        new_id: Callable[[], str] | None = None,
        new_nonce: Callable[[], str] | None = None,
        synchronous: str = "FULL",
        autostart: bool = True,
    ):
        cfg = config or DeckConfig()
        self.executor: ActionExecutor = executor or MockExecutor()
        self.store = DeckStore(Path(data_dir) / DB_NAME, synchronous=synchronous)
        self.service = DeckService(
            self.store,
            clock=clock,
            config=cfg,
            executor_kind=self.executor.kind,
            new_id=new_id,
            new_nonce=new_nonce,
        )
        self.runner = DeckRunner(self.service, self.executor, queue_size=cfg.queue_size, auto_start=autostart)
        self.service.attach_runner(self.runner.submit, self.runner.has_capacity)
        self.run_limiter = RateLimiter(cfg.runs_per_minute, 60.0, clock)
        self.confirm_limiter = RateLimiter(cfg.confirms_per_minute, 60.0, clock)
        self.recovered = self.service.recover_after_restart()
        if any(self.recovered.values()):
            log.warning("deck startup: %s job(s) left by a previous process were settled", self.recovered)
        if self.executor.kind == "mock":
            log.warning("Control Deck uses the mock executor: actions are recorded, nothing is executed")
        self._users = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._users += 1
            if self._users == 1:
                self.runner.start()

    def stop(self) -> None:
        with self._lock:
            self._users = max(0, self._users - 1)
            if self._users == 0:
                self.runner.stop()  # also ends a worker that auto-started without any lifespan

    def close(self) -> None:
        self.runner.stop()
        self.store.close()


_shared: dict[Path, DeckRuntime] = {}
_shared_lock = threading.Lock()


def shared_runtime(data_dir: Path) -> DeckRuntime:
    """One runtime per data directory in this process (the default for `install`)."""
    key = Path(data_dir).resolve()
    with _shared_lock:
        rt = _shared.get(key)
        if rt is None:
            rt = _shared[key] = DeckRuntime(key)
        return rt


def install(app: FastAPI, runtime: DeckRuntime | None = None) -> DeckRuntime:
    """Register the Control Deck on `app` (idempotent) and return its runtime."""
    existing = getattr(app.state, "deck", None)
    if existing is not None:
        return existing
    rt = runtime or shared_runtime(app.state.settings.data_dir)
    app.state.deck = rt
    app.include_router(device_router)
    if getattr(app.state, "audience", "loopback") == "loopback":
        app.include_router(owner_router)

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        rt.start()
        try:
            async with inner(a) as state:
                yield state
        finally:
            rt.stop()

    app.router.lifespan_context = lifespan
    return rt
