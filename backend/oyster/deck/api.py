"""Control Deck routes (Issue #4). Auth semantics follow the rest of the API: missing or invalid token 401,
wrong role 403, ids of another device 404, unknown input 422.

Device routes (`device_router`, served on both listeners):
  GET  /v1/deck/profiles                          profiles bound to the caller (owner: all, full detail)
  POST /v1/deck/profiles/{id}/slots/{n}/run       n in 1..9; header Idempotency-Key (8..80 chars) required
  POST /v1/deck/jobs/{id}/confirm                 {job_id, nonce, approved} from the device that owns the job
  POST /v1/deck/jobs/{id}/cancel
  GET  /v1/deck/jobs/{id}
  GET  /v1/deck/history                           caller's own entries (owner: all), newest first, <= 50/page
Owner routes (`owner_router`, loopback listener only):
  PUT|GET|DELETE /v1/deck/profiles/{id}, GET /v1/deck/jobs, GET /v1/deck/audit

The run request body is optional and never read: a confirmation, approval or impact flag in it has no effect.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from oyster.api.common import AdminBody, AnyPrincipal, Device, DeviceBody, Owner, parse_model
from oyster.api.errors import ApiError
from oyster.deck import audit
from oyster.deck.jobs import KEY_PATTERN, STATES, DeckError, job_wire
from oyster.deck.profiles import ProfileInvalid, load_profile

RUN_BODY_LIMIT = 1024
PROFILE_PAGE_MAX = 5  # worst-case device profile summary is ~0.7 KiB; docs/API.md keeps responses under 4 KiB
_KEY = re.compile(KEY_PATTERN)

device_router = APIRouter()
owner_router = APIRouter()


def _rt(request: Request):
    return request.app.state.deck


def optional_json_body(limit: int):
    """Bounded body that may be empty; when present it must be a JSON object. Its content is not used."""

    async def _read(request: Request) -> dict:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ApiError(413, "payload_too_large", f"body exceeds {limit} bytes")
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > limit:
                raise ApiError(413, "payload_too_large", f"body exceeds {limit} bytes")
        if not buf.strip():
            return {}
        try:
            data = json.loads(buf.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(422, "validation_failed", "body is not valid UTF-8 JSON") from None
        if not isinstance(data, dict):
            raise ApiError(422, "validation_failed", "body must be a JSON object")
        return data

    return _read


RunBody = Annotated[dict, Depends(optional_json_body(RUN_BODY_LIMIT))]


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=64)
    nonce: str = Field(min_length=8, max_length=128)
    approved: StrictBool


def _throttle(limiter, device_id: str | None) -> None:
    wait = limiter.check(device_id or "?")
    if wait is not None:
        detail = f"too many requests; retry in {wait} s"
        raise ApiError(429, "rate_limited", detail, {"Retry-After": str(wait)})


def _page(items: list, next_before: int | None, key: str) -> dict:
    return {key: items, "next_before": next_before}


# --- device routes ---------------------------------------------------------------------------------


@device_router.get("/v1/deck/profiles")
def list_profiles(
    request: Request,
    p: AnyPrincipal,
    limit: Annotated[int | None, Query(ge=1, le=32)] = None,
    after: Annotated[str | None, Query(max_length=40)] = None,
) -> dict:
    service = _rt(request).service
    if p.is_owner:
        profiles, nxt = service.list_profiles(None, after, limit or 32)
        return {"profiles": [pr.model_dump(mode="json") for pr in profiles], "next_after": nxt}
    profiles, nxt = service.list_profiles(p.device_id, after, min(limit or 4, PROFILE_PAGE_MAX))
    return {"profiles": [pr.summary() for pr in profiles], "next_after": nxt}


@device_router.post("/v1/deck/profiles/{profile_id}/slots/{n}/run")
def run_slot(profile_id: str, n: int, request: Request, p: Device, body: RunBody) -> dict:
    """Admits a run. Impactful slots answer `awaiting_confirmation` with the nonce for the confirm call;
    the others are queued for the runner. A repeated Idempotency-Key returns the stored job."""
    rt = _rt(request)
    if not 1 <= n <= 9:
        raise ApiError(422, "validation_failed", "slot must be 1..9")
    _throttle(rt.run_limiter, p.device_id)
    key = request.headers.get("idempotency-key", "")
    if not _KEY.match(key):
        raise ApiError(422, "validation_failed", "Idempotency-Key header (8..80 characters) is required")
    job, duplicate = rt.service.run_slot(p.device_id, profile_id, n, key)
    return {**job_wire(job, view="device"), "duplicate": duplicate}


@device_router.post("/v1/deck/jobs/{job_id}/confirm")
def confirm_job(job_id: str, request: Request, p: Device, body: DeviceBody) -> dict:
    rt = _rt(request)
    _throttle(rt.confirm_limiter, p.device_id)
    req = parse_model(ConfirmRequest, body)
    job = rt.service.confirm(p.device_id, job_id, req.job_id, req.nonce, req.approved)
    return job_wire(job, view="device")


@device_router.post("/v1/deck/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, p: Device) -> dict:
    rt = _rt(request)
    _throttle(rt.confirm_limiter, p.device_id)
    return job_wire(rt.service.cancel(p.device_id, job_id), view="device")


@device_router.get("/v1/deck/jobs/{job_id}")
def get_job(job_id: str, request: Request, p: AnyPrincipal) -> dict:
    job = _rt(request).service.get_job(job_id, None if p.is_owner else p.device_id)
    return job_wire(job, view="owner" if p.is_owner else "device")


def _history(
    request: Request, p, device_id: str | None, job_id: str | None, before: int | None, limit: int | None
) -> dict:
    if not p.is_owner:
        if device_id not in (None, p.device_id):
            raise DeckError(404, "not_found", "unknown id")
        device_id = p.device_id
    rows, nxt = _rt(request).service.history(device_id=device_id, job_id=job_id, before=before, limit=limit)
    return _page([audit.entry_wire(e, owner=p.is_owner) for e in rows], nxt, "entries")


@device_router.get("/v1/deck/history")
def history(
    request: Request,
    p: AnyPrincipal,
    limit: Annotated[int | None, Query(ge=1, le=audit.MAX_PAGE)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
    device_id: Annotated[str | None, Query(max_length=40)] = None,
    job_id: Annotated[str | None, Query(max_length=64)] = None,
) -> dict:
    return _history(request, p, device_id, job_id, before, limit)


# --- owner routes ----------------------------------------------------------------------------------


def _profile_or_422(body: Any):
    try:
        return load_profile(body)
    except ProfileInvalid as e:
        code = "action_not_allowed" if e.reason == "action_not_allowed" else "validation_failed"
        raise ApiError(422, code, e.detail) from None


@owner_router.put("/v1/deck/profiles/{profile_id}")
def put_profile(profile_id: str, request: Request, _: Owner, body: AdminBody) -> dict:
    """Define or replace a profile. Unknown action types and unlisted hosts or services are 422."""
    profile = _profile_or_422(body)
    if profile.profile_id != profile_id:
        raise ApiError(422, "validation_failed", "profile_id in the body does not match the URL")
    result = _rt(request).service.put_profile(profile)
    return {"result": result, "profile": profile.model_dump(mode="json")}


@owner_router.get("/v1/deck/profiles/{profile_id}")
def get_profile(profile_id: str, request: Request, _: Owner) -> dict:
    return _rt(request).service.get_profile(profile_id).model_dump(mode="json")


@owner_router.delete("/v1/deck/profiles/{profile_id}")
def delete_profile(profile_id: str, request: Request, _: Owner) -> dict:
    _rt(request).service.delete_profile(profile_id)
    return {"deleted": profile_id}


@owner_router.get("/v1/deck/jobs")
def list_jobs(
    request: Request,
    _: Owner,
    limit: Annotated[int | None, Query(ge=1, le=audit.MAX_PAGE)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
    device_id: Annotated[str | None, Query(max_length=40)] = None,
    state: Annotated[str | None, Query(max_length=40)] = None,
) -> dict:
    if state is not None and state not in STATES:
        raise ApiError(422, "validation_failed", "unknown state")
    rows, nxt = _rt(request).service.list_jobs(device_id=device_id, state=state, before=before, limit=limit)
    return _page([{**job_wire(j, view="owner"), "seq": j.seq} for j in rows], nxt, "jobs")


@owner_router.get("/v1/deck/audit")
def read_audit(
    request: Request,
    _: Owner,
    limit: Annotated[int | None, Query(ge=1, le=audit.MAX_PAGE)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
    device_id: Annotated[str | None, Query(max_length=40)] = None,
    job_id: Annotated[str | None, Query(max_length=64)] = None,
) -> dict:
    rows, nxt = _rt(request).service.history(device_id=device_id, job_id=job_id, before=before, limit=limit)
    return _page([audit.entry_wire(e, owner=True) for e in rows], nxt, "entries")
