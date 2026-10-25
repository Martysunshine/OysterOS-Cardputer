"""Sessions, evidence upload, AI questions and reports."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from oyster.api.common import AnyPrincipal, Device, DeviceBody, parse_model, schema_failure
from oyster.api.errors import ApiError
from oyster.contracts import DEVICE_OPERATIONS
from oyster.evidence.validate import SchemaValidationError, parse_event
from oyster.reports.render import build_report, render_markdown

log = logging.getLogger("oyster.api")
router = APIRouter()

MAX_EVENTS_PER_BATCH = 64
MAX_QUESTION_CHARS = 1000


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = Field(default=None, pattern=r"^sess-[0-9a-zA-Z_-]{1,40}$")
    source_mode: Literal["live", "simulated"]
    capabilities: list[str] = Field(default_factory=list, max_length=len(DEVICE_OPERATIONS))
    room_label: str | None = Field(default=None, max_length=32)


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[Any] = Field(max_length=MAX_EVENTS_PER_BATCH)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


@router.post("/v1/sessions")
def register_session(request: Request, p: Device, body: DeviceBody) -> dict:
    """Idempotent for the same device; an id bound to another device -> 409."""
    req = parse_model(SessionCreate, body)
    unknown = [c for c in req.capabilities if c not in DEVICE_OPERATIONS]
    if unknown:
        raise ApiError(422, "validation_failed", f"unknown capabilities {unknown}")
    if p.simulated and req.source_mode != "simulated":
        raise ApiError(422, "validation_failed", "a simulated device may only open simulated sessions")
    rec, created = request.app.state.store.register_session(
        p.device_id, req.session_id, req.source_mode, req.capabilities, req.room_label
    )
    return {"session_id": rec.session_id, "created": created}


@router.post("/v1/sessions/{session_id}/events")
def upload_events(session_id: str, request: Request, p: Device, body: DeviceBody) -> dict:
    """Each event is validated against event.schema.json; idempotent by event_id."""
    store = request.app.state.store
    store.get_session(session_id, p.device_id)  # 404 before anything about the body is revealed
    batch = parse_model(EventBatch, body)
    events = []
    for i, raw in enumerate(batch.events):
        try:
            ev = parse_event(raw)
        except SchemaValidationError as e:
            raise schema_failure(e, f"events/{i}: ") from None
        if ev.session_id != session_id or ev.device_id != p.device_id:
            raise ApiError(
                422, "validation_failed", f"events/{i}: session_id/device_id must match the URL and the token"
            )
        events.append(ev)
    accepted, duplicates, acked = store.add_events(p.device_id, session_id, events)
    return {"accepted": accepted, "duplicates": duplicates, "acked_through": acked}


def _provider_descriptor(request: Request) -> dict:
    worker = request.app.state.worker
    prov = getattr(worker, "provider", None) if worker is not None else None
    if prov is not None and hasattr(prov, "capabilities"):
        return prov.capabilities()
    s = request.app.state.settings
    return {"name": s.ai_provider or "none", "model": s.ai_model or None, "is_fake": s.provider_is_fake}


@router.post("/v1/sessions/{session_id}/assistant/messages")
def ask(session_id: str, request: Request, p: AnyPrincipal, body: DeviceBody) -> dict:
    """Start an asynchronous AI run. Without a run worker the run ends ai_unavailable (no made-up answer).
    Admission limits (429): one active run per session, four queued runs overall, 30 runs per device-hour."""
    store = request.app.state.store
    store.get_session(session_id, p.scope)
    q = parse_model(Question, body)
    run = store.create_run(
        session_id, q.question, _provider_descriptor(request), limits=request.app.state.settings.run_limits
    )
    worker = request.app.state.worker
    if worker is None:
        run.status = "ai_unavailable"
        run.error = (
            "AI run worker not available (oyster.ai package missing or not started); no answer generated."
        )
        store.save_run(run)
    else:
        try:
            worker.submit(run.run_id)
        except Exception as e:  # the run must not stay queued forever
            log.warning("run %s: submit failed: %s", run.run_id, type(e).__name__)
            run.status = "ai_unavailable"
            run.error = f"AI run worker rejected the run ({type(e).__name__})."
            store.save_run(run)
    return {"run_id": run.run_id, "status": store.get_run(run.run_id).status}


@router.get("/v1/sessions/{session_id}/report")
def report(
    session_id: str,
    request: Request,
    p: AnyPrincipal,
    format: Annotated[Literal["md", "json"], Query()] = "md",
):  # noqa: A002 - name fixed by the API
    store = request.app.state.store
    store.get_session(session_id, p.scope)
    rep = build_report(store, session_id)
    if format == "json":
        return rep
    return PlainTextResponse(render_markdown(rep), media_type="text/markdown; charset=utf-8")
