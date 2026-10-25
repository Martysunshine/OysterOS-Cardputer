"""Pure state machine of the live lab fixture (schemas/lab-profile.json, ADR-007). No Bluetooth here.

gatt_winrt.py feeds every read/write request in and sends the result back; clock and randomness are
injectable so every rule is unit-testable on any platform.

Application authorization (app_auth):
- read (offset 0) issues a fresh random challenge and returns [state u8: 0|1][challenge 16 bytes];
- write HMAC-SHA256(key, challenge) (32 bytes; key = UTF-8 bytes of LAB_FIXTURE_AUTH_TOKEN) authorizes
  the fixture globally for authorization_ttl_s. The challenge is single-use and expires after
  CHALLENGE_TTL_S. A well-formed write always gets an ATT success; the result is the state byte of the
  next read. A failed attempt leaves the current authorization unchanged. reset() clears everything.

Modes (local only, never exposed over GATT): VULNERABLE omits the application-authorization check for
diag_manifest / chunk_ctrl / chunk_data; CORRECTED enforces it. virtual_led writes need authorization in
both modes. Link-level policies (restricted_enc / restricted_mitm) are enforced by the OS protection
level, not here.

Denials: diag_manifest returns the single byte 0x01; chunk_data returns a header with status
not_authorized and no payload; a virtual_led write is rejected with ATT 0x08 (Insufficient
Authorization, the Core-spec code for server/application authorization - distinct from link-level
0x05 authentication and 0x0F encryption).

Long (prepared) writes: a write at offset 0 starts a value, a write at offset == bytes held continues it;
the value is processed once it reaches the expected length. A value that fits one ATT write
(chunk_ctrl always; app_auth when the session MTU allows) is never started in parts: a short plain write
is rejected with ATT 0x0D (invalid attribute value length). Long reads: the value is sliced at the
requested offset (app_auth slices the snapshot of its last offset-0 read).
The challenge lives for app_auth.challenge_ttl_s of the loaded profile.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from oyster_lab.fixture.profile import LabProfile
from oyster_lab.synthetic import APP_DENIAL, make_object, manifest_bytes, object_tag

log = logging.getLogger("oyster_lab.fixture")

MIN_TOKEN_BYTES = 32
CHALLENGE_TTL_S = 60.0  # default only; the fixture uses app_auth.challenge_ttl_s of the loaded profile
ATT_MIN_WRITE_PAYLOAD = 20  # a Write Request at the default 23-byte ATT_MTU carries 20 value bytes

# ATT error codes (Bluetooth Core Vol 3 Part F 3.4.1.1)
ATT_READ_NOT_PERMITTED = 0x02
ATT_WRITE_NOT_PERMITTED = 0x03
ATT_INVALID_OFFSET = 0x07
ATT_INSUFFICIENT_AUTHORIZATION = 0x08
ATT_ATTRIBUTE_NOT_FOUND = 0x0A
ATT_INVALID_ATTRIBUTE_VALUE_LENGTH = 0x0D
ATT_VALUE_NOT_ALLOWED = 0x13

CTRL = struct.Struct("<B4sIHH")  # [op][object_tag 4][offset u32][length u16][seq u16] = 13 bytes
CHUNK_HEADER = struct.Struct("<BHIH")  # [status][seq u16][offset u32][len u16] = 9 bytes
DIAG_STATUS = struct.Struct("<BH")  # [last_status][last_seq u16]

RESTRICTED_VALUES = {"restricted_enc": b"LAB-ENC-OK", "restricted_mitm": b"LAB-MITM-OK"}


class Mode(StrEnum):
    VULNERABLE = "vulnerable"
    CORRECTED = "corrected"


@dataclass(frozen=True)
class ReadResult:
    value: bytes | None = None
    att_error: int | None = None


@dataclass(frozen=True)
class WriteResult:
    att_error: int | None = None  # None = ATT success


def chunk_len_for_mtu(chunk_max: int, att_mtu: int, header_bytes: int = CHUNK_HEADER.size) -> int:
    """chunk_len <= min(chunk_max, ATT_MTU - 1 - header); 0 when the MTU is too small."""
    return max(0, min(chunk_max, att_mtu - 1 - header_bytes))


def auth_response(key: bytes, challenge: bytes) -> bytes:
    """HMAC-SHA256(key, challenge): what an enrolled client writes to app_auth."""
    return hmac.new(key, challenge, hashlib.sha256).digest()


@dataclass
class _Response:
    status: int
    seq: int
    offset: int
    length: int  # payload bytes; 0 unless status == ok


class FixtureState:
    """Fixture-global state for every characteristic of the lab profile (one client at a time)."""

    def __init__(
        self,
        profile: LabProfile,
        mode: Mode | str,
        token: bytes,
        *,
        obj: bytes | None = None,
        chunk_max: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if len(token) < MIN_TOKEN_BYTES:
            raise ValueError(f"auth token must be at least {MIN_TOKEN_BYTES} bytes")
        self._p = profile
        self._mode = Mode(mode)
        self._key = bytes(token)
        self._clock = clock
        self._random = random_bytes
        self._obj = make_object(profile.object_id) if obj is None else bytes(obj)
        if not 1 <= len(self._obj) <= profile.max_object_bytes:
            raise ValueError("synthetic object size outside 1..max_object_bytes")
        if CHUNK_HEADER.size != profile.chunk_header_bytes or CTRL.size != profile.characteristics[
            "chunk_ctrl"
        ].max_length:
            raise ValueError("lab profile transfer framing does not match this fixture")
        limit = profile.characteristics["chunk_data"].max_length - CHUNK_HEADER.size
        self._chunk_max = limit if chunk_max is None else int(chunk_max)
        if not 1 <= self._chunk_max <= limit:
            raise ValueError(f"chunk_max must be 1..{limit}")
        self._manifest = manifest_bytes(self._obj, profile.object_id, self._chunk_max)
        self._tag = object_tag(self._obj)
        self._st = profile.status_codes
        self._ops = profile.ctrl_ops
        self._led_allowed = frozenset(profile.led_values.values())
        self._lock = threading.RLock()
        self._t0 = clock()
        self._reset_locked()

    # ----- introspection (local only; never served over GATT) -----
    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def manifest(self) -> bytes:
        return self._manifest

    @property
    def object_bytes(self) -> bytes:
        return self._obj

    @property
    def chunk_max(self) -> int:
        return self._chunk_max

    @property
    def tag(self) -> bytes:
        return self._tag

    # ----- state -----
    def _reset_locked(self) -> None:
        self._authorized_until: float | None = None
        self._challenge: bytes | None = None
        self._challenge_expires = 0.0
        self._auth_snapshot: bytes | None = None
        self._pending: dict[str, bytearray] = {}
        self._response: _Response | None = None
        self._last_status = self._st["no_request"]
        self._last_seq = 0
        self._led = 0

    def reset(self) -> None:
        """Return to the initial state: unauthorized, no challenge, no transfer, virtual LED off."""
        with self._lock:
            self._reset_locked()
        log.info("state reset: authorization, challenge, transfer and virtual LED cleared")

    def is_authorized(self) -> bool:
        with self._lock:
            if self._authorized_until is None:
                return False
            if self._clock() >= self._authorized_until:
                self._authorized_until = None
                log.info("app_auth: authorization expired")
                return False
            return True

    def expire(self) -> None:
        """Drop an expired authorization (called periodically so the expiry is logged promptly)."""
        self.is_authorized()

    def _app_allowed(self) -> bool:
        return self._mode is Mode.VULNERABLE or self.is_authorized()

    def sensor_value(self) -> bytes:
        """Synthetic temperature in centi-degrees C, int16 LE, slow deterministic sine of uptime."""
        t = self._clock() - self._t0
        return struct.pack("<h", round(2150 + 120 * math.sin(2 * math.pi * t / 300.0)))

    # ----- reads -----
    def handle_read(self, name: str, offset: int = 0) -> ReadResult:
        spec = self._p.characteristics.get(name)
        if spec is None:
            return ReadResult(att_error=ATT_ATTRIBUTE_NOT_FOUND)
        if "read" not in spec.properties:
            return ReadResult(att_error=ATT_READ_NOT_PERMITTED)
        with self._lock:
            value = self._read_value(name, offset)
        if offset < 0 or offset > len(value):
            return ReadResult(att_error=ATT_INVALID_OFFSET)
        return ReadResult(value=value[offset:])

    def _read_value(self, name: str, offset: int) -> bytes:
        if name == "public_sensor":
            return self.sensor_value()
        if name in RESTRICTED_VALUES:
            if offset == 0:
                log.info("%s read: returned (link-level policy enforced by the OS)", name)
            return RESTRICTED_VALUES[name]
        if name == "device_info":
            return self._p.device_info
        if name == "virtual_led":
            return bytes([self._led])
        if name == "diag_status":
            return DIAG_STATUS.pack(self._last_status, self._last_seq)
        if name == "app_auth":
            if offset == 0 or self._auth_snapshot is None:
                self._issue_challenge()
            assert self._auth_snapshot is not None
            return self._auth_snapshot
        if name == "diag_manifest":
            allowed = self._app_allowed()
            if offset == 0:
                self._log_access("diag_manifest read", allowed)
            return self._manifest if allowed else APP_DENIAL
        if name == "chunk_data":
            return self._chunk_data_value(log_it=offset == 0)
        raise KeyError(name)

    def _log_access(self, what: str, allowed: bool) -> None:
        if not allowed:
            log.info("%s: denied (application authorization required)", what)
        elif self.is_authorized():
            log.info("%s: returned (application authorized)", what)
        else:
            log.info("%s: returned WITHOUT application authorization (vulnerable mode)", what)

    def _issue_challenge(self) -> None:
        self._challenge = bytes(self._random(self._p.challenge_bytes))
        if len(self._challenge) != self._p.challenge_bytes:
            raise RuntimeError("random source returned the wrong number of bytes")
        self._challenge_expires = self._clock() + self._p.challenge_ttl_s
        state = 1 if self.is_authorized() else 0
        self._auth_snapshot = bytes([state]) + self._challenge
        log.info("app_auth: challenge issued (state=%d)", state)

    def _chunk_data_value(self, *, log_it: bool) -> bytes:
        r = self._response
        if not self._app_allowed():
            seq, off = (r.seq, r.offset) if r else (self._last_seq, 0)
            if log_it:
                log.info("chunk_data read: denied (application authorization required)")
            return CHUNK_HEADER.pack(self._st["not_authorized"], seq, off, 0)
        if r is None:
            return CHUNK_HEADER.pack(self._st["no_request"], self._last_seq, 0, 0)
        if r.status != self._st["ok"]:
            return CHUNK_HEADER.pack(r.status, r.seq, r.offset, 0)
        if log_it:
            self._log_access(f"chunk_data read seq={r.seq} offset={r.offset} len={r.length}", True)
        payload = self._obj[r.offset : r.offset + r.length]
        return CHUNK_HEADER.pack(r.status, r.seq, r.offset, len(payload)) + payload

    # ----- writes -----
    def handle_write(self, name: str, data: bytes, offset: int = 0, mtu: int | None = None) -> WriteResult:
        """Process a write. ``mtu`` is the session ATT_MTU when the stack reports it."""
        spec = self._p.characteristics.get(name)
        if spec is None:
            return WriteResult(ATT_ATTRIBUTE_NOT_FOUND)
        if "write" not in spec.properties:
            return WriteResult(ATT_WRITE_NOT_PERMITTED)
        data = bytes(data)
        with self._lock:
            if name == "virtual_led":
                if offset != 0:
                    return WriteResult(ATT_INVALID_OFFSET)
                return self._write_led(data)
            expected = self._p.response_bytes if name == "app_auth" else CTRL.size
            value, err = self._assemble(name, data, offset, expected, mtu)
            if err is not None or value is None:
                return WriteResult(err)
            if name == "app_auth":
                return self._write_app_auth(value)
            if name == "chunk_ctrl":
                return self._write_chunk_ctrl(value, mtu)
        return WriteResult(ATT_WRITE_NOT_PERMITTED)

    def _assemble(
        self, name: str, data: bytes, offset: int, expected: int, mtu: int | None = None
    ) -> tuple[bytes | None, int | None]:
        held = self._pending.pop(name, None)
        if offset == 0:
            buf = bytearray(data)
            # A value that fits one ATT write (always for chunk_ctrl; for app_auth once the session MTU
            # allows it) is never sent in parts: a short plain write is an invalid length, not a start.
            fits_one_write = expected <= ATT_MIN_WRITE_PAYLOAD or (mtu is not None and mtu - 3 >= expected)
            if len(buf) < expected and fits_one_write:
                return None, ATT_INVALID_ATTRIBUTE_VALUE_LENGTH
        elif held is not None and offset == len(held):
            buf = held + data
        else:
            return None, ATT_INVALID_OFFSET
        if len(buf) > expected:
            return None, ATT_INVALID_ATTRIBUTE_VALUE_LENGTH
        if len(buf) < expected:
            self._pending[name] = buf  # first part of a long write; wait for the rest
            return None, None
        return bytes(buf), None

    def _write_app_auth(self, response: bytes) -> WriteResult:
        challenge, expires = self._challenge, self._challenge_expires
        self._challenge = None  # single use
        self._auth_snapshot = None
        now = self._clock()
        if challenge is None:
            log.info("app_auth: response rejected (no outstanding challenge)")
        elif now >= expires:
            log.info("app_auth: response rejected (challenge expired)")
        elif hmac.compare_digest(auth_response(self._key, challenge), response):
            self._authorized_until = now + self._p.auth_ttl_s
            log.info("app_auth: authorized for %d s", self._p.auth_ttl_s)
        else:
            log.info("app_auth: response rejected (mismatch)")
        return WriteResult()

    def _set_response(self, status: int, seq: int, offset: int, length: int = 0) -> None:
        self._response = _Response(status, seq, offset, length)
        self._last_status, self._last_seq = status, seq

    def _write_chunk_ctrl(self, raw: bytes, mtu: int | None) -> WriteResult:
        op, tag, offset, length, seq = CTRL.unpack(raw)
        st = self._st
        if not self._app_allowed():
            self._set_response(st["not_authorized"], seq, offset)
            log.info("chunk_ctrl op=%d seq=%d: denied (application authorization required)", op, seq)
            return WriteResult()
        if op == self._ops["request"]:
            limit = self._chunk_max if mtu is None else chunk_len_for_mtu(self._chunk_max, mtu)
            if tag != self._tag:
                status, n = st["stale_object"], 0
            elif length == 0 or length > limit:
                status, n = st["bad_request"], 0
            elif offset >= len(self._obj):
                status, n = st["out_of_range"], 0
            else:
                status, n = st["ok"], min(length, len(self._obj) - offset)
            self._set_response(status, seq, offset, n)
            log.info(
                "chunk_ctrl request seq=%d offset=%d len=%d limit=%d: status=%d",
                seq, offset, length, limit, status,
            )
        elif op == self._ops["cancel"]:
            self._set_response(st["cancelled"], seq, offset)
            log.info("chunk_ctrl cancel seq=%d", seq)
        elif op == self._ops["reset"]:
            self._response = None
            self._last_status, self._last_seq = st["no_request"], 0
            log.info("chunk_ctrl reset: transfer state cleared")
        else:
            self._set_response(st["bad_request"], seq, offset)
            log.info("chunk_ctrl unknown op=%d seq=%d: bad_request", op, seq)
        return WriteResult()

    def _write_led(self, data: bytes) -> WriteResult:
        if len(data) != 1:
            return WriteResult(ATT_INVALID_ATTRIBUTE_VALUE_LENGTH)
        if not self.is_authorized():  # enforced in every mode
            log.info("virtual_led write: denied (application authorization required)")
            return WriteResult(ATT_INSUFFICIENT_AUTHORIZATION)
        if data[0] not in self._led_allowed:
            log.info("virtual_led write: value %d not allowed", data[0])
            return WriteResult(ATT_VALUE_NOT_ALLOWED)
        old, self._led = self._led, data[0]
        log.info("virtual_led write: %d -> %d", old, self._led)
        return WriteResult()
