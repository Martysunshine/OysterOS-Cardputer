"""Bounded single-worker runner for accepted Control Deck jobs.

One worker thread drains a bounded queue (`submit` raises queue.Full when it is full). For each job it
claims the job through the service (a cancelled, expired or already started job is skipped, so a job can
never run twice), re-validates every stored action against the allowlist, and executes the actions in
order with a per-action timeout. The first failed action stops the job; later steps are `skipped`.

Outcome mapping for one action:
  result ok                          -> succeeded
  result not ok / ActionFailed       -> failed
  timeout, lost result, any other
  executor error                     -> impactful action: the job ends `unknown_outcome` (it may have run);
                                        non-impactful action: the job ends `failed`.
Nothing is retried. The executor is injected (default MockExecutor, which executes nothing on any host).
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from collections.abc import Callable

from oyster.deck.actions import (
    ActionExecutor,
    ActionFailed,
    ActionOutcomeUnknown,
    ActionRejected,
    ActionResult,
    ActionTimeout,
    MockExecutor,
    is_impactful,
    parse_action,
    timeout_for,
)
from oyster.deck.jobs import FAILED, SUCCEEDED, UNKNOWN_OUTCOME, DeckJob, DeckService

log = logging.getLogger("oyster.deck")

_CODE = re.compile(r"^[a-z0-9_]{1,40}$")
_NOT_PRINTABLE = re.compile(r"[^\x20-\x7e]")
MAX_SUMMARY = 120


def _clean_code(code: object, default: str) -> str:
    return code if isinstance(code, str) and _CODE.match(code) else default


def _clean_summary(text: object) -> str:
    return _NOT_PRINTABLE.sub("", text)[:MAX_SUMMARY] if isinstance(text, str) else ""


class DeckRunner:
    def __init__(
        self,
        service: DeckService,
        executor: ActionExecutor | None = None,
        *,
        queue_size: int = 16,
        timeout_of: Callable = timeout_for,  # per-action timeout in seconds (injectable for tests)
        sweep_interval_s: float = 1.0,
        auto_start: bool = False,
    ):
        self.service = service
        self.executor: ActionExecutor = executor or MockExecutor()
        self.timeout_of = timeout_of
        self.sweep_interval_s = sweep_interval_s
        self.auto_start = auto_start  # start the worker on the first submit (no lifespan needed)
        self._q: queue.Queue[str | None] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # --- queue ---
    def submit(self, job_id: str) -> None:
        """Raises queue.Full when the bounded queue is full."""
        self._q.put_nowait(job_id)
        if self.auto_start:
            self.start()

    def has_capacity(self) -> bool:
        return self._q.qsize() < self._q.maxsize

    # --- lifecycle ---
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="oyster-deck-runner", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass  # the loop also checks the stop flag after each job
        thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._q.get(timeout=self.sweep_interval_s)
            except queue.Empty:
                self._sweep()
                continue
            if job_id is None:
                break
            try:
                self.run_one(job_id)
            except Exception:  # the job stays `running` and is closed by its deadline
                log.exception("deck job runner failed")

    def _sweep(self) -> None:
        try:
            self.service.sweep()
        except Exception:
            log.exception("deck deadline sweep failed")

    def run_pending(self) -> int:
        """Run every queued job on the calling thread (tests; do not use while the worker runs)."""
        n = 0
        while True:
            try:
                job_id = self._q.get_nowait()
            except queue.Empty:
                return n
            if job_id is not None:
                self.run_one(job_id)
                n += 1

    # --- execution ---
    def run_one(self, job_id: str) -> DeckJob | None:
        job = self.service.begin(job_id)
        if job is None:
            return None
        steps = list(job.steps)
        final, reason = SUCCEEDED, None
        for i, raw in enumerate(job.actions):
            if final != SUCCEEDED:
                steps[i] = {**steps[i], "outcome": "skipped"}
                continue
            try:
                action = parse_action(raw)  # the allowlist is applied again right before execution
            except ActionRejected:
                steps[i] = {**steps[i], "outcome": "failed", "code": "action_not_allowed"}
                final, reason = FAILED, "action_not_allowed"
                continue
            outcome, code, summary = self._execute(action)
            steps[i] = {**steps[i], "outcome": outcome, "code": code}
            if summary:
                steps[i]["summary"] = summary
            if outcome == "failed":
                final, reason = FAILED, code
            elif outcome == "unknown":
                final, reason = (UNKNOWN_OUTCOME if is_impactful(action) else FAILED), code
        return self.service.finish(job_id, final, steps, reason)

    def _execute(self, action) -> tuple[str, str, str]:
        """(outcome, code, summary) with outcome in succeeded | failed | unknown."""
        holder: dict = {}

        def call() -> None:
            try:
                holder["result"] = self.executor.execute(action)
            except BaseException as e:  # every executor error is classified below
                holder["error"] = e

        worker = threading.Thread(target=call, name="oyster-deck-action", daemon=True)
        worker.start()
        worker.join(self.timeout_of(action))
        if worker.is_alive():
            return "unknown", "timeout", ""  # the call cannot be stopped; its result is ignored
        error = holder.get("error")
        if isinstance(error, ActionFailed):
            return "failed", "failed", ""
        if isinstance(error, ActionTimeout):
            return "unknown", "timeout", ""
        if isinstance(error, ActionOutcomeUnknown):
            return "unknown", "lost_result", ""
        if error is not None:
            return "unknown", "executor_error", ""  # the message is dropped: it may carry host details
        result = holder.get("result")
        if not isinstance(result, ActionResult):
            return "unknown", "executor_error", ""
        if result.ok:
            return "succeeded", _clean_code(result.code, "ok"), _clean_summary(result.summary)
        return "failed", _clean_code(result.code, "failed"), _clean_summary(result.summary)
