"""web_fetch: read one public web page as plain text (ADR-010).

Rules, enforced here: the target passes `safety.check_target` before the request and after every redirect
(at most MAX_REDIRECTS, followed by hand so each hop is re-checked); only text-like content types are read;
the download stops at MAX_DOWNLOAD_BYTES; one timeout covers connect, read and write. HTML is reduced to
readable text (scripts, styles and the other non-content elements removed) and clipped to `max_chars`.
Every failure is a result dict with an "error" code, never an exception, so a run can carry on.
"""

from __future__ import annotations

import logging
import re
import ssl
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
import truststore

from oyster.ai.redact import redact_text
from oyster.web import safety
from oyster.web.safety import MAX_DOWNLOAD_BYTES, MAX_REDIRECTS, Resolver

log = logging.getLogger("oyster.web")

USER_AGENT = "OysterPocket-backend/0.1 (+web_fetch; text only)"
ACCEPT = "text/html, application/xhtml+xml, text/plain;q=0.9, application/json;q=0.8, */*;q=0.1"
TEXT_TYPES = frozenset(
    {"application/xhtml+xml", "application/xml", "application/json", "application/ld+json"}
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_TITLE_CHARS = 200
# Elements whose content is not page text (the <title> inside <head> is read separately).
_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed"})
_BLOCK = frozenset(
    {
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "table",
        "section", "article", "header", "footer", "nav", "aside", "main", "blockquote", "pre", "dd", "dt",
        "dl", "figure", "figcaption", "hr", "address", "summary", "details", "body",
    }
)
_BLANKS = re.compile(r"[ \t\r\f\v]+")
_NEWLINES = re.compile(r"\s*\n\s*(?:\n\s*)+")


class _HideUrl(logging.Filter):
    """The HTTP library logs every request at INFO with its full URL. Fetched URLs are not logged (the
    run record keeps the domain); records that name a host we fetched from are dropped."""

    def __init__(self) -> None:
        super().__init__()
        self._hosts: set[str] = set()

    def add(self, url: str) -> None:
        host = urlsplit(url).netloc
        if host:
            self._hosts.add(host)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(h in message for h in self._hosts)


HIDE_URLS = _HideUrl()
logging.getLogger("httpx").addFilter(HIDE_URLS)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def tls_verify(transport: httpx.BaseTransport | None) -> ssl.SSLContext | bool:
    """OS trust store for real connections; a test transport needs no context."""
    return True if transport is not None else truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class _TextExtractor(HTMLParser):
    """HTML -> readable text: block elements separate lines; skipped elements contribute nothing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
            return
        if self._skip_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        return normalize_text("".join(self.parts))

    def page_title(self) -> str:
        return normalize_text(" ".join(self.title)).replace("\n", " ")[:MAX_TITLE_CHARS]


def normalize_text(text: str) -> str:
    """Strip control characters and credential-like strings, then collapse runs of blanks and blank lines."""
    text = redact_text(text, keep_long_hex=True)
    text = _BLANKS.sub(" ", text)
    return _NEWLINES.sub("\n", text).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """(title, readable text) of an HTML document. Tolerates broken markup."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:  # noqa: BLE001 - the stdlib parser raises on some malformed input; keep what it got
        log.debug("html parser stopped early: %s", type(e).__name__)
    return parser.page_title(), parser.text()


def _content_type(headers: httpx.Headers) -> tuple[str, str | None]:
    """(media type, charset) from the Content-Type header, lower-cased."""
    raw = headers.get("content-type", "")
    media, _, params = raw.partition(";")
    charset = None
    for p in params.split(";"):
        k, _, v = p.strip().partition("=")
        if k.strip().lower() == "charset" and v.strip():
            charset = v.strip().strip('"').lower()
    return media.strip().lower(), charset


def _is_text(media: str) -> bool:
    return media.startswith("text/") or media in TEXT_TYPES


def _decode(body: bytes, charset: str | None) -> str:
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return body.decode(enc, errors="replace" if enc == "utf-8" else "strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


class _TooLarge(Exception):
    pass


def _read_capped(resp: httpx.Response) -> bytes:
    length = resp.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
        raise _TooLarge
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > MAX_DOWNLOAD_BYTES:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


class PageFetcher:
    def __init__(
        self,
        *,
        timeout_s: float = 8.0,
        max_chars: int = 6000,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
    ):
        self.max_chars = max(200, int(max_chars))
        self.resolver = resolver
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": ACCEPT, "Accept-Language": "en"},
            timeout=httpx.Timeout(float(timeout_s)),
            follow_redirects=False,  # redirects are followed by hand so every hop is checked
            verify=tls_verify(transport),
            trust_env=False,  # no proxy from the environment: the address check must see the real target
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str) -> dict:
        """{url, domain, title, text, truncated} or {"error": code, "url"?: ..., "domain"?: ...}."""
        current = url.strip() if isinstance(url, str) else ""
        for _hop in range(MAX_REDIRECTS + 1):
            reason = safety.check_target(current, self.resolver)
            if reason:
                return {"error": reason, "domain": safety.domain_of(current)}
            HIDE_URLS.add(current)
            try:
                with self._client.stream("GET", current) as resp:
                    status = resp.status_code
                    if status in REDIRECT_STATUSES:
                        location = resp.headers.get("location")
                        if not location:
                            return {"error": f"http_{status}", "domain": safety.domain_of(current)}
                        current = urljoin(current, location)
                        continue
                    if status != 200:
                        return {"error": f"http_{status}", "domain": safety.domain_of(current)}
                    media, charset = _content_type(resp.headers)
                    if not _is_text(media):
                        return {"error": "unsupported_content_type", "domain": safety.domain_of(current)}
                    body = _read_capped(resp)
            except httpx.TimeoutException:
                return {"error": "timeout", "domain": safety.domain_of(current)}
            except _TooLarge:
                return {"error": "too_large", "domain": safety.domain_of(current)}
            except httpx.HTTPError as e:
                log.info("web_fetch failed: %s", type(e).__name__)
                return {"error": "unreachable", "domain": safety.domain_of(current)}
            return self._result(current, media, _decode(body, charset))
        return {"error": "too_many_redirects", "domain": safety.domain_of(current)}

    def _result(self, url: str, media: str, document: str) -> dict:
        if media in ("text/html", "application/xhtml+xml"):
            title, text = html_to_text(document)
        else:
            title, text = "", normalize_text(document)
        truncated = len(text) > self.max_chars
        return {
            "url": url[: safety.MAX_URL_CHARS],
            "domain": safety.domain_of(url),
            "title": title,
            "text": text[: self.max_chars],
            "truncated": truncated,
        }
