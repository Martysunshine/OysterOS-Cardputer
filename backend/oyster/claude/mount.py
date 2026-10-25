"""Wiring: `install(app)` adds the Claude Code link to a FastAPI app.

    from oyster.claude.mount import install
    install(app)            # after the app's error handlers are installed

- Routes: the device router on every app; the owner router (the hook's long poll, the status report, the
  audit read) only when `app.state.audience == "loopback"`, so the LAN-facing listener never serves them.
- State: one `ClaudeRuntime` (claude.db, service, rate limiters) per data directory per process, so the
  loopback and device apps of `oyster serve` share it. It is started and stopped with the app lifespan
  (reference counted). `oyster serve` runs its apps with the lifespan off; call `app.state.claude.stop()`
  at shutdown to end waiting requests promptly (without it a stopping server waits for each open long poll
  to reach its own deadline).
- Idempotent: installing twice on one app changes nothing.
- Restart safety: opening the runtime ends the approvals a previous process left pending, because nobody
  is waiting for their answers any more.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from oyster.claude.api import device_router, owner_router
from oyster.claude.service import ClaudeConfig, ClaudeService
from oyster.claude.store import DB_NAME, ClaudeStore
from oyster.deck.jobs import RateLimiter

log = logging.getLogger("oyster.claude")


class ClaudeRuntime:
    """The link's long-lived objects. Construction opens claude.db and ends approvals left pending."""

    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        config: ClaudeConfig | None = None,
        new_id: Callable[[], str] | None = None,
        new_nonce: Callable[[], str] | None = None,
        sleep=None,
        synchronous: str = "FULL",
    ):
        cfg = config or ClaudeConfig()
        self.data_dir = Path(data_dir)
        self.store = ClaudeStore(self.data_dir / DB_NAME, synchronous=synchronous)
        extra = {"sleep": sleep} if sleep is not None else {}
        self.service = ClaudeService(
            self.store, clock=clock, config=cfg, new_id=new_id, new_nonce=new_nonce, **extra
        )
        self.create_limiter = RateLimiter(cfg.creates_per_minute, 60.0, clock)
        self.decide_limiter = RateLimiter(cfg.decides_per_minute, 60.0, clock)
        self.list_limiter = RateLimiter(cfg.lists_per_minute, 60.0, clock)
        self.status_limiter = RateLimiter(cfg.status_per_minute, 60.0, clock)
        self.recovered = self.service.recover_after_restart()
        if self.recovered:
            log.warning(
                "claude link startup: %d pending approval(s) from a previous process ended", self.recovered
            )
        self._users = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._users += 1

    def stop(self) -> None:
        """End every pending approval so waiting requests return at once (idempotent). Calling it without a
        matching start() is the shutdown path of `oyster serve`, which runs without a lifespan."""
        with self._lock:
            self._users = max(0, self._users - 1)
            if self._users:
                return
        self.service.close_pending("backend_stopping")

    def close(self) -> None:
        self.store.close()


_shared: dict[Path, ClaudeRuntime] = {}
_shared_lock = threading.Lock()


def shared_runtime(data_dir: Path) -> ClaudeRuntime:
    """One runtime per data directory in this process (the default for `install`)."""
    key = Path(data_dir).resolve()
    with _shared_lock:
        rt = _shared.get(key)
        if rt is None:
            rt = _shared[key] = ClaudeRuntime(key)
        return rt


def adopt(runtime: ClaudeRuntime) -> ClaudeRuntime:
    """Make `runtime` the shared one for its data directory (tests and embedding inject clocks this way,
    before `create_app` installs the link). Replaces and closes a previous one."""
    key = runtime.data_dir.resolve()
    with _shared_lock:
        old = _shared.get(key)
        _shared[key] = runtime
    if old is not None and old is not runtime:
        old.close()
    return runtime


def forget(data_dir: Path) -> None:
    """Drop (and close) the shared runtime of a data directory."""
    with _shared_lock:
        rt = _shared.pop(Path(data_dir).resolve(), None)
    if rt is not None:
        rt.close()


def install(app: FastAPI, runtime: ClaudeRuntime | None = None) -> ClaudeRuntime:
    """Register the Claude Code link on `app` (idempotent) and return its runtime."""
    existing = getattr(app.state, "claude", None)
    if existing is not None:
        return existing
    rt = runtime or shared_runtime(app.state.settings.data_dir)
    app.state.claude = rt
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
