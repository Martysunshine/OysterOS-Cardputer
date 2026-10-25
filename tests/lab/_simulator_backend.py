"""In-process fake backend for simulator tests (httpx.MockTransport).

Implements only the contract the device uses (docs/API.md): session registration, job
delivery (at most one per poll), event upload (idempotent by event_id), job status transitions, and
idempotent result submission (keyed by idempotency_key). It is an independent test double; it does not
import backend code.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from oyster_lab.simulator.scenarios import Scenario


class FakeBackend:
    def __init__(self, *, device_id: str = "sim-cardputer-01"):
        self.device_id = device_id
        self.sessions: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.job_queue: list[dict[str, Any]] = []
        self.delivered: list[str] = []
        self.status_log: list[tuple[str, str]] = []
        self.result_submissions: list[dict[str, Any]] = []
        self.result_by_job: dict[str, dict[str, Any]] = {}
        self.seen_keys: set[str] = set()
        self.result_fail_remaining = 0
        self.last_session: str | None = None
        self.auto_stamp_session = False  # True: jobs get the registered session id at delivery
        # (job_id, status) pairs the backend refuses with 409 (job expired/cancelled backend-side), or that
        # fail at the transport level
        self.reject_status: set[tuple[str, str]] = set()
        self.status_transport_error: set[tuple[str, str]] = set()

    # --- queue helpers ---
    def enqueue(self, job: dict[str, Any]) -> None:
        self.job_queue.append(job)

    def enqueue_plan(self, scenario: Scenario, session_id: str) -> list[str]:
        ids = []
        for raw in scenario.plan:
            job = dict(raw)
            job["schema_version"] = "1.0"
            job["session_id"] = session_id
            job.setdefault("device_id", self.device_id)
            self.enqueue(job)
            ids.append(job["job_id"])
        return ids

    # --- transport ---
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle), base_url="http://backend.test")

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        if method == "POST" and path == "/v1/sessions":
            sid = body.get("session_id") or "sess-fake-001"
            self.sessions[sid] = body
            self.last_session = sid
            return httpx.Response(200, json={"session_id": sid, "created": True})

        if method == "GET" and path == "/v1/device/jobs":
            if self.job_queue:
                job = self.job_queue.pop(0)
                if self.auto_stamp_session and self.last_session:
                    job["session_id"] = self.last_session
                    job["device_id"] = self.device_id
                self.delivered.append(job["job_id"])
                return httpx.Response(200, json={"jobs": [job]})
            return httpx.Response(200, json={"jobs": []})

        if method == "POST" and path.endswith("/events") and "/sessions/" in path:
            accepted = 0
            duplicates = 0
            for e in body.get("events", []):
                eid = e["event_id"]
                if eid in self.events:
                    duplicates += 1
                else:
                    self.events[eid] = e
                    accepted += 1
            acked = max((e["sequence"] for e in self.events.values()), default=-1)
            return httpx.Response(
                200, json={"accepted": accepted, "duplicates": duplicates, "acked_through": acked}
            )

        if method == "POST" and "/jobs/" in path and path.endswith("/status"):
            job_id = path.split("/jobs/")[1].split("/status")[0]
            status = body.get("status", "")
            self.status_log.append((job_id, status))
            if (job_id, status) in self.status_transport_error:
                raise httpx.ConnectError("simulated transport failure", request=request)
            if (job_id, status) in self.reject_status:
                return httpx.Response(409, json={"error": "conflict", "detail": "job is expired"})
            return httpx.Response(200, json={"status": status})

        if method == "POST" and "/jobs/" in path and path.endswith("/result"):
            if self.result_fail_remaining > 0:
                self.result_fail_remaining -= 1
                raise httpx.ConnectError("simulated transport failure", request=request)
            job_id = path.split("/jobs/")[1].split("/result")[0]
            self.result_submissions.append(body)
            key = body.get("idempotency_key", "")
            # Events referenced by a result must already be uploaded.
            missing = [eid for eid in body.get("event_ids", []) if eid not in self.events]
            if missing:
                return httpx.Response(409, json={"error": "events_missing", "detail": ",".join(missing)})
            if key in self.seen_keys:
                return httpx.Response(200, json={"status": "stored", "duplicate": True})
            self.seen_keys.add(key)
            self.result_by_job[job_id] = body
            return httpx.Response(200, json={"status": "stored", "duplicate": False})

        return httpx.Response(404, json={"error": "not_found", "detail": path})
