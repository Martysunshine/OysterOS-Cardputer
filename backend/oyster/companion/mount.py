"""Mount the companion API on an existing FastAPI app: `install(app)`.

    from oyster.companion.mount import install
    install(app)                 # after create_app(...) built the app

Idempotent (a second call returns the first installation). The device app (`audience="device"`) gets the
device routes only; the loopback app also gets the owner routes, mirroring oyster.main. The companion keeps
its own database (`companion.db` in settings.data_dir); the store is opened on first use and by the
application's startup, and closed at shutdown. Authentication reuses oyster.auth through the same
dependencies the existing routers use, and device enrolment reuses `app.state.store.create_device`.

Host adapters (oyster.companion.adapters): the mock, and the read-only `local` adapter for the machine the
backend runs on. Nothing here talks to a remote host.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from oyster.auth import AuthThrottle
from oyster.companion import api
from oyster.companion.adapters import (
    AdapterFactory,
    AdapterReading,
    HostAdapter,
    HostTimeout,
    HostUnreachable,
    build_adapter,
)
from oyster.companion.audit import AuditLog
from oyster.companion.models import HostRecord
from oyster.companion.pairing import PairingService, Rng
from oyster.companion.registry import HostRegistry
from oyster.companion.store import CompanionStore

log = logging.getLogger("oyster.companion")

ADAPTER_DEADLINE_S = 5.0  # hard cap on one adapter read, whatever the adapter does
ADAPTER_THREADS = 4
# more than this many redemption attempts from one client address within a minute block it for 60 s
PAIR_ATTEMPTS_PER_WINDOW = 10
DB_NAME = "companion.db"


class Companion:
    """Everything the routes need, built from injectable parts (clock, RNG, adapter factory)."""

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        rng: Rng | None = None,
        adapter_factory: AdapterFactory = build_adapter,
        deadline_s: float = ADAPTER_DEADLINE_S,
    ):
        self.db_path = db_path
        self.clock = clock
        self._rng: Rng = rng or secrets.SystemRandom()
        self._adapter_factory = adapter_factory
        self._deadline_s = deadline_s
        self.pair_throttle = AuthThrottle(
            clock, max_failures=PAIR_ATTEMPTS_PER_WINDOW, window_s=60.0, block_s=60.0
        )
        self._lock = threading.RLock()
        self._store: CompanionStore | None = None
        self._audit: AuditLog | None = None
        self._registry: HostRegistry | None = None
        self._pairing: PairingService | None = None
        self._executor: ThreadPoolExecutor | None = None

    # --- lifecycle ---
    def open(self) -> None:
        """Create the store and the services (tables are created idempotently). Safe to call repeatedly."""
        with self._lock:
            if self._store is None:
                self._store = CompanionStore(self.db_path, clock=self.clock)
                self._audit = AuditLog(self._store, self.clock)
                self._registry = HostRegistry(self._store, self.clock)
                self._pairing = PairingService(self._store, self._audit, self.clock, self._rng)

    def close(self) -> None:
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None
            if self._store is not None:
                self._store.close()
                self._store = self._audit = self._registry = self._pairing = None

    @property
    def store(self) -> CompanionStore:
        self.open()
        assert self._store is not None
        return self._store

    @property
    def audit(self) -> AuditLog:
        self.open()
        assert self._audit is not None
        return self._audit

    @property
    def registry(self) -> HostRegistry:
        self.open()
        assert self._registry is not None
        return self._registry

    @property
    def pairing(self) -> PairingService:
        self.open()
        assert self._pairing is not None
        return self._pairing

    # --- adapters ---
    def adapter_for(self, host: HostRecord) -> HostAdapter:
        return self._adapter_factory(host)

    def read(self, adapter: HostAdapter, metrics: frozenset[str]) -> AdapterReading:
        """One adapter read under a hard deadline. Raises HostTimeout / HostUnreachable only; whatever the
        adapter raised is logged by class name and never forwarded."""
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(ADAPTER_THREADS, thread_name_prefix="companion-adapter")
            executor = self._executor
        try:
            future = executor.submit(adapter.health, metrics)
        except RuntimeError:  # executor already shut down
            raise HostUnreachable("companion is shutting down") from None
        try:
            return future.result(timeout=self._deadline_s)
        except TimeoutError:
            future.cancel()
            raise HostTimeout("adapter deadline exceeded") from None
        except (HostTimeout, HostUnreachable):
            raise
        except Exception as e:
            log.warning("host adapter failed (%s)", type(e).__name__)
            raise HostUnreachable("adapter error") from None


def install(
    app: FastAPI,
    *,
    clock: Callable[[], float] | None = None,
    rng: Rng | None = None,
    adapter_factory: AdapterFactory = build_adapter,
    deadline_s: float = ADAPTER_DEADLINE_S,
    db_path: Path | None = None,
) -> Companion:
    """Register the routes and the store's startup/shutdown on `app`. The keyword arguments exist for
    tests and embedding; production use is `install(app)`."""
    existing = getattr(app.state, "companion", None)
    if existing is not None:
        return existing
    store = getattr(app.state, "store", None)
    companion = Companion(
        db_path or Path(app.state.settings.data_dir) / DB_NAME,
        clock=clock or getattr(store, "now", None) or time.time,
        rng=rng,
        adapter_factory=adapter_factory,
        deadline_s=deadline_s,
    )
    app.state.companion = companion
    app.include_router(api.device_router)
    if getattr(app.state, "audience", "loopback") == "loopback":
        app.include_router(api.admin_router)

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        companion.open()
        try:
            async with inner(a) as state:
                yield state
        finally:
            companion.close()

    app.router.lifespan_context = lifespan
    return companion
