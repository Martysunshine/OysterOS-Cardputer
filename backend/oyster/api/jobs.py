"""Device job delivery, lifecycle reports and idempotent results."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from oyster.api.common import Device, DeviceBody, parse_model, schema_failure, wire_job
from oyster.api.errors import ApiError
from oyster.auth import Principal
from oyster.evidence.validate import SchemaValidationError, parse_job_result
from oyster.store import NotFound

router = APIRouter()


def _owned(store, p: Principal, job_id: str) -> None:
    """404 for unknown ids and ids bound to another device, before the body is examined."""
    if store.get_job(job_id).job.device_id != p.device_id:
        raise NotFound(job_id)


class StatusReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["awaiting_local_confirmation", "running"]


@router.get("/v1/device/jobs")
def poll_jobs(request: Request, p: Device) -> dict:
    """At most one job per poll; delivery moves it queued -> delivered (expired jobs are swept first)."""
    job = request.app.state.store.next_job_for_device(p.device_id)
    return {"jobs": [wire_job(job)] if job is not None else []}


@router.post("/v1/jobs/{job_id}/status")
def report_status(job_id: str, request: Request, p: Device, body: DeviceBody) -> dict:
    """Forward-only lifecycle report (409 otherwise)."""
    store = request.app.state.store
    _owned(store, p, job_id)
    req = parse_model(StatusReport, body)
    return {"status": store.set_job_status(p.device_id, job_id, req.status).status}


@router.post("/v1/jobs/{job_id}/result")
def submit_result(job_id: str, request: Request, p: Device, body: DeviceBody) -> dict:
    """Idempotent by idempotency_key; 409 events_missing until every cited event is uploaded."""
    store = request.app.state.store
    _owned(store, p, job_id)
    try:
        result = parse_job_result(body)
    except SchemaValidationError as e:
        raise schema_failure(e) from None
    if result.job_id != job_id:
        raise ApiError(422, "validation_failed", "job_id in the body does not match the URL")
    rec, duplicate = store.submit_job_result(p.device_id, result)
    return {"status": rec.status, "duplicate": duplicate}
