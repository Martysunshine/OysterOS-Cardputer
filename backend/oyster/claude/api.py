"""Claude Code link routes (docs/CLAUDE_CODE_LINK.md). Auth follows the rest of the API: missing or invalid
token 401, wrong role 403, unknown ids 404, state conflicts 409, unknown input 422, rate limit 429.

Device routes (`device_router`, served on both listeners):
  GET  /v1/claude/approvals                 up to 5 pending approvals, newest first, with this device nonce
  POST /v1/claude/approvals/{id}/decide     {decision: allow|deny, nonce}
  GET  /v1/claude/status                    the newest usage line, or {"configured": false}
Owner routes (`owner_router`, loopback listener only):
  POST /v1/claude/approvals                 the hook's request: creates an approval and waits for the answer
  POST /v1/claude/status                    the status line's report
  GET  /v1/claude/audit                     append-only history

A device cannot create an approval and the owner token cannot decide one: the two sides never share a route.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from oyster.api.common import AnyPrincipal, Device, DeviceBody, Owner, json_body, parse_model
from oyster.api.errors import ApiError
from oyster.claude import store as store_mod
from oyster.claude.models import ApprovalCreate, DecisionRequest, StatusUpdate

CREATE_BODY_LIMIT = 2048  # a request is ~400 bytes: session, tool, a 160-character summary, wait_s
STATUS_BODY_LIMIT = 1024

device_router = APIRouter()
owner_router = APIRouter()

CreateBody = Annotated[Any, Depends(json_body(CREATE_BODY_LIMIT))]
StatusBody = Annotated[Any, Depends(json_body(STATUS_BODY_LIMIT))]


def _rt(request: Request):
    return request.app.state.claude


def _throttle(limiter, key: str | None) -> None:
    wait = limiter.check(key or "?")
    if wait is not None:
        detail = f"too many requests; retry in {wait} s"
        raise ApiError(429, "rate_limited", detail, {"Retry-After": str(wait)})


# --- device routes ---------------------------------------------------------------------------------


@device_router.get("/v1/claude/approvals")
def list_approvals(request: Request, p: Device) -> dict:
    rt = _rt(request)
    _throttle(rt.list_limiter, p.device_id)
    return {"approvals": rt.service.list_for_device(p.device_id)}


@device_router.post("/v1/claude/approvals/{approval_id}/decide")
def decide(approval_id: str, request: Request, p: Device, body: DeviceBody) -> dict:
    rt = _rt(request)
    _throttle(rt.decide_limiter, p.device_id)
    req = parse_model(DecisionRequest, body)
    done = rt.service.decide(p.device_id, approval_id, req.decision, req.nonce)
    return {"approval_id": done.approval_id, "state": done.state, "decision": req.decision}


@device_router.get("/v1/claude/status")
def get_status(request: Request, p: AnyPrincipal) -> dict:
    return _rt(request).service.get_status()


# --- owner routes ----------------------------------------------------------------------------------


@owner_router.post("/v1/claude/approvals")
async def create_approval(request: Request, _: Owner, body: CreateBody) -> dict:
    """Long poll: answers only when a device decided, the window closed or the backend stopped. The reply is
    `{"decision": "allow" | "deny" | "timeout", "approval_id": ...}`; "allow" always means a recorded device
    decision. When the caller disconnects first, the approval is cancelled."""
    rt = _rt(request)
    _throttle(rt.create_limiter, "owner")
    req = parse_model(ApprovalCreate, body)
    approval = await run_in_threadpool(rt.service.create, req.session, req.tool, req.summary, req.wait_s)
    decision = await rt.service.wait_decision(approval.approval_id, request.is_disconnected)
    return {
        "decision": "timeout" if decision == "disconnected" else decision,
        "approval_id": approval.approval_id,
    }


@owner_router.post("/v1/claude/status")
def post_status(request: Request, _: Owner, body: StatusBody) -> dict:
    rt = _rt(request)
    _throttle(rt.status_limiter, "owner")
    rt.service.set_status(parse_model(StatusUpdate, body))
    return {"stored": True}


@owner_router.get("/v1/claude/audit")
def read_audit(
    request: Request,
    _: Owner,
    limit: Annotated[int | None, Query(ge=1, le=store_mod.MAX_PAGE)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
    approval_id: Annotated[str | None, Query(max_length=40)] = None,
) -> dict:
    rows, nxt = _rt(request).service.history(before=before, limit=limit, approval_id=approval_id)
    return {"entries": [store_mod.entry_wire(e) for e in rows], "next_before": nxt}
