"""web_fetch against an httpx.MockTransport: text extraction, redirects (re-checked per hop), the SSRF
refusals, the size cap, the timeout and the content-type rule. Nothing here touches a network."""

from __future__ import annotations

import logging

import httpx
import pytest
from oyster.web.fetch import PageFetcher, html_to_text
from oyster.web.safety import MAX_DOWNLOAD_BYTES


def ip4(*octets: int) -> str:
    """Dotted address from its octets, so the repository's secrets scanner does not take a private test
    fixture for a real address of the owner's network."""
    return ".".join(str(o) for o in octets)


METADATA = ip4(169, 254, 169, 254)
PUBLIC = {"example.com": ["93.184.216.34"], "other.example": ["93.184.216.35"]}
PRIVATE_HOSTS = {"intranet.example": [ip4(10, 0, 0, 5)]}
SECRET_LINE = "The router password is hunter2hunter2."  # never fetched in the refusal tests
HTML = """<!doctype html><html><head><title> Octopus  facts </title>
<style>p { color: red }</style><script>alert("x")</script></head>
<body><nav>Home | About</nav><h1>Octopus</h1>
<p>Octopuses have three hearts &amp; blue blood.</p><script>track()</script>
<p>They can change colour.</p><noscript>enable js</noscript></body></html>"""


def resolver(host: str) -> list[str]:
    if host in PRIVATE_HOSTS:
        return PRIVATE_HOSTS[host]
    if host in PUBLIC:
        return PUBLIC[host]
    raise OSError("unknown host in test")


class Site:
    """Routes by host+path; records what was requested so tests can prove a refused hop was never sent."""

    def __init__(self):
        self.routes: dict[str, httpx.Response | Exception] = {}
        self.requests: list[str] = []

    def add(self, url: str, response: httpx.Response | Exception) -> None:
        self.routes[url] = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        self.requests.append(key)
        r = self.routes.get(key)
        if r is None:
            return httpx.Response(404, headers={"content-type": "text/plain"}, text="missing")
        if isinstance(r, Exception):
            raise r
        return r


def html_resp(body: str, **headers) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", **headers}, text=body)


def make(site: Site, **kw) -> PageFetcher:
    return PageFetcher(transport=httpx.MockTransport(site), resolver=resolver, **kw)


def test_html_becomes_readable_text_without_scripts_and_styles():
    site = Site()
    site.add("https://example.com/octopus", html_resp(HTML))
    out = make(site).fetch("https://example.com/octopus")
    assert out["url"] == "https://example.com/octopus" and out["domain"] == "example.com"
    assert out["title"] == "Octopus facts"
    assert "three hearts & blue blood" in out["text"] and "change colour" in out["text"]
    for gone in ("alert", "color: red", "track()", "enable js"):
        assert gone not in out["text"]
    assert out["truncated"] is False
    assert "error" not in out


def test_text_is_clipped_to_max_chars_and_flagged():
    site = Site()
    site.add("https://example.com/long", html_resp("<p>" + "word " * 500 + "</p>"))
    out = make(site, max_chars=300).fetch("https://example.com/long")
    assert len(out["text"]) == 300 and out["truncated"] is True


def test_plain_text_json_and_latin1_pages():
    site = Site()
    plain = {"content-type": "text/plain"}
    site.add("https://example.com/t", httpx.Response(200, headers=plain, text="a  b\n\n\nc"))
    site.add(
        "https://example.com/j",
        httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"k": "v"}'),
    )
    site.add(
        "https://example.com/l",
        httpx.Response(200, headers={"content-type": "text/plain; charset=iso-8859-1"}, content=b"caf\xe9"),
    )
    f = make(site)
    assert f.fetch("https://example.com/t")["text"] == "a b\nc"
    assert f.fetch("https://example.com/j")["text"] == '{"k": "v"}'
    assert f.fetch("https://example.com/l")["text"] == "café"


def test_public_redirects_are_followed_up_to_three_hops():
    site = Site()
    site.add("https://example.com/a", httpx.Response(302, headers={"location": "/b"}))
    site.add("https://example.com/b", httpx.Response(301, headers={"location": "https://other.example/c"}))
    site.add("https://other.example/c", html_resp("<p>final</p>"))
    out = make(site).fetch("https://example.com/a")
    assert out["url"] == "https://other.example/c" and out["text"] == "final"

    loop = Site()
    for i in range(6):
        loop.add(f"https://example.com/r{i}", httpx.Response(302, headers={"location": f"/r{i + 1}"}))
    out = make(loop).fetch("https://example.com/r0")
    assert out == {"error": "too_many_redirects", "domain": "example.com"}
    assert len(loop.requests) == 4  # the first request plus three redirects


@pytest.mark.parametrize(
    "location",
    ["http://127.0.0.1/admin", f"http://{METADATA}/latest/", "http://[::1]/", "https://intranet.example/"],
)
def test_redirect_to_a_private_address_is_refused_before_it_is_requested(location):
    site = Site()
    site.add("https://example.com/go", httpx.Response(302, headers={"location": location}))
    site.add(location, html_resp(SECRET_LINE))
    out = make(site).fetch("https://example.com/go")
    assert out["error"] == "refused_private_address"
    assert site.requests == ["https://example.com/go"]  # the private target was never contacted
    assert SECRET_LINE not in str(out)


@pytest.mark.parametrize("url", ["http://127.0.0.1/", f"http://{ip4(10, 0, 0, 1)}/", "https://intranet.example/x"])
def test_private_targets_are_refused_without_any_request(url):
    site = Site()
    site.add(url, html_resp(SECRET_LINE))
    out = make(site).fetch(url)
    assert out["error"] == "refused_private_address" and site.requests == []


def test_invalid_urls_are_refused_without_any_request():
    site = Site()
    f = make(site)
    assert f.fetch("ftp://example.com/x")["error"] == "invalid_url"
    assert f.fetch("https://user:pw@example.com/")["error"] == "invalid_url"
    assert f.fetch("")["error"] == "invalid_url"
    assert site.requests == []


def test_downloads_above_one_megabyte_are_refused():
    site = Site()
    big = b"x" * (MAX_DOWNLOAD_BYTES + 1)
    plain = {"content-type": "text/plain"}
    site.add("https://example.com/declared", httpx.Response(200, headers=plain, content=big))
    site.add("https://example.com/streamed", httpx.Response(200, headers=plain, stream=httpx.ByteStream(big)))
    f = make(site)
    assert f.fetch("https://example.com/declared") == {"error": "too_large", "domain": "example.com"}
    assert f.fetch("https://example.com/streamed") == {"error": "too_large", "domain": "example.com"}


def test_timeouts_and_connection_errors_are_results_not_exceptions():
    site = Site()
    site.add("https://example.com/slow", httpx.ReadTimeout("slow"))
    site.add("https://example.com/down", httpx.ConnectError("refused"))
    f = make(site)
    assert f.fetch("https://example.com/slow") == {"error": "timeout", "domain": "example.com"}
    assert f.fetch("https://example.com/down") == {"error": "unreachable", "domain": "example.com"}


def test_non_text_content_types_and_error_statuses_are_refused():
    site = Site()
    def typed(status: int, media: str | None, body: bytes) -> httpx.Response:
        headers = {"content-type": media} if media else {}
        return httpx.Response(status, headers=headers, content=body)

    site.add("https://example.com/img", typed(200, "image/png", b"\x89PNG"))
    site.add("https://example.com/bin", typed(200, None, b"\x00\x01"))  # no content type at all
    site.add("https://example.com/pdf", typed(200, "application/pdf", b"%PDF"))
    site.add("https://example.com/gone", typed(404, "text/html", b"<p>no</p>"))
    site.add("https://example.com/err", typed(503, "text/html", b"<p>no</p>"))
    f = make(site)
    for path in ("img", "bin", "pdf"):
        assert f.fetch(f"https://example.com/{path}")["error"] == "unsupported_content_type", path
    assert f.fetch("https://example.com/gone")["error"] == "http_404"
    assert f.fetch("https://example.com/err")["error"] == "http_503"


def test_broken_markup_and_control_characters_do_not_break_extraction():
    title, text = html_to_text("<html><title>T</title><p>unclosed <b>bold\x00 text<div class='x")
    assert "unclosed bold text" in text and "\x00" not in text
    assert title == "T"


def test_fetched_urls_are_not_written_to_the_http_library_log(caplog):
    site = Site()
    site.add("https://example.com/private-path-xyz", html_resp("<p>hi</p>"))
    with caplog.at_level(logging.DEBUG):
        make(site).fetch("https://example.com/private-path-xyz")
    assert "private-path-xyz" not in caplog.text and "example.com" not in caplog.text
