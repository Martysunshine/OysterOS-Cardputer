"""Shared API helpers: bounded JSON bodies, request-model parsing, wire shapes."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import Depends, Request
from pydantic import BaseModel, ValidationError

from oyster.api.errors import ApiError
from oyster.auth import Principal, require_device, require_owner, require_principal
from oyster.contracts import Job
from oyster.evidence.validate import SchemaValidationError

DEVICE_BODY_LIMIT = 4096  # docs/API.md: device requests stay under 4 KiB
ADMIN_BODY_LIMIT = 16384


def json_body(limit: int):
    """Dependency: read at most `limit` bytes (413 beyond) and parse JSON (422 when malformed)."""

    async def _read(request: Request) -> Any:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ApiError(413, "payload_too_large", f"body exceeds {limit} bytes")
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > limit:
                raise ApiError(413, "payload_too_large", f"body exceeds {limit} bytes")
        try:
            return json.loads(buf.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(422, "validation_failed", "body is not valid UTF-8 JSON") from None

    return _read


# Dependency aliases (auth first, so unauthenticated requests never get their body read)
Owner = Annotated[Principal, Depends(require_owner)]
Device = Annotated[Principal, Depends(require_device)]
AnyPrincipal = Annotated[Principal, Depends(require_principal)]
DeviceBody = Annotated[Any, Depends(json_body(DEVICE_BODY_LIMIT))]
AdminBody = Annotated[Any, Depends(json_body(ADMIN_BODY_LIMIT))]


def parse_model[M: BaseModel](model: type[M], body: Any) -> M:
    if not isinstance(body, dict):
        raise ApiError(422, "validation_failed", "body must be a JSON object")
    try:
        return model.model_validate(body)
    except ValidationError as e:
        first = e.errors()[0]
        where = "/".join(str(p) for p in first.get("loc", ())) or "(body)"
        raise ApiError(422, "validation_failed", f"{where}: {first.get('msg')}"[:300]) from None


def schema_failure(e: SchemaValidationError, prefix: str = "") -> ApiError:
    return ApiError(422, "validation_failed", (prefix + "; ".join(e.errors))[:500])


def wire_job(job: Job) -> dict:
    """Job as job.schema.json expects it: absent limits are omitted, not null."""
    d = job.model_dump(mode="json")
    d["limits"] = {k: v for k, v in d["limits"].items() if v is not None}
    return d
