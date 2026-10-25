"""Pairing: the owner creates a short-lived code, a device redeems it for a device bearer token.

- Code: 8 characters from an unambiguous 30-character alphabet (no 0/O, 1/I/L, U), from an injectable RNG
  (secrets.SystemRandom by default). Valid for 300 s, single use, stored only as a salted PBKDF2-SHA256 hash.
- One live code at a time: creating a new code burns any code still outstanding, so a failed attempt is
  always charged to exactly one code. After 5 failed attempts that code is burned.
- Redemption compares hashes in constant time (also when no code exists, to keep timing flat), then claims
  the code with one conditional statement (exactly one caller wins) and only then asks the existing device
  store to enrol the device (`create_device`, which is what POST /v1/admin/devices calls). A device id that
  already exists is refused (409) instead of rotating that device's token: pairing can add a device, never
  take over one.
- Every outcome is audited with identifiers only; the code and the token never reach the audit log.
Throttling per client address is applied by the API layer (see mount.Companion).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from collections.abc import Callable
from typing import Any, Protocol

from oyster.companion.audit import AuditLog
from oyster.companion.models import PairingCreate, PairingCreated, PairingRedeem, PairingRedeemed
from oyster.companion.store import CompanionStore, PairingRow
from oyster.store import NotFound

log = logging.getLogger("oyster.companion")

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # 30 symbols: no 0 O 1 I L U
CODE_LENGTH = 8
PAIRING_TTL_S = 300
MAX_FAILED_ATTEMPTS = 5
PBKDF2_ROUNDS = 100_000
SALT_BYTES = 16
_DUMMY_SALT = bytes(SALT_BYTES)


class Rng(Protocol):
    def choice(self, seq: Any) -> Any: ...
    def randrange(self, stop: int) -> int: ...


class PairingRejected(Exception):
    """Wrong, expired, reused or burned code: one generic answer for the client, `reason` is for the audit."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PairingUnavailable(Exception):
    """The device store cannot take part safely (no way to check an id, or enrolment failed)."""


class DeviceExists(Exception):
    """The requested device id is already enrolled; pairing never replaces an existing device."""


def hash_code(code: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), salt, PBKDF2_ROUNDS).hex()


def _state(row: PairingRow | None, now: float) -> str:
    if row is None:
        return "none"
    if row.redeemed_at is not None:
        return "redeemed"
    if row.burned_at is not None:
        return "burned"
    if now >= row.expires_at:
        return "expired"
    return "active"


class PairingService:
    def __init__(
        self,
        store: CompanionStore,
        audit: AuditLog,
        clock: Callable[[], float],
        rng: Rng | None = None,
    ):
        self._store = store
        self._audit = audit
        self._clock = clock
        self._rng: Rng = rng or secrets.SystemRandom()
        self._lock = threading.Lock()  # serialises redemptions inside this process

    def create(self, req: PairingCreate) -> PairingCreated:
        now = self._clock()
        code = "".join(self._rng.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        salt = bytes(self._rng.randrange(256) for _ in range(SALT_BYTES))
        pairing_id = "pair-" + "".join(self._rng.choice("0123456789abcdef") for _ in range(8))
        row = PairingRow(
            pairing_id=pairing_id,
            code_salt=salt.hex(),
            code_hash=hash_code(code, salt),
            bound_device_id=req.device_id,
            simulated=req.simulated,
            created_at=now,
            expires_at=now + PAIRING_TTL_S,
            failed_attempts=0,
            redeemed_at=None,
            burned_at=None,
            device_id=None,
        )
        self._store.create_pairing(row)
        detail = f"ttl_s={PAIRING_TTL_S} bound={req.device_id is not None}"
        self._audit.record("owner", "pairing_created", pairing_id, "ok", detail)
        return PairingCreated(
            pairing_id=pairing_id,
            code=code,
            ttl_s=PAIRING_TTL_S,
            expires_at=row.expires_at,
            device_id=req.device_id,
        )

    def redeem(self, req: PairingRedeem, device_store: Any, client: str = "unknown") -> PairingRedeemed:
        """Raises PairingRejected, DeviceExists or PairingUnavailable; on success returns the token once."""
        with self._lock:
            now = self._clock()
            code = req.code.strip().upper()
            row = self._store.latest_pairing()
            state = _state(row, now)
            salt = bytes.fromhex(row.code_salt) if row is not None else _DUMMY_SALT
            digest = hash_code(code, salt)
            matches = row is not None and hmac.compare_digest(digest, row.code_hash)
            if row is None or not matches or state != "active":
                reason = "no_code" if row is None else "wrong_code" if not matches else _DEAD_REASON[state]
                known = row is not None and (matches or state == "active")
                self._fail(row if known else None, reason, client, now, charge=state == "active")
                raise PairingRejected(reason)
            if row.bound_device_id is not None and row.bound_device_id != req.device_id:
                self._fail(row, "device_mismatch", client, now, charge=True)
                raise PairingRejected("device_mismatch")
            get_device = getattr(device_store, "get_device", None)
            if get_device is None:
                self._deny(row.pairing_id, "denied", "no_device_lookup")
                raise PairingUnavailable("the device store cannot check whether a device id exists")
            try:
                get_device(req.device_id)
            except NotFound:
                pass
            else:
                self._deny(row.pairing_id, "denied", "device_exists")
                raise DeviceExists(req.device_id)
            if not self._store.claim_pairing(row.pairing_id, req.device_id, now):
                self._fail(row, "reused", client, now, charge=False)  # another listener claimed it first
                raise PairingRejected("reused")
            try:
                token = device_store.create_device(
                    req.device_id, req.display_name or req.device_id, row.simulated
                )
            except Exception as e:
                log.warning("pairing: device enrolment failed (%s)", type(e).__name__)
                self._deny(row.pairing_id, "error", "enrol_error")
                raise PairingUnavailable("device enrolment failed") from None
            actor = f"device:{req.device_id}"
            self._audit.record(actor, "pairing_redeemed", row.pairing_id, "ok", f"simulated={row.simulated}")
            return PairingRedeemed(device_id=req.device_id, token=token)

    def _deny(self, target: str, outcome: str, reason: str, extra: str = "") -> None:
        self._audit.record("anonymous", "pairing_failed", target, outcome, f"reason={reason}{extra}")

    def _fail(self, row: PairingRow | None, reason: str, client: str, now: float, *, charge: bool) -> None:
        """Audit a failed attempt. With `charge` the live code takes one strike (burned at the limit)."""
        outcome, extra = "denied", f" client={client}"
        if charge:
            live = self._store.latest_pairing()  # the live code, whether or not the digest matched
            if live is not None and _state(live, now) == "active":
                count, burned = self._store.record_pairing_failure(live.pairing_id, now, MAX_FAILED_ATTEMPTS)
                extra += f" attempts={count}"
                outcome = "burned" if burned else outcome
        self._deny(row.pairing_id if row is not None else "-", outcome, reason, extra)


_DEAD_REASON = {"redeemed": "reused", "burned": "burned", "expired": "expired"}
