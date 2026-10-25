"""Unauthenticated endpoints: health and the clock hint. Nothing sensitive."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter

router = APIRouter()


@router.get("/v1/health")
def health() -> dict:
    return {"status": "ok", "api": "v1"}


@router.get("/v1/time")
def clock_hint() -> dict:
    """A clock hint only (clock_quality backend_hint); never endpoint authentication."""
    now = int(time.time())
    return {"unix": now, "iso": datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
