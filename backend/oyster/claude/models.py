"""Wire models, limits and states of the Claude Code link (docs/CLAUDE_CODE_LINK.md).

The PC-side hook posts an approval request; a paired device lists it and answers allow or deny. Request
bodies are strict (`extra="forbid"`) and every string is bounded, so nothing unbounded reaches the store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SESSION_MAX = 64
TOOL_MAX = 40
SUMMARY_MAX = 160  # what the hook may send (it clips to this) and what is stored
DEVICE_SUMMARY_MAX = 80  # what a device receives: its list shows 28 columns, the detail view a few lines
MODEL_MAX = 40
NONCE_MIN, NONCE_MAX = 8, 128
WAIT_MIN, WAIT_DEFAULT, WAIT_MAX = 1, 90, 120

# Identifier-like strings only: no spaces, control characters or quotes ever reach the store or a screen.
IDENT_PATTERN = r"^[A-Za-z0-9_.:-]+$"
APPROVAL_ID = re.compile(r"^appr-[0-9a-f]{16}$")
_PRINTABLE = r"^[\x20-\x7e]+$"

PENDING = "pending"
ALLOWED = "allowed"
DENIED = "denied"
EXPIRED = "expired"
CANCELLED = "cancelled"
STATES = (PENDING, ALLOWED, DENIED, EXPIRED, CANCELLED)
DECIDED = (ALLOWED, DENIED)  # the only states that carry a device id

Pct = Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)]


class ApprovalCreate(BaseModel):
    """Hook side: one tool call waiting for an answer."""

    model_config = ConfigDict(extra="forbid")
    session: str = Field(min_length=1, max_length=SESSION_MAX, pattern=IDENT_PATTERN)
    tool: str = Field(min_length=1, max_length=TOOL_MAX, pattern=IDENT_PATTERN)
    summary: str = Field(max_length=SUMMARY_MAX)
    wait_s: Annotated[int, Field(strict=True, ge=WAIT_MIN, le=WAIT_MAX)] = WAIT_DEFAULT


class DecisionRequest(BaseModel):
    """Device side: the answer, bound to the nonce issued to this device when it listed the approval."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["allow", "deny"]
    nonce: str = Field(min_length=NONCE_MIN, max_length=NONCE_MAX)


class StatusUpdate(BaseModel):
    """Status line side: what Claude Code reports about the session. Plan limits are optional and only
    present when Claude Code itself sends them (claude.ai subscribers); nothing is computed here."""

    model_config = ConfigDict(extra="forbid")
    session: str = Field(min_length=1, max_length=SESSION_MAX, pattern=IDENT_PATTERN)
    model: str = Field(min_length=1, max_length=MODEL_MAX, pattern=_PRINTABLE)
    cost_usd: Annotated[float, Field(strict=True, ge=0, le=1_000_000, allow_inf_nan=False)]
    context_pct: Pct | None = None
    five_hour_pct: Pct | None = None
    seven_day_pct: Pct | None = None


@dataclass(frozen=True)
class Approval:
    approval_id: str
    session: str
    tool: str
    summary: str  # stored redacted
    state: str
    created_at: float
    expires_at: float
    reason: str | None = None
    decided_by: str | None = None  # device id; set exactly when state is allowed or denied
    decided_at: float | None = None
    seq: int = 0


@dataclass(frozen=True)
class Status:
    session: str
    model: str
    cost_usd: float
    updated_at: float
    context_pct: float | None = None
    five_hour_pct: float | None = None
    seven_day_pct: float | None = None
