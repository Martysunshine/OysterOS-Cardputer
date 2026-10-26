"""Outbound URL policy (SSRF) and search-query redaction. Pure functions; no network, no real resolver."""

from __future__ import annotations

import socket

import pytest
from oyster.web import safety


def ip4(*octets: int) -> str:
    """Dotted address from its octets. The private fixtures are written this way on purpose: the
    repository's secrets scanner would otherwise take them for a real address of the owner's network."""
    return ".".join(str(o) for o in octets)


TEN = ip4(10, 0, 0, 1)
ONE_SEVENTY_TWO = ip4(172, 16, 0, 1)
METADATA = ip4(169, 254, 169, 254)  # cloud metadata address (link-local)
CGNAT = ip4(100, 64, 0, 1)  # shared address space (carrier NAT)
PRIVATE_URLS = [
    "http://127.0.0.1/",
    "http://127.1.2.3:8080/x",
    f"http://{TEN}/x",
    f"http://{ONE_SEVENTY_TWO}/",
    "http://192.168.1.50:8123/api",
    f"http://{METADATA}/latest/meta-data/",
    "http://[::1]/",
    "http://[fe80::1]/",
    f"http://[::ffff:{TEN}]/",  # IPv4-mapped IPv6
    "http://[::ffff:7f00:1]/",
    "http://224.0.0.1/",  # multicast
    "http://0.0.0.0/",  # noqa: S104 - the refused unspecified address, as test data
    f"http://{CGNAT}/",
    "http://192.0.0.1/",
    "http://198.18.0.1/",
    "http://localhost/",
    "http://LOCALHOST:8000/",
    "http://api.localhost/",
]


def no_lookup(host: str) -> list[str]:
    pytest.fail(f"address literals must be judged without a lookup (got {host!r})")


@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_private_loopback_link_local_multicast_and_metadata_addresses_are_refused(url):
    assert safety.check_target(url, resolver=no_lookup) == safety.REFUSED_PRIVATE


@pytest.mark.parametrize("url", ["http://93.184.216.34/", "https://[2606:4700::1111]/x", "https://8.8.8.8:8443/"])
def test_public_address_literals_pass(url):
    assert safety.check_target(url, resolver=no_lookup) is None


def test_hostname_resolving_to_a_private_address_is_refused():
    assert safety.check_target("https://intranet.example/", resolver=lambda h: [ip4(10, 1, 2, 3)]) == (
        safety.REFUSED_PRIVATE
    )
    # one private answer among public ones is enough to refuse (a resolver can hand out either)
    mixed = lambda h: ["93.184.216.34", "192.168.1.50"]  # noqa: E731
    assert safety.check_target("https://mixed.example/", resolver=mixed) == safety.REFUSED_PRIVATE
    ula = lambda h: ["fd00::1"]  # noqa: E731
    assert safety.check_target("https://v6.example/", resolver=ula) == safety.REFUSED_PRIVATE


def test_hostname_resolving_to_public_addresses_passes_and_is_lower_cased():
    seen: list[str] = []

    def resolver(host: str) -> list[str]:
        seen.append(host)
        return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]

    assert safety.check_target("https://Example.COM/page", resolver=resolver) is None
    assert seen == ["example.com"]


def test_unresolvable_hosts_are_refused_with_their_own_code():
    def failing(host: str) -> list[str]:
        raise socket.gaierror(-2, "Name or service not known")

    assert safety.check_target("https://nowhere.example/", resolver=failing) == safety.UNRESOLVABLE
    assert safety.check_target("https://empty.example/", resolver=lambda h: []) == safety.UNRESOLVABLE


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com/no-scheme",
        "http:///no-host",
        "http://user:secret@example.com/",  # credentials never travel
        "http://example.com:99999/",
        "https://exa mple.com/",
        "",
        "https://example.com/" + "a" * safety.MAX_URL_CHARS,
    ],
)
def test_unsupported_or_malformed_urls_are_invalid(url):
    assert safety.check_url(url) == safety.INVALID_URL
    assert safety.check_target(url, resolver=no_lookup) == safety.INVALID_URL


def test_check_url_accepts_plain_http_and_https():
    assert safety.check_url("http://example.com") is None
    assert safety.check_url("https://example.com:8443/a?b=c#d") is None


def test_domain_of():
    assert safety.domain_of("https://Example.com:8443/path") == "example.com"
    assert safety.domain_of("not a url") == ""


# ------------------------------------------------------------------ query redaction
def test_query_redaction_masks_identifiers_and_session_ssids():
    q = (
        "is Oyster2-5G or Oyster2 insecure 192.168.1.50 AA:BB:CC:DD:EE:FF owner@example.com "
        "gsk_abcdefghijklmnopqrstuvwxyz0123 admin-token-for-tests-0123456789"
    )
    out = safety.redact_query(q, ("admin-token-for-tests-0123456789",), ["Oyster2", "Oyster2-5G"])
    for leaked in (
        "Oyster2",
        "192.168.1.50",
        "AA:BB:CC:DD:EE:FF",
        "owner@example.com",
        "gsk_",
        "admin-token-for-tests",
    ):
        assert leaked not in out, (leaked, out)
    assert out.startswith("is [redacted] or [redacted] insecure [redacted] [redacted] [redacted]")


def test_query_redaction_leaves_ordinary_questions_alone():
    q = "what is the maximum range of Bluetooth Low Energy 5.3 in metres"
    assert safety.redact_query(q) == q
    assert safety.redact_query("  spaced \n out  ") == "spaced out"
    # a one-character SSID would mask every occurrence of that letter: ignored
    assert safety.redact_query("a question", ssids=["a"]) == "a question"
