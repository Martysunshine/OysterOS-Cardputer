"""Approval state machine, redaction and the usage line of the Claude Code link.

States (`models.STATES`) and the only legal moves, all out of `pending`:

    pending -> allowed | denied      (a paired device answered with the nonce issued to it)
    pending -> expired               (the window closed, the backend restarted or stopped)
    pending -> cancelled             (the hook disconnected before an answer arrived)

`allowed` is reachable only through `decide`, which needs a device id, a live pending approval and the
nonce that device was given when it listed this approval; the schema additionally refuses an allowed row
without a device (store.py). The long poll maps expired and cancelled to "timeout", never to "allow".
Every transition writes its audit row in the same transaction. Clock, id and nonce sources are injectable.
"""

from __future__ import annotations

import hmac
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace

import anyio
from anyio import to_thread

from oyster.ai.redact import REDACTED, env_secrets, redact_text
from oyster.api.errors import ApiError
from oyster.claude.models import (
    ALLOWED,
    APPROVAL_ID,
    CANCELLED,
    DENIED,
    DEVICE_SUMMARY_MAX,
    EXPIRED,
    MODEL_MAX,
    PENDING,
    SUMMARY_MAX,
    Approval,
    Status,
    StatusUpdate,
)
from oyster.claude.store import AuditEntry, ClaudeStore, clamp_limit

SYSTEM = ("system", "claude")
OWNER = ("owner", "hook")
_OUTCOME = {ALLOWED: "allow", DENIED: "deny", EXPIRED: "timeout", CANCELLED: "timeout"}


class ClaudeError(ApiError):
    """An API error raised by the service layer (same body shape as every other route)."""


class IllegalTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudeConfig:
    max_pending: int = 8  # approvals waiting at once: bounds the open long polls
    list_limit: int = 5  # what one device list returns
    retention_s: float = 7 * 86400  # finished approvals are dropped after this (the audit rows stay)
    keep_sessions: int = 8  # sessions kept for the usage line
    poll_interval_s: float = 0.25  # how often a waiting request looks at its approval
    creates_per_minute: int = 30  # hook requests (owner)
    decides_per_minute: int = 30  # per device, wrong nonces included
    lists_per_minute: int = 120  # per device (the device polls every few seconds)
    status_per_minute: int = 240  # status line posts (owner)


# --- redaction ----------------------------------------------------------------------------------------
# oyster.ai.redact removes PEM blocks, bearer tokens, vendor-prefixed keys and long hex. These add the
# shapes a shell command shows: key=value pairs, flags that take a secret, URL credentials, vendor tokens
# and any long token-like run. Over-redacting a summary only costs readability.
_CRED_NAME = (
    r"(?:[\w.-]{0,80}(?:pass(?:word|wd)?|pwd|token|secret|api[_-]?key|auth(?:orization)?|credential|cookie"
    r"|private[_-]?key|access[_-]?key|session[_-]?key|signature)[\w.-]{0,80}|key)"
)
_VALUE = r"""(?:(?:bearer|basic|token|digest)\s+)?(?:"[^"]*"|'[^']*'|\S+)"""
_KV = re.compile(rf"(?i)(?<![\w-])({_CRED_NAME})(\s*[=:]\s*)({_VALUE})")
_FLAG = re.compile(rf"(?i)(--?{_CRED_NAME})(\s+|=)({_VALUE})")
_URL_CREDENTIALS = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_VENDOR = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*)"
)
_RUN = re.compile(r"(?<![A-Za-z0-9+_=-])[A-Za-z0-9+_=-]{32,}(?![A-Za-z0-9+_=-])")
_SPACE = re.compile(r"\s+")


def _mask_run(m: re.Match) -> str:
    s = m.group(0)
    has_letter = any(c.isalpha() for c in s)
    has_digit = any(c.isdigit() for c in s)
    return REDACTED if has_letter and has_digit else s


def _kv(m: re.Match) -> str:
    return f"{m.group(1)}{m.group(2)}{REDACTED}"


def clip_text(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max(0, max_chars - 3)] + "..."


def redact_summary(text: str, max_chars: int = SUMMARY_MAX, extra_secrets: Iterable[str] = ()) -> str:
    """One line of text with credential-looking substrings removed, bounded to `max_chars`."""
    t = redact_text(text, extra_secrets)
    t = _SPACE.sub(" ", t).strip()
    t = _URL_CREDENTIALS.sub(rf"\1{REDACTED}@", t)
    t = _FLAG.sub(_kv, t)
    t = _KV.sub(_kv, t)
    t = _VENDOR.sub(REDACTED, t)
    t = _RUN.sub(_mask_run, t)
    return clip_text(t, max_chars)


def device_text(text: str, max_chars: int = DEVICE_SUMMARY_MAX) -> str:
    """What a device screen can show: printable ASCII only, clipped with an explicit marker."""
    ascii_only = "".join(c if " " <= c <= "~" else "?" for c in text)
    return clip_text(ascii_only, max_chars)


# --- service ------------------------------------------------------------------------------------------


class ClaudeService:
    """All approval logic. Every state change is one transaction that also writes its audit row."""

    def __init__(
        self,
        store: ClaudeStore,
        *,
        clock: Callable[[], float] = time.time,
        config: ClaudeConfig | None = None,
        new_id: Callable[[], str] | None = None,
        new_nonce: Callable[[], str] | None = None,
        secrets_source: Callable[[], Iterable[str]] = env_secrets,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ):
        self.store = store
        self.clock = clock
        self.config = config or ClaudeConfig()
        self.new_id = new_id or (lambda: "appr-" + secrets.token_hex(8))
        self.new_nonce = new_nonce or (lambda: secrets.token_urlsafe(16))
        self._secrets = secrets_source
        self._sleep = sleep

    # --- internals ---------------------------------------------------------------------------------

    def _audit(
        self, conn, actor: tuple[str, str], a: Approval, *, event: str, outcome: str, reason: str | None
    ) -> None:
        self.store.insert_audit(
            conn,
            AuditEntry(
                ts=self.clock(),
                actor_kind=actor[0],
                actor_id=actor[1],
                device_id=a.decided_by,
                event=event,
                outcome=outcome,
                reason=reason,
                approval_id=a.approval_id,
                tool=a.tool,
            ),
        )

    def _move(
        self,
        conn,
        a: Approval,
        new: str,
        *,
        actor: tuple[str, str],
        reason: str | None = None,
        decided_by: str | None = None,
    ) -> Approval:
        if a.state != PENDING or new == PENDING:
            raise IllegalTransition(f"{a.state} -> {new}")
        moved = replace(a, state=new, reason=reason, decided_by=decided_by, decided_at=self.clock())
        self.store.update_approval(conn, PENDING, moved)
        self.store.drop_nonces(conn, a.approval_id)  # a nonce is single use
        self._audit(conn, actor, moved, event=f"approval.{new}", outcome=new, reason=reason)
        return moved

    def _refuse(
        self,
        conn,
        actor: tuple[str, str],
        status: int,
        code: str,
        detail: str,
        *,
        reason: str,
        device_id: str | None,
        approval_id: str | None = None,
        tool: str | None = None,
        headers: dict | None = None,
    ) -> ClaudeError:
        self.store.insert_audit(
            conn,
            AuditEntry(
                ts=self.clock(),
                actor_kind=actor[0],
                actor_id=actor[1],
                device_id=device_id,
                event="request.denied",
                outcome="refused",
                reason=reason,
                approval_id=approval_id,
                tool=tool,
            ),
        )
        return ClaudeError(status, code, detail, headers)

    def _sweep_one(self, conn, a: Approval, now: float) -> Approval:
        if a.state == PENDING and now >= a.expires_at:
            return self._move(conn, a, EXPIRED, actor=SYSTEM, reason="timeout")
        return a

    def _sweep_all(self, conn, now: float) -> int:
        n = 0
        for a in self.store.due(conn, now):
            self._move(conn, a, EXPIRED, actor=SYSTEM, reason="timeout")
            n += 1
        return n

    def _clean(self, text: str, max_chars: int) -> str:
        return redact_summary(text, max_chars, self._secrets())

    # --- hook side ---------------------------------------------------------------------------------

    def create(self, session: str, tool: str, summary: str, wait_s: int) -> Approval:
        """Admit one approval request. The summary is redacted again here whatever the hook did."""
        clean = self._clean(summary, SUMMARY_MAX)
        result: Approval | ClaudeError
        with self.store.tx() as conn:
            now = self.clock()
            self._sweep_all(conn, now)
            self.store.prune_approvals(conn, now - self.config.retention_s)
            if self.store.count_pending(conn, now) >= self.config.max_pending:
                result = self._refuse(
                    conn,
                    OWNER,
                    503,
                    "busy",
                    "too many approvals are waiting; retry shortly",
                    reason="busy",
                    device_id=None,
                    tool=tool,
                    headers={"Retry-After": "5"},
                )
            else:
                result = self.store.insert_approval(
                    conn,
                    Approval(
                        approval_id=self.new_id(),
                        session=session,
                        tool=tool,
                        summary=clean,
                        state=PENDING,
                        created_at=now,
                        expires_at=now + wait_s,
                    ),
                )
                self._audit(conn, OWNER, result, event="approval.created", outcome=PENDING, reason=None)
        if isinstance(result, ClaudeError):
            raise result
        return result

    def cancel(self, approval_id: str, reason: str = "hook_disconnected") -> bool:
        """The hook went away: nobody waits for an answer any more. True when it was still pending."""
        with self.store.tx() as conn:
            a = self.store.get_approval(conn, approval_id)
            if a is None or a.state != PENDING:
                return False
            self._move(conn, a, CANCELLED, actor=OWNER, reason=reason)
            return True

    def poll(self, approval_id: str) -> str | None:
        """Current state (deadlines applied), None for an unknown id. Reads without a transaction while the
        window is open."""
        peek = self.store.peek_state(approval_id)
        if peek is None:
            return None
        state, expires_at = peek
        if state == PENDING and self.clock() >= expires_at:
            with self.store.tx() as conn:
                a = self.store.get_approval(conn, approval_id)
                if a is not None:
                    state = self._sweep_one(conn, a, self.clock()).state
        return state

    async def wait_decision(
        self, approval_id: str, is_disconnected: Callable[[], Awaitable[bool]] | None = None
    ) -> str:
        """Block (cooperatively) until the approval is decided or its window closes.

        Returns "allow" or "deny" only for a recorded device decision, "timeout" when the window closed
        or the backend stopped, "disconnected" when the caller went away (the approval is cancelled)."""
        finished = False
        try:
            while True:
                state = await to_thread.run_sync(self.poll, approval_id)
                if state is None:
                    finished = True
                    return "timeout"
                if state != PENDING:
                    finished = True
                    return _OUTCOME[state]
                if is_disconnected is not None and await is_disconnected():
                    await to_thread.run_sync(self.cancel, approval_id)
                    finished = True
                    return "disconnected"
                await self._sleep(self.config.poll_interval_s)
        finally:
            if not finished:  # the task itself was cancelled (server shutdown, test teardown)
                with anyio.CancelScope(shield=True):
                    await to_thread.run_sync(self.cancel, approval_id, "request_cancelled")

    def close_pending(self, reason: str, actor: tuple[str, str] = SYSTEM) -> int:
        """End every pending approval as expired (restart or stop): nobody is waiting for them any more, so
        a late answer from a device must not look like it did anything."""
        n = 0
        with self.store.tx() as conn:
            for a in self.store.due(conn, None):
                self._move(conn, a, EXPIRED, actor=actor, reason=reason)
                n += 1
            self.store.orphan_nonces(conn)
        return n

    def recover_after_restart(self) -> int:
        return self.close_pending("backend_restarted", ("system", "startup"))

    # --- device side -------------------------------------------------------------------------------

    def list_for_device(self, device_id: str) -> list[dict]:
        """Up to `list_limit` pending approvals, newest first, each with the nonce issued to this device
        (the same nonce on every later list while the approval is pending)."""
        out: list[dict] = []
        with self.store.tx() as conn:
            now = self.clock()
            self._sweep_all(conn, now)
            for a in self.store.pending(conn, now, self.config.list_limit):
                nonce = self.store.nonce_for(conn, a.approval_id, device_id)
                if nonce is None:
                    nonce = self.new_nonce()
                    self.store.add_nonce(conn, a.approval_id, device_id, nonce, now)
                out.append(
                    {
                        "approval_id": a.approval_id,
                        "tool": a.tool,
                        "summary": device_text(a.summary),
                        "age_s": max(0, int(now - a.created_at)),
                        "expires_in_s": max(0, math.ceil(a.expires_at - now)),
                        "nonce": nonce,
                    }
                )
        return out

    def decide(self, device_id: str, approval_id: str, decision: str, nonce: str) -> Approval:
        """Record one device's answer. Rejected (and audited) unless the approval is pending and unexpired
        and `nonce` is the one issued to this device for this approval. One decision only."""
        actor = ("device", device_id)
        known_format = bool(APPROVAL_ID.match(approval_id))
        outcome: Approval | ClaudeError
        with self.store.tx() as conn:
            now = self.clock()
            a = self.store.get_approval(conn, approval_id) if known_format else None
            if a is None:
                outcome = self._refuse(
                    conn,
                    actor,
                    404,
                    "not_found",
                    "unknown id",
                    reason="unknown_approval",
                    device_id=device_id,
                    approval_id=approval_id if known_format else None,
                )
            else:
                outcome = self._decide(conn, actor, now, a, decision, nonce)
        if isinstance(outcome, ClaudeError):
            raise outcome
        return outcome

    def _decide(
        self, conn, actor: tuple[str, str], now: float, a: Approval, decision: str, nonce: str
    ) -> Approval | ClaudeError:
        device_id = actor[1]
        ctx = {"device_id": device_id, "approval_id": a.approval_id, "tool": a.tool}
        a = self._sweep_one(conn, a, now)
        if a.state != PENDING:
            if a.state == EXPIRED:
                return self._refuse(
                    conn, actor, 409, "expired", "the approval window has closed", reason="expired", **ctx
                )
            return self._refuse(
                conn,
                actor,
                409,
                "already_decided",
                "the approval is no longer pending",
                reason="not_pending",
                **ctx,
            )
        stored = self.store.nonce_for(conn, a.approval_id, device_id)
        if not stored or not hmac.compare_digest(stored.encode(), nonce.encode()):
            # also the case for a device that never listed this approval: it has no nonce to present
            return self._refuse(conn, actor, 403, "forbidden", "decision rejected", reason="bad_nonce", **ctx)
        new = ALLOWED if decision == "allow" else DENIED
        return self._move(conn, a, new, actor=actor, decided_by=device_id)

    # --- usage line --------------------------------------------------------------------------------

    def set_status(self, upd: StatusUpdate) -> None:
        now = self.clock()
        model = self._clean(upd.model, MODEL_MAX) or "?"
        status = Status(
            session=upd.session,
            model=model,
            cost_usd=round(upd.cost_usd, 4),
            context_pct=_pct(upd.context_pct),
            five_hour_pct=_pct(upd.five_hour_pct),
            seven_day_pct=_pct(upd.seven_day_pct),
            updated_at=now,
        )
        with self.store.tx() as conn:
            self.store.upsert_status(conn, status, self.config.keep_sessions)

    def get_status(self) -> dict:
        s = self.store.newest_status()
        if s is None:
            return {"configured": False}
        return {
            "configured": True,
            "updated_s_ago": max(0, int(self.clock() - s.updated_at)),
            "model": s.model,
            "cost_usd": s.cost_usd,
            "context_pct": s.context_pct,
            "five_hour_pct": s.five_hour_pct,
            "seven_day_pct": s.seven_day_pct,
        }

    # --- owner read --------------------------------------------------------------------------------

    def history(self, *, before: int | None = None, limit: int | None = None, approval_id: str | None = None):
        """Newest first. Returns (page, next_before): `next_before` is set when more rows exist."""
        n = clamp_limit(limit)
        with self.store.tx() as conn:
            rows = self.store.audit_page(conn, before=before, limit=n + 1, approval_id=approval_id)
        return rows[:n], (rows[n - 1].seq if len(rows) > n else None)


def _pct(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
