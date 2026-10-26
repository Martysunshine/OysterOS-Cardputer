"""Run worker: executes AI runs in background threads.

main.py creates one RunWorker with the shared Store, calls start() at startup, submit()
after POST /assistant/messages creates a run, and stop() at shutdown. With no usable provider every
submitted run ends `ai_unavailable` immediately; nothing is ever fabricated.
"""

from __future__ import annotations

import logging
import queue
import threading

from oyster.ai.orchestrator import Orchestrator
from oyster.ai.provider import Provider
from oyster.settings import Settings
from oyster.store import RunRec, Store

log = logging.getLogger("oyster.ai")

OPENAI_COMPATIBLE_PROVIDERS = ("groq", "openai_compatible", "openai", "openrouter")


def build_provider(settings: Settings) -> Provider | None:
    """Provider from settings, or None when AI_PROVIDER/AI_MODEL/AI_API_KEY are not usable.

    AI_PROVIDER=fake returns the FakeProvider (simulation only, is_fake=true). The model id comes only
    from AI_MODEL; there is no fallback model and no silent switch to another provider.
    """
    kind = (settings.ai_provider or "").strip().lower()
    if kind == "fake":
        from oyster.ai.fake import FakeProvider

        return FakeProvider()
    if kind not in OPENAI_COMPATIBLE_PROVIDERS:
        log.warning("AI provider %r is not supported; AI runs will report ai_unavailable", kind)
        return None
    if not settings.ai_model or not settings.ai_api_key:
        log.warning("AI_MODEL or AI_API_KEY not set; AI runs will report ai_unavailable")
        return None
    from oyster.ai.openai_compat import OpenAICompatProvider

    try:
        return OpenAICompatProvider(
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            name=kind,
            timeout_s=settings.ai_timeout_s,
            max_output_tokens=settings.ai_max_output_tokens,
        )
    except ValueError as e:
        log.warning("AI provider configuration unusable: %s", e)
        return None


class RunWorker:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        provider: Provider | None,
        *,
        workers: int = 1,
        poll_interval_s: float = 0.5,
    ):
        self.store = store
        self.settings = settings
        self.provider = provider
        self.workers = max(1, workers)
        self.poll_interval_s = poll_interval_s
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._inflight: set[str] = set()

    def _orchestrator(self) -> Orchestrator:
        return Orchestrator(
            self.store,
            self.settings,
            self.provider,
            stop_event=self._stop,
            poll_interval_s=self.poll_interval_s,
            max_context_events=self.settings.ai_max_context_events,
        )

    def start(self) -> None:
        """Start the background thread(s). Idempotent."""
        with self._lock:
            if any(t.is_alive() for t in self._threads):
                return
            self._stop.clear()
            self._threads = [
                threading.Thread(target=self._loop, name=f"oyster-run-worker-{i}", daemon=True)
                for i in range(self.workers)
            ]
            for t in self._threads:
                t.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop the threads; an active run ends `failed` ("backend stopped") at its next checkpoint."""
        with self._lock:
            threads = list(self._threads)
            self._stop.set()
            for _ in threads:
                self._queue.put(None)
        for t in threads:
            t.join(timeout_s)
        with self._lock:
            self._threads = [t for t in threads if t.is_alive()]

    def submit(self, run_id: str) -> None:
        """Queue a run. Without a provider the run is finalized as ai_unavailable right away."""
        if self.provider is None:
            self.run_sync(run_id)
            return
        with self._lock:
            if run_id in self._inflight:
                return
            self._inflight.add(run_id)
        self._queue.put(run_id)

    def run_sync(self, run_id: str) -> RunRec:
        """Execute one run in the calling thread (CLI/tests)."""
        return self._orchestrator().execute(run_id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            run_id = self._queue.get()
            if run_id is None:
                break
            try:
                self._orchestrator().execute(run_id)
            except Exception:  # noqa: BLE001 - the worker thread must survive any single run
                log.exception("run %s: worker error", run_id)
            finally:
                with self._lock:
                    self._inflight.discard(run_id)
