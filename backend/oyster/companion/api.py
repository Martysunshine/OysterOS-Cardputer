"""Companion routes (Issue #3). See docs/COMPANION_API.md.

Device routes (device bearer token; also served on the LAN listener):
  GET  /v1/hosts                  hosts the calling device may see
  GET  /v1/hosts/{id}/health      allowlisted read-only metrics of one host
  POST /v1/pair                   redeem a pairing code (unauthenticated, throttled)
Owner routes (owner token, loopback listener only; absent from the device app):
  POST/GET /v1/admin/hosts, DELETE /v1/admin/hosts/{id}   enrol / list / delete hosts
  POST /v1/admin/pairing                                  create a pairing code
  GET  /v1/admin/audit                                    read the audit log
`GET /v1/health` already exists in oyster.api.system and is not redefined here.
Errors use the shared {"error", "detail"} body; an unreachable host adds "state" and "host_id".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from oyster.api.common import AdminBody, Device, DeviceBody, Owner, parse_model
from oyster.api.errors import ApiError
from oyster.auth import Principal
from oyster.companion.adapters import AdapterError, HostTimeout
from oyster.companion.models import (
    AuditPage,
    HealthReport,
    HostAdminList,
    HostEnrol,
    HostEnrolled,
    HostList,
    HostRecord,
    HostRemoved,
    HostSummary,
    HostUnavailable,
    PairingCreate,
    PairingCreated,
    PairingRedeem,
    PairingRedeemed,
)
from oyster.companion.pairing import DeviceExists, PairingRejected, PairingUnavailable
from oyster.companion.registry import build_report
from oyster.store import NotFound

if TYPE_CHECKING:
    from oyster.companion.mount import Companion

log = logging.getLogger("oyster.companion")

device_router = APIRouter(tags=["companion"])
admin_router = APIRouter(tags=["companion"])

_NO_STORE = {"Cache-Control": "no-store"}


def companion(request: Request) -> Companion:
    return request.app.state.companion


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _actor(who: Principal) -> str:
    return "owner" if who.is_owner else f"device:{who.device_id}"


# --- device routes -----------------------------------------------------------------------------------------
@device_router.get("/v1/hosts", response_model=HostList)
def list_hosts(request: Request, who: Device) -> HostList:
    hosts = companion(request).registry.visible_to(who.device_id or "")
    return HostList(hosts=[HostSummary(host_id=h.host_id, display_name=h.display_name) for h in hosts])


@device_router.get(
    "/v1/hosts/{host_id}/health",
    response_model=HealthReport,
    response_model_exclude_none=True,
    responses={502: {"model": HostUnavailable, "description": "the host did not deliver a reading"}},
)
def host_health(request: Request, who: Device, host_id: str):
    c = companion(request)
    actor = _actor(who)
    try:
        host = c.registry.get_for_device(host_id, who.device_id or "")
    except NotFound:
        c.audit.record(actor, "health_read", host_id, "denied", "unknown or not shared")
        raise
    try:
        # only allowlisted names are ever requested from the adapter
        reading = c.read(c.adapter_for(host), frozenset(host.metrics))
        report = build_report(host, reading, c.clock())
    except AdapterError as e:
        state = "timeout" if isinstance(e, HostTimeout) else "unreachable"
        c.audit.record(actor, "health_read", host.host_id, state, "adapter failure")
        detail = "host did not answer in time" if state == "timeout" else "host could not be reached"
        body = HostUnavailable(detail=detail, state=state, host_id=host.host_id)
        return JSONResponse(body.model_dump(), status_code=502, headers=_NO_STORE)
    c.audit.record(actor, "health_read", host.host_id, report.state, f"missing={len(report.missing)}")
    return report


@device_router.post("/v1/pair", response_model=PairingRedeemed)
def redeem_pairing(request: Request, body: DeviceBody):
    c = companion(request)
    client = _client(request)
    wait = c.pair_throttle.retry_after(client)
    if wait is not None:
        detail = f"too many pairing attempts; retry in {wait} s"
        raise ApiError(429, "rate_limited", detail, {"Retry-After": str(wait)})
    c.pair_throttle.record_failure(client)  # every attempt counts, successful or not
    req = parse_model(PairingRedeem, body)
    try:
        result = c.pairing.redeem(req, request.app.state.store, client)
    except PairingRejected:
        raise ApiError(401, "unauthorized", "invalid or expired pairing code") from None
    except DeviceExists:
        raise ApiError(409, "conflict", "device id is already enrolled") from None
    except PairingUnavailable:
        raise ApiError(503, "pairing_unavailable", "pairing is not available") from None
    return JSONResponse(result.model_dump(), headers=_NO_STORE)


# --- owner routes ------------------------------------------------------------------------------------------
@admin_router.post("/v1/admin/hosts", response_model=HostEnrolled)
def enrol_host(request: Request, _: Owner, body: AdminBody) -> HostEnrolled:
    """Enrol a host or replace an enrolled one (same id). The allowlist is the whole contract."""
    c = companion(request)
    req = parse_model(HostEnrol, body)
    probe = HostRecord(**req.model_dump(), created_at=0.0, updated_at=0.0)
    try:
        info = c.adapter_for(probe).describe()
    except Exception:
        raise ApiError(422, "validation_failed", "no adapter available for this host") from None
    if not info.read_only or not set(req.metrics) <= set(info.supported_metrics):
        raise ApiError(422, "validation_failed", "the adapter cannot provide the requested metrics read-only")
    rec, created = c.registry.enrol(req)
    what = "created" if created else "replaced"
    detail = f"{what} metrics={','.join(rec.metrics)} devices={len(rec.device_ids)}"
    c.audit.record("owner", "host_enrolled", rec.host_id, "ok", detail)
    return HostEnrolled(host=rec, created=created)


@admin_router.get("/v1/admin/hosts", response_model=HostAdminList)
def admin_list_hosts(request: Request, _: Owner) -> HostAdminList:
    return HostAdminList(hosts=companion(request).registry.all_hosts())


@admin_router.delete("/v1/admin/hosts/{host_id}", response_model=HostRemoved)
def delete_host(request: Request, _: Owner, host_id: str) -> HostRemoved:
    c = companion(request)
    c.registry.delete(host_id)
    c.audit.record("owner", "host_deleted", host_id, "ok")
    return HostRemoved(deleted=host_id)


@admin_router.post("/v1/admin/pairing", response_model=PairingCreated)
def create_pairing(request: Request, _: Owner, body: AdminBody):
    """Returns the code exactly once. A new code burns the previous one."""
    req = parse_model(PairingCreate, body)
    created = companion(request).pairing.create(req)
    return JSONResponse(created.model_dump(), headers=_NO_STORE)


@admin_router.get("/v1/admin/audit", response_model=AuditPage)
def read_audit(
    request: Request,
    _: Owner,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, ge=1),
) -> AuditPage:
    entries = companion(request).audit.entries(limit, before_id)
    return AuditPage(entries=entries, next_before_id=entries[-1].id if len(entries) == limit else None)
