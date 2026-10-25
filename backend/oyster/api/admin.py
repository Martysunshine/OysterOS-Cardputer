"""Owner-only enrolment endpoints (/v1/admin/*), kept apart from device job access (ADR-004)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from oyster.api.common import AdminBody, Owner, parse_model, schema_failure
from oyster.api.errors import ApiError
from oyster.evidence.validate import SchemaValidationError, parse_target
from oyster.policy import catalogue

router = APIRouter()


class DeviceEnrol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(pattern=r"^[0-9a-zA-Z_-]{1,40}$")
    display_name: str | None = Field(default=None, max_length=64)
    simulated: bool = False


@router.post("/v1/admin/devices")
def enrol_device(request: Request, _: Owner, body: AdminBody) -> dict:
    """Returns the token exactly once. Re-enrolling an existing id rotates (revokes) its old token."""
    req = parse_model(DeviceEnrol, body)
    store = request.app.state.store
    token = store.create_device(req.device_id, req.display_name or req.device_id, req.simulated)
    return {"device_id": req.device_id, "token": token}


@router.post("/v1/admin/targets")
def enrol_target(request: Request, _: Owner, body: AdminBody) -> dict:
    try:
        target = parse_target(body)
    except SchemaValidationError as e:
        raise schema_failure(e) from None
    if catalogue.load_profile(target.profile_id) is None:
        known = catalogue.known_profile_ids()
        raise ApiError(422, "validation_failed", f"unknown profile_id {target.profile_id!r}; known: {known}")
    stored = request.app.state.store.upsert_target(target)
    return stored.model_dump(mode="json")


@router.get("/v1/admin/targets")
def list_targets(request: Request, _: Owner) -> dict:
    return {"targets": [t.model_dump(mode="json") for t in request.app.state.store.list_targets()]}
