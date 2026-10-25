"""Smart Home routes. Auth semantics follow the rest of the API: missing or invalid token 401, wrong role 403,
unknown input 422, anything outside the allowlist 404/403.

Device routes (`device_router`, served on both listeners; the owner token works on the loopback one):
  GET  /v1/home/rooms                     {configured, reason?, rooms: [{id, name, count}], next_after}
  GET  /v1/home/rooms/{room_id}/devices   {room, devices: [{entity_id, label, state, impactful, actions}],
                                          next_after}
  POST /v1/home/actions                   {entity_id, action, confirmed?}; header Idempotency-Key
                                          (8..80 chars); device token only: the confirmation is a key press
                                          on the device
Owner routes (`owner_router`, loopback listener only):
  GET  /v1/home/status                    what is configured (never the URL or the token), allowlist problems
  GET  /v1/home/audit                     newest first, <= 50/page

Responses stay under 4 KiB (rooms <= 16 per page, devices <= 8 per page).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from oyster.api.common import AnyPrincipal, Device, DeviceBody, Owner, parse_model
from oyster.api.errors import ApiError
from oyster.auth import Principal
from oyster.home import store
from oyster.home.allowlist import ENTITY_ID
from oyster.home.service import DEVICES_PAGE_MAX, KEY_PATTERN, ROOMS_PAGE_MAX, Actor

_KEY = re.compile(KEY_PATTERN)
_CURSOR = r"^[0-9]{1,3}$"

device_router = APIRouter()
owner_router = APIRouter()


def _service(request: Request):
    return request.app.state.home.service


def _actor(p: Principal) -> Actor:
    if p.is_owner:
        return Actor("owner", "admin", None)
    return Actor("device", p.device_id or "?", p.device_id)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(min_length=3, max_length=64, pattern=ENTITY_ID.pattern)
    action: Literal["turn_on", "turn_off", "toggle"]
    confirmed: StrictBool = False


@device_router.get("/v1/home/rooms")
def list_rooms(
    request: Request,
    p: AnyPrincipal,
    limit: Annotated[int | None, Query(ge=1, le=ROOMS_PAGE_MAX)] = None,
    after: Annotated[str | None, Query(pattern=_CURSOR)] = None,
) -> dict:
    return _service(request).rooms(_actor(p), after, limit)


@device_router.get("/v1/home/rooms/{room_id}/devices")
def list_devices(
    room_id: Annotated[str, Path(max_length=40)],
    request: Request,
    p: AnyPrincipal,
    limit: Annotated[int | None, Query(ge=1, le=DEVICES_PAGE_MAX)] = None,
    after: Annotated[str | None, Query(pattern=_CURSOR)] = None,
) -> dict:
    return _service(request).devices(_actor(p), room_id, after, limit)


@device_router.post("/v1/home/actions")
def post_action(request: Request, p: Device, body: DeviceBody) -> dict:
    key = request.headers.get("idempotency-key", "")
    if not _KEY.fullmatch(key):
        raise ApiError(422, "validation_failed", "Idempotency-Key header (8..80 characters) is required")
    req = parse_model(ActionRequest, body)
    return _service(request).act(_actor(p), req.entity_id, req.action, req.confirmed, key)


@owner_router.get("/v1/home/status")
def home_status(request: Request, _: Owner) -> dict:
    return _service(request).status()


@owner_router.get("/v1/home/audit")
def read_audit(
    request: Request,
    _: Owner,
    limit: Annotated[int | None, Query(ge=1, le=store.MAX_PAGE)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
    device_id: Annotated[str | None, Query(max_length=40)] = None,
) -> dict:
    rows, nxt = _service(request).store.audit_page(device_id=device_id, before=before, limit=limit)
    return {"entries": [store.entry_wire(e) for e in rows], "next_before": nxt}
