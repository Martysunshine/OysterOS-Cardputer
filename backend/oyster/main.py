"""FastAPI application factory.

create_app(settings, store=None, worker=None, *, audience="loopback", runtime=None,
           companion_options=None, deck_runtime=None):
  - store: any oyster.store.Store; defaults to SqliteStore at settings.db_path.
  - worker: an oyster.ai.runner.RunWorker (start/stop/submit). When None, AI runs end ai_unavailable.
  - audience: "loopback" serves every route (owner/admin + device routes) and accepts the owner token; it
    is for the 127.0.0.1 listener and in-process tests. "device" serves ONLY the device routes (health,
    time, sessions, events, assistant, runs, device/jobs, jobs status/result, report) and answers 403 to
    the owner token: it is the only app that may face the LAN (ADR-004, ADR-003).
  - runtime: shared background services (worker, sweeper); `oyster serve` passes one Runtime to both apps.
  - companion_options: keyword arguments for oyster.companion.mount.install (clock, rng, adapter factory);
    for tests and embedding. Production uses the defaults.
  - deck_runtime: an oyster.deck.mount.DeckRuntime (executor, clock, limits) for tests and embedding;
    by default one runtime per data directory is shared by every app of the process.
Creating an app reconciles state left by a previous process once per Runtime (interrupted runs fail).
The app lifespan starts/stops the Runtime (idempotent). `oyster serve` runs two apps behind two uvicorn
listeners and drives the same Runtime itself (see oyster/cli.py).
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI

from oyster.api import admin, errors, jobs, runs, sessions, system
from oyster.auth import AuthThrottle
from oyster.claude.mount import install as install_claude
from oyster.companion.mount import install as install_companion
from oyster.deck.mount import DeckRuntime
from oyster.deck.mount import install as install_deck
from oyster.home.mount import install as install_home
from oyster.jobs.sweeper import ExpirySweeper
from oyster.settings import Settings
from oyster.store import Store

log = logging.getLogger("oyster")


class Runtime:
    """Background services shared by every listener: the AI run worker and the expiry sweeper."""

    def __init__(self, store: Store, worker=None, sweep_interval_s: float = 5.0):
        self.store = store
        self.worker = worker
        self.sweeper = ExpirySweeper(store, sweep_interval_s)
        self._lock = threading.Lock()
        self._users = 0
        self._reconciled = False

    def reconcile(self) -> dict | None:
        """Once per Runtime: after a crash or restart fail the runs left active and end their jobs that
        cannot have started, so nothing stays actionable. Returns the counts (None when already done)."""
        with self._lock:
            if self._reconciled:
                return None
            self._reconciled = True
        fn = getattr(self.store, "reconcile_startup", None)
        if fn is None:
            return None
        counts = fn()
        if counts and any(counts.values()):
            log.warning(
                "startup reconciliation: %d interrupted run(s) failed, %d job(s) cancelled",
                counts["runs_failed"],
                counts["jobs_cancelled"],
            )
        return counts

    def start(self) -> None:
        self.reconcile()
        with self._lock:
            self._users += 1
            if self._users > 1:
                return
            self.sweeper.start()
            if self.worker is not None:
                self.worker.start()

    def stop(self) -> None:
        with self._lock:
            if self._users == 0:
                return
            self._users -= 1
            if self._users:
                return
            if self.worker is not None:
                self.worker.stop()
            self.sweeper.stop()


def create_app(
    settings: Settings,
    store: Store | None = None,
    worker=None,
    *,
    audience: Literal["loopback", "device"] = "loopback",
    runtime: Runtime | None = None,
    companion_options: dict | None = None,
    deck_runtime: DeckRuntime | None = None,
) -> FastAPI:
    if audience not in ("loopback", "device"):
        raise ValueError(f"unknown audience {audience!r}")
    if store is None:
        from oyster.db.sqlite_store import SqliteStore

        store = SqliteStore(settings.db_path)
    if runtime is None:
        runtime = Runtime(store, worker)
    runtime.reconcile()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    # No interactive docs: the HTTPS listener faces the LAN; docs/API.md is the reference.
    app = FastAPI(
        title="Oyster Pocket backend",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.worker = worker
    app.state.runtime = runtime
    app.state.audience = audience
    app.state.auth_throttle = AuthThrottle(getattr(store, "now", None) or time.time)
    errors.install(app)
    routers = [system.router, sessions.router, runs.router, jobs.router]
    if audience == "loopback":
        routers.insert(1, admin.router)
    for r in routers:
        app.include_router(r)
    # hosts, pairing and audit: device routes, plus owner routes on the loopback app
    install_companion(app, **(companion_options or {}))
    # Control Deck (allowlisted jobs with local confirmation): same audience split
    install_deck(app, deck_runtime)
    # Smart Home (Home Assistant behind an allowlist): same audience split
    install_home(app)
    # Claude Code link (yes/no approvals from the device, usage line): same audience split
    install_claude(app)
    if audience == "loopback" and not settings.admin_token:
        log.warning("OYSTER_ADMIN_TOKEN is empty: owner endpoints will refuse every request")
    return app
