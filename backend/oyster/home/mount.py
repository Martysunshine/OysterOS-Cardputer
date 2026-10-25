"""Wiring: `install(app)` adds the Smart Home routes to a FastAPI app.

    from oyster.home.mount import install
    install(app)            # after the app's error handlers are installed

- Routes: the device router on every app; the owner router only when `app.state.audience == "loopback"`
  (the LAN-facing listener never serves owner functions, ADR-004).
- State: one `HomeRuntime` (configuration, allowlist cache, audit file, rate limiters, the Home Assistant
  client) per data directory per process, so the loopback and device apps of `oyster serve` share it.
  Nothing is read from disk or the network at install time except the environment; the Home Assistant client
  and home.db are created on first use. The configuration is read from the environment once, when the shared
  runtime is created: restart the backend after changing HOME_ASSISTANT_* (the allowlist file is re-read when
  it changes).
- Idempotent: installing twice on one app changes nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from oyster.home.api import device_router, owner_router
from oyster.home.config import HomeConfig
from oyster.home.service import HomeService
from oyster.home.store import DB_NAME, HomeStore

log = logging.getLogger("oyster.home")


class HomeRuntime:
    def __init__(
        self,
        data_dir: Path,
        *,
        config: HomeConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        **limits,
    ):
        self.config = config or HomeConfig.from_env(Path(data_dir))
        self.store = HomeStore(Path(data_dir) / DB_NAME)
        self.service = HomeService(
            self.config, self.store, clock=clock, monotonic=monotonic, transport=transport, **limits
        )
        problem = self.config.problem()
        if problem is None and not self.config.tls:
            log.warning(
                "Home Assistant URL uses plain http: its access token crosses the network unencrypted"
            )
        elif problem not in (None, "no_url"):
            log.warning("Home Assistant settings are incomplete (%s); Smart Home stays off", problem)
        self._users = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._users += 1

    def stop(self) -> None:
        with self._lock:
            self._users = max(0, self._users - 1)
            idle = self._users == 0
        if idle:
            self.service.close()

    def close(self) -> None:
        self.service.close()


_shared: dict[Path, HomeRuntime] = {}
_shared_lock = threading.Lock()


def shared_runtime(data_dir: Path) -> HomeRuntime:
    """One runtime per data directory in this process (the default for `install`)."""
    key = Path(data_dir).resolve()
    with _shared_lock:
        rt = _shared.get(key)
        if rt is None:
            rt = _shared[key] = HomeRuntime(key)
        return rt


def install(app: FastAPI, runtime: HomeRuntime | None = None) -> HomeRuntime:
    """Register the Smart Home routes on `app` (idempotent) and return its runtime."""
    existing = getattr(app.state, "home", None)
    if existing is not None:
        return existing
    rt = runtime or shared_runtime(app.state.settings.data_dir)
    app.state.home = rt
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
