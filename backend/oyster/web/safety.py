"""Safety rules for the assistant's web access (ADR-010).

Two concerns live here, both pure functions so they can be tested without a network:

- Outbound URL policy (SSRF): only http/https, no credentials in the URL, and the host must resolve to
  public addresses only. Loopback, private, link-local (169.254.x.x, which holds the cloud metadata
  address), multicast, unspecified, reserved and the other IANA special-purpose ranges are refused. The
  check is applied before the first request and again on every redirect target.
- Query redaction: a search query written by the model is sent to a third party, so credentials,
  e-mail addresses, IPv4 addresses, MAC addresses and the SSIDs seen in the session are masked first.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from oyster.ai.redact import REDACTED, redact_text

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_URL_CHARS = 2048
MAX_REDIRECTS = 3
MAX_DOWNLOAD_BYTES = 1024 * 1024

# Reasons returned by the checks; they become the "error" field of a tool result.
REFUSED_PRIVATE = "refused_private_address"
INVALID_URL = "invalid_url"
UNRESOLVABLE = "unresolvable_host"

# Names that always mean "this machine" (RFC 6761), refused without a lookup.
_LOCAL_NAMES = frozenset({"localhost", "localhost."})
# Host names and address literals as they appear after URL parsing (brackets removed); anything else, such
# as blanks or non-ASCII, is refused rather than guessed at (an internationalised name must be in punycode).
_HOST_OK = re.compile(r"^[A-Za-z0-9._:%-]{1,253}$")

Resolver = Callable[[str], list[str]]
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def default_resolver(host: str) -> list[str]:
    """Every address the OS resolver returns for `host` (A and AAAA), as strings."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def _unwrap(addr: Address) -> Address:
    """An IPv6 address that embeds an IPv4 one (::ffff:a.b.c.d, 6to4, Teredo) is judged by that IPv4."""
    if isinstance(addr, ipaddress.IPv6Address):
        for embedded in (addr.ipv4_mapped, addr.sixtofour, addr.teredo and addr.teredo[1]):
            if embedded is not None:
                return embedded
    return addr


def address_is_public(text: str) -> bool:
    """True only for a globally routable unicast address."""
    try:
        addr = _unwrap(ipaddress.ip_address(text.strip("[]").split("%", 1)[0]))
    except ValueError:
        return False
    return bool(addr.is_global) and not addr.is_multicast


def check_url(url: str) -> str | None:
    """Static URL policy. Returns a refusal code or None when the URL may be resolved."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        return INVALID_URL
    try:
        parts = urlsplit(url)
    except ValueError:
        return INVALID_URL
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
        return INVALID_URL
    if not _HOST_OK.match(parts.hostname):
        return INVALID_URL
    if parts.username is not None or parts.password is not None:
        return INVALID_URL  # credentials in a URL are never forwarded anywhere
    try:
        if parts.port is not None and not (1 <= parts.port <= 65535):
            return INVALID_URL
    except ValueError:
        return INVALID_URL
    return None


def check_host(host: str, resolver: Resolver | None = None) -> str | None:
    """Resolve `host` and refuse it unless every address is public. Returns a refusal code or None."""
    name = host.strip("[]").lower()
    if name in _LOCAL_NAMES or name.endswith(".localhost"):
        return REFUSED_PRIVATE
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        pass
    else:
        return None if address_is_public(name) else REFUSED_PRIVATE
    try:
        addresses = (resolver or default_resolver)(name)
    except (OSError, UnicodeError, ValueError):
        return UNRESOLVABLE
    if not addresses:
        return UNRESOLVABLE
    if not all(address_is_public(a) for a in addresses):
        return REFUSED_PRIVATE
    return None


def check_target(url: str, resolver: Resolver | None = None) -> str | None:
    """URL policy plus host resolution; the one check used before a request and after each redirect."""
    reason = check_url(url)
    if reason:
        return reason
    return check_host(urlsplit(url).hostname or "", resolver)


def domain_of(url: str) -> str:
    """The host part of a URL for display (lower-cased, no port, at most 100 characters)."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return host.lower()[:100]


# --- query redaction -------------------------------------------------------------------------------
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"(?<![0-9.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![0-9.])")
_MAC = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
_SPACE = re.compile(r"\s+")
MIN_SSID_CHARS = 2


def redact_query(query: str, secrets: Iterable[str] = (), ssids: Iterable[str] = ()) -> str:
    """Mask credentials, e-mail addresses, IPv4 addresses, MAC addresses and the session's SSIDs."""
    out = redact_text(query or "", secrets)
    names = {s for s in ssids if isinstance(s, str) and len(s) >= MIN_SSID_CHARS}
    for ssid in sorted(names, key=len, reverse=True):  # longest first: "Oyster2-5G" before "Oyster2"
        out = out.replace(ssid, REDACTED)
    out = _EMAIL.sub(REDACTED, out)
    out = _MAC.sub(REDACTED, out)
    out = _IPV4.sub(REDACTED, out)
    return _SPACE.sub(" ", out).strip()
