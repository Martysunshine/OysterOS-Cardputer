"""Redaction applied to everything sent to the AI provider.

Credentials, pairing passkeys, application secrets and unneeded raw payloads are withheld. Evidence ids,
lengths and statuses are preserved so the model can still reason about (and cite) withheld payloads.
Deterministic: the same input always yields the same output.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable

REDACTED = "[redacted]"
FULL_IDENTIFIERS_ENV = "AI_SEND_FULL_IDENTIFIERS"
_SECRET_ENV_NAME = re.compile(r"(_TOKEN|_KEY|_SECRET|_PASSWORD|_PASSPHRASE)$")
MIN_SECRET_LEN = 8

# Keys whose values are secret-like. Matched against the lower-cased key name.
_SECRET_KEY = re.compile(
    r"(passw|passphrase|passkey|psk|secret|token|api_?key|apikey|credential|hmac|challenge|"
    r"auth_response|private|bearer|nonce|^pin$|_pin$|^key$|_key$|cookie|session_key|ltk|irk|csrk)"
)
# Keys whose long hex values are digests/identifiers, not secrets.
_DIGEST_KEY = re.compile(r"(sha256|sha_256|hash|digest|uuid|bssid|address|_id$|^id$)")
# Raw payload keys never forwarded (lengths are kept by the summarizer instead).
_RAW_PAYLOAD_KEY = re.compile(
    r"(^raw$|_raw$|^raw_(?!att_status)|payload|mfg_data|manufacturer_data|service_data|"
    r"adv_hex|scan_rsp|^data_hex$|ssid_hex)"
)

_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
_API_KEY_LIKE = re.compile(r"\b(gsk|sk|pk|rk|xox[abp])[-_][A-Za-z0-9_-]{16,}\b")
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key.lower()))


def is_raw_payload_key(key: str) -> bool:
    return bool(_RAW_PAYLOAD_KEY.search(key.lower()))


def redact_text(text: str, extra_secrets: Iterable[str] = (), *, keep_long_hex: bool = False) -> str:
    """Remove credential-looking substrings and control characters from free text."""
    out = text
    for s in extra_secrets:
        if s and len(s) >= 6:
            out = out.replace(s, REDACTED)
    out = _PEM.sub(REDACTED, out)
    out = _BEARER.sub("Bearer " + REDACTED, out)
    out = _API_KEY_LIKE.sub(REDACTED, out)
    if not keep_long_hex:
        out = _LONG_HEX.sub(REDACTED, out)
    return _CTRL.sub(" ", out)


def clip(text: str, max_chars: int) -> str:
    """Bound a string; marks truncation explicitly so a clipped value never looks complete."""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 12)] + "...[clipped]"


def redact_value(key: str, value, extra_secrets: Iterable[str] = (), *, max_str: int = 64):
    """Redact one scalar value found under `key`."""
    if is_secret_key(key):
        if value is None or isinstance(value, bool):
            return value
        length = len(value) if isinstance(value, str | list | bytes) else None
        return {"withheld": "secret", "length": length}
    if isinstance(value, str):
        keep_hex = bool(_DIGEST_KEY.search(key.lower()))
        return clip(redact_text(value, extra_secrets, keep_long_hex=keep_hex), max_str)
    return value


def redact_tree(
    obj,
    extra_secrets: Iterable[str] = (),
    *,
    max_str: int = 64,
    max_items: int = 12,
    max_depth: int = 4,
    _depth: int = 0,
):
    """Recursively redact and bound a JSON-like tree (dict/list/scalars).

    Lists keep their first `max_items` items plus an explicit `_truncated` count; raw payload keys are
    replaced by their length; secret keys are withheld with their length; nesting deeper than
    `max_depth` collapses to a type marker.
    """
    extra = tuple(extra_secrets)
    if _depth > max_depth:
        return {"withheld": "depth"}
    if isinstance(obj, dict):
        out: dict = {}
        for k in sorted(obj.keys(), key=str):
            key = str(k)
            v = obj[k]
            if is_secret_key(key):
                out[key] = redact_value(key, v, extra, max_str=max_str)
            elif is_raw_payload_key(key) and isinstance(v, str | list | bytes | dict):
                out[key] = {
                    "withheld": "raw_payload",
                    "length": len(v) if isinstance(v, str | list | bytes) else None,
                }
            elif isinstance(v, dict | list):
                out[key] = redact_tree(
                    v, extra, max_str=max_str, max_items=max_items, max_depth=max_depth, _depth=_depth + 1
                )
            else:
                out[key] = redact_value(key, v, extra, max_str=max_str)
        return out
    if isinstance(obj, list):
        items = [
            redact_tree(
                v, extra, max_str=max_str, max_items=max_items, max_depth=max_depth, _depth=_depth + 1
            )
            for v in obj[:max_items]
        ]
        if len(obj) > max_items:
            items.append({"_truncated": len(obj) - max_items})
        return items
    if isinstance(obj, str):
        return clip(redact_text(obj, extra), max_str)
    return obj


def env_secrets(environ=None) -> tuple[str, ...]:
    """Values of every *_TOKEN / *_KEY / *_SECRET / *_PASSWORD environment variable (>= 8 chars).

    LAB_FIXTURE_AUTH_TOKEN, OYSTER_ADMIN_TOKEN, AI_API_KEY and any other credential-named variable are
    scrubbed from everything sent to the provider or stored in a run record.
    """
    env = os.environ if environ is None else environ
    vals = {
        v.strip()
        for k, v in env.items()
        if isinstance(v, str) and _SECRET_ENV_NAME.search(k.upper()) and len(v.strip()) >= MIN_SECRET_LEN
    }
    return tuple(sorted(vals))


def send_full_identifiers(environ=None) -> bool:
    """AI_SEND_FULL_IDENTIFIERS=true opts out of identifier masking (default: mask)."""
    env = os.environ if environ is None else environ
    return str(env.get(FULL_IDENTIFIERS_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


_MAC = re.compile(
    r"(?<![0-9A-Fa-f:-])([0-9A-Fa-f]{2})([:-])([0-9A-Fa-f]{2})\2([0-9A-Fa-f]{2})(?:\2[0-9A-Fa-f]{2}){3}(?![0-9A-Fa-f:-])"
)


class IdentifierMasker:
    """Masks neighbouring Wi-Fi BSSIDs and BLE addresses before text reaches the provider.

    `aa:bb:cc:dd:ee:ff` becomes `aa:bb:cc:xx:xx:xx#ap-1f3a9` (OUI kept, last three octets withheld) with a
    stable alias derived from sha256(session_id | normalised address): the same address keeps the same
    alias in every request and run of the session, so the model can still refer to it. `ap-` is used when
    the text just before the address names a bssid, `dev-` otherwise. Deterministic and stateless.
    """

    def __init__(self, session_id: str, *, enabled: bool = True):
        self.session_id = session_id
        self.enabled = enabled

    def alias(self, address: str, kind: str) -> str:
        norm = re.sub(r"[^0-9a-f]", "", address.lower())
        h = hashlib.sha256(f"{self.session_id}|{norm}".encode()).hexdigest()
        return f"{kind}-{h[:5]}"

    def mask_text(self, text: str) -> str:
        if not self.enabled or not text:
            return text

        def repl(m: re.Match) -> str:
            full = m.group(0)
            sep = m.group(2)
            before = text[max(0, m.start() - 24) : m.start()].lower()
            kind = "ap" if "bssid" in before or "ap_" in before else "dev"
            head = sep.join((m.group(1), m.group(3), m.group(4)))
            return f"{head}{sep}xx{sep}xx{sep}xx#{self.alias(full, kind)}"

        return _MAC.sub(repl, text)
