"""Home Assistant REST client (synchronous httpx): reads one entity state, calls one service.

    GET  /api/                          reachability and token check
    GET  /api/states/<entity_id>        {"state": "on", ...}
    POST /api/services/<domain>/<service>   {"entity_id": "<entity_id>"}  ->  list of changed states

Limits and safety: 5 s timeouts, no redirects (a redirect is an error, so the token is never sent to another
place), TLS verification on (optional HOME_ASSISTANT_CA), no proxy or CA settings taken from the process
environment, responses capped at 64 KiB. Every failure becomes a HomeError with a fixed text: the exception
text of the HTTP library can contain the URL, so it is never copied into an error, a log line or a response.
The token is only ever placed in the Authorization header.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from oyster.home.config import HomeConfig

log = logging.getLogger("oyster.home")

TIMEOUT_S = 5.0
MAX_BODY_BYTES = 64 * 1024
STATE_WORD = re.compile(r"[^a-z0-9_ .-]")


class _TooLarge(Exception):
    pass


class _HideHost(logging.Filter):
    """The HTTP library logs every request at INFO with its full URL, which would print the Home Assistant
    address. Records that mention it are dropped; the other records of that logger are untouched."""

    def __init__(self) -> None:
        super().__init__()
        self._needles: set[str] = set()

    def add(self, url: str) -> None:
        netloc = urlsplit(url).netloc
        self._needles |= {n for n in (url, netloc) if n}

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(n in message for n in self._needles)


_HIDE_HOST = _HideHost()
logging.getLogger("httpx").addFilter(_HIDE_HOST)
logging.getLogger("httpcore").setLevel(logging.WARNING)  # its DEBUG lines name the host and port


class HomeError(Exception):
    """An upstream failure with the HTTP status and error code the backend answers with."""

    def __init__(self, code: str, status: int, detail: str):
        super().__init__(detail)
        self.code = code
        self.status = status
        self.detail = detail

    @property
    def unknown_outcome(self) -> bool:
        """True when the request may have reached Home Assistant and been carried out."""
        return self.code == "home_timeout"


class EntityMissing(HomeError):
    """Home Assistant does not know the entity (it is in the allowlist but not in Home Assistant)."""

    def __init__(self) -> None:
        super().__init__("home_entity_missing", 502, "Home Assistant does not know this entity")


def clean_state(value: object) -> str:
    """A state word for the device screen: lowercase, [a-z0-9_ .-], at most 16 characters."""
    if not isinstance(value, str) or not value:
        return "unknown"
    return STATE_WORD.sub("?", value.lower())[:16]


class HomeAssistantClient:
    def __init__(self, cfg: HomeConfig, *, transport: httpx.BaseTransport | None = None):
        _HIDE_HOST.add(cfg.url)
        verify: ssl.SSLContext | bool = True
        if cfg.tls and cfg.ca_file:
            verify = ssl.create_default_context(cafile=cfg.ca_file)
        self._client = httpx.Client(
            base_url=cfg.url,
            headers={"Authorization": f"Bearer {cfg.token}", "Accept": "application/json"},
            timeout=httpx.Timeout(TIMEOUT_S),
            follow_redirects=False,
            verify=verify,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # ---- calls ---------------------------------------------------------------------------------------

    def ping(self) -> None:
        """Raises HomeError when Home Assistant cannot be reached or does not accept the token."""
        self._request("GET", "/api/")

    def get_state(self, entity_id: str) -> str:
        body = self._request("GET", f"/api/states/{quote(entity_id, safe='._')}", missing_ok=True)
        return clean_state(body.get("state") if isinstance(body, dict) else None)

    def call_service(self, domain: str, service: str, entity_id: str) -> str | None:
        """Calls domain.service for one entity. Returns the state Home Assistant reports for it in the answer
        (a list of the states that changed), or None when the answer does not mention it."""
        path = f"/api/services/{quote(domain, safe='_')}/{quote(service, safe='_')}"
        body = self._request("POST", path, payload={"entity_id": entity_id})
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict) and item.get("entity_id") == entity_id:
                    return clean_state(item.get("state"))
        return None

    # ---- transport -----------------------------------------------------------------------------------

    def _request(self, method: str, path: str, *, payload: Any = None, missing_ok: bool = False) -> Any:
        try:
            with self._client.stream(method, path, json=payload) as resp:
                status = resp.status_code
                raw = _read_capped(resp.iter_bytes())
        except httpx.TimeoutException:
            raise HomeError("home_timeout", 503, "Home Assistant did not answer in time") from None
        except httpx.HTTPError as e:
            log.warning("home assistant request failed: %s", type(e).__name__)
            tls = isinstance(e.__cause__, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(e)
            detail = (
                "Home Assistant's certificate was not accepted"
                if tls
                else "Home Assistant could not be reached"
            )
            raise HomeError("home_unreachable", 503, detail) from None
        except _TooLarge:
            raise HomeError("home_upstream_error", 502, "Home Assistant sent an oversized answer") from None
        if status in (401, 403):
            raise HomeError("home_auth_failed", 502, "Home Assistant did not accept the configured token")
        if status == 404 and missing_ok:
            raise EntityMissing
        if status in (400, 422):
            raise HomeError("home_refused", 502, "Home Assistant refused the request")
        if status not in (200, 201):
            raise HomeError("home_upstream_error", 502, "Home Assistant answered with an unexpected status")
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HomeError("home_upstream_error", 502, "Home Assistant sent an unreadable answer") from None


def _read_capped(chunks: Iterator[bytes]) -> bytes:
    buf = bytearray()
    for chunk in chunks:
        buf += chunk
        if len(buf) > MAX_BODY_BYTES:
            raise _TooLarge
    return bytes(buf)
