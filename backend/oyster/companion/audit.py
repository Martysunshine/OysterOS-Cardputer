"""Append-only audit log of companion events.

Records who did what to which target with which outcome: pairing created / redeemed / failed, host
enrolled / deleted, health read. This class offers `record` and `entries` and nothing else, and the
storage layer has no statement that changes or removes an audit row (the table also carries triggers that
abort any such attempt). It never receives a secret: callers pass identifiers only, and every field is
reduced to a short, printable, constrained string before it is stored. Retention is unbounded in this
version (see docs/COMPANION_API.md).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from oyster.companion.models import AuditEntry
from oyster.companion.store import CompanionStore

AUDIT_ACTIONS = (
    "pairing_created",
    "pairing_redeemed",
    "pairing_failed",
    "host_enrolled",
    "host_deleted",
    "health_read",
)
MAX_FIELD = 64
MAX_DETAIL = 160
_UNSAFE = re.compile(r"[^0-9A-Za-z_.:@=,/ -]")


def _clean(value: str, limit: int) -> str:
    return _UNSAFE.sub("?", str(value))[:limit]


class AuditLog:
    def __init__(self, store: CompanionStore, clock: Callable[[], float]):
        self._store = store
        self._clock = clock

    def record(
        self, actor: str, action: str, target: str = "-", outcome: str = "ok", detail: str = ""
    ) -> int:
        if action not in AUDIT_ACTIONS:
            raise ValueError(f"unknown audit action {action!r}")
        return self._store.audit_append(
            self._clock(),
            _clean(actor, MAX_FIELD),
            action,
            _clean(target, MAX_FIELD) or "-",
            _clean(outcome, MAX_FIELD) or "-",
            _clean(detail, MAX_DETAIL),
        )

    def entries(self, limit: int = 50, before_id: int | None = None) -> list[AuditEntry]:
        """Newest first; `before_id` continues from an earlier page."""
        return self._store.audit_list(max(1, min(limit, 200)), before_id)
