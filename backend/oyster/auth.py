"""Authentication: per-device bearer tokens (salted SHA-256 at rest) and the single owner token.

- Device token: secrets.token_urlsafe(32), returned once at enrolment, stored as (salt, sha256(salt||token)).
- Owner: OYSTER_ADMIN_TOKEN. An empty admin token disables owner access entirely (never matches).
- Missing/invalid token -> 401. Valid token of the wrong role -> 403. Comparisons use hmac.compare_digest.
- The LAN-facing (device) listener never accepts the owner token: presenting it there is a 403 and counts as
  a failed authentication.
- More than 10 failed authentications from one client address within a minute block that address for 60 s
  (429 with Retry-After); the counters live in memory.
Tokens are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request

from oyster.api.errors import ApiError

TOKEN_BYTES = 32
SALT_BYTES = 16
MAX_TOKEN_LEN = 512  # one bound for every bearer token (device tokens are 43 chars, the admin token <= this)
MIN_ADMIN_TOKEN_LEN = 24
ADMIN_TOKEN_PLACEHOLDER = "change-me-to-a-long-random-string"  # noqa: S105 - the .env.example placeholder
_PLACEHOLDER_MARKERS = ("change-me", "changeme", "change_me", "replace-me", "placeholder", "your-token")
ADMIN_TOKEN_COMMAND = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'  # noqa: S105 - a shell command, not a secret


def admin_token_problem(token: str) -> str | None:
    """Why `token` may not be used as OYSTER_ADMIN_TOKEN (None when acceptable)."""
    if not token:
        return "is empty"
    if token == ADMIN_TOKEN_PLACEHOLDER or any(m in token.lower() for m in _PLACEHOLDER_MARKERS):
        return "is still a placeholder value"
    if len(token) < MIN_ADMIN_TOKEN_LEN:
        return f"is shorter than {MIN_ADMIN_TOKEN_LEN} characters"
    if len(token) > MAX_TOKEN_LEN:
        return f"is longer than {MAX_TOKEN_LEN} characters"
    if len(set(token)) < 8:
        return "has too little variety to be random"
    return None


def admin_destination_problem(base_url: str, allow_remote: bool = False) -> str | None:
    """Why the admin token must not be sent to `base_url` (None when it may be).

    Loopback hosts are always fine. Any other host needs --allow-remote-admin, and even then a plain
    http URL is sent in clear text (the caller should warn)."""
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return f"{base_url!r} is not an http(s) URL"
    if host == "localhost":
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    if allow_remote:
        return None
    where = "plain http to a non-loopback host" if parts.scheme == "http" else "a non-loopback host"
    return (
        f"refusing to send the admin token to {host} ({where}); the owner API is served on 127.0.0.1 only. "
        "Pass --allow-remote-admin to override"
    )


class AuthThrottle:
    """In-memory failed-authentication throttle per client address (clock injectable for tests)."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        *,
        max_failures: int = 10,
        window_s: float = 60.0,
        block_s: float = 60.0,
        max_clients: int = 4096,
    ):
        self._clock = clock
        self.max_failures = max_failures
        self.window_s = window_s
        self.block_s = block_s
        self.max_clients = max_clients
        self._failures: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> int | None:
        """Seconds until `client` may authenticate again, or None when it is not blocked."""
        with self._lock:
            until = self._blocked_until.get(client)
            if until is None:
                return None
            now = self._clock()
            if now >= until:
                del self._blocked_until[client]
                self._failures.pop(client, None)
                return None
            return max(1, int(until - now + 0.999))

    def record_failure(self, client: str) -> None:
        with self._lock:
            now = self._clock()
            q = self._failures.setdefault(client, deque())
            q.append(now)
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) > self.max_failures:
                self._blocked_until[client] = now + self.block_s
            if len(self._failures) > self.max_clients:
                self._prune(now)

    def _prune(self, now: float) -> None:
        for c in [c for c, q in self._failures.items() if not q or q[-1] <= now - self.window_s]:
            self._failures.pop(c, None)
        for c in [c for c, t in self._blocked_until.items() if t <= now]:
            self._blocked_until.pop(c, None)


def new_device_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str, salt: bytes | None = None) -> tuple[str, str]:
    """Return (salt_hex, sha256_hex) for storage."""
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.sha256(salt + token.encode("utf-8")).hexdigest()
    return salt.hex(), digest


def verify_token(token: str, salt_hex: str, digest_hex: str) -> bool:
    _, digest = hash_token(token, bytes.fromhex(salt_hex))
    return hmac.compare_digest(digest, digest_hex)


def is_owner_token(token: str | None, admin_token: str) -> bool:
    if not token or not admin_token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), admin_token.encode("utf-8"))


@dataclass(frozen=True)
class Principal:
    kind: str  # "owner" | "device"
    device_id: str | None = None
    simulated: bool = False

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"

    @property
    def scope(self) -> str | None:
        """device_id for store ownership checks; None means owner (sees everything)."""
        return None if self.is_owner else self.device_id


_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def bearer_token(request: Request) -> str | None:
    raw = request.headers.get("authorization", "")
    scheme, _, value = raw.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value or len(value) > MAX_TOKEN_LEN:
        return None
    return value


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def authenticate(request: Request) -> Principal | None:
    """The authenticated principal, or None (the caller answers 401). A blocked client gets 429; the owner
    token on the device listener gets 403."""
    state = request.app.state
    throttle: AuthThrottle | None = getattr(state, "auth_throttle", None)
    client = _client(request)
    if throttle is not None:
        wait = throttle.retry_after(client)
        if wait is not None:
            raise ApiError(
                429,
                "rate_limited",
                f"too many failed authentication attempts; retry in {wait} s",
                {"Retry-After": str(wait)},
            )
    token = bearer_token(request)
    if token is not None:
        if is_owner_token(token, state.settings.admin_token):
            if getattr(state, "audience", "loopback") == "device":
                if throttle is not None:
                    throttle.record_failure(client)
                raise ApiError(
                    403, "forbidden", "the owner token is not accepted on the device listener"
                )
            return Principal(kind="owner")
        dev = state.store.device_for_token(token)
        if dev is not None:
            return Principal(kind="device", device_id=dev.device_id, simulated=dev.simulated)
    if throttle is not None:
        throttle.record_failure(client)
    return None


def _unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", "missing or invalid bearer token", _UNAUTHORIZED_HEADERS)


# FastAPI dependencies (sync: the store is synchronous and thread-safe)
def require_owner(request: Request) -> Principal:
    p = authenticate(request)
    if p is None:
        raise _unauthorized()
    if not p.is_owner:
        raise ApiError(403, "forbidden", "owner token required")
    return p


def require_device(request: Request) -> Principal:
    p = authenticate(request)
    if p is None:
        raise _unauthorized()
    if p.is_owner:
        raise ApiError(403, "forbidden", "device token required")
    return p


def require_principal(request: Request) -> Principal:
    p = authenticate(request)
    if p is None:
        raise _unauthorized()
    return p
