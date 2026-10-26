"""web_search: one query to a search API, normalized to {results: [...], provider} (ADR-010).

`SearchProvider` is the small interface a provider implements; `BraveSearch` is the only one in this
version. The API key travels in a request header only; it is never logged, never part of a result and
never in an error text. The query is logged as its length only. Every failure is a result dict with an
"error" code, never an exception.

Brave Web Search API (as documented by the provider; not exercised against the live service in this
repository, the owner has no key yet): GET https://api.search.brave.com/res/v1/web/search?q=...&count=N
with header X-Subscription-Token; answers {"web": {"results": [{"title", "url", "description"}, ...]}}.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from oyster.ai.redact import clip, redact_text
from oyster.web import safety
from oyster.web.fetch import HIDE_URLS, tls_verify

log = logging.getLogger("oyster.web")

MAX_QUERY_CHARS = 200
MAX_COUNT = 5
DEFAULT_COUNT = 3
MAX_SNIPPET_CHARS = 300
MAX_TITLE_CHARS = 120
MAX_RESPONSE_BYTES = 512 * 1024
PROVIDERS = ("brave",)


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, count: int) -> dict:
        """{results: [{title, domain, url, snippet}], provider} or {"error": code, "provider": name}."""


def _clean(text: object, limit: int) -> str:
    plain = redact_text(str(text) if text is not None else "", keep_long_hex=True)
    return clip(" ".join(plain.split()), limit)


def normalize_results(items: list, count: int) -> list[dict]:
    """Keep http(s) results only, bounded and cleaned; nothing else from the provider's answer is kept."""
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or safety.check_url(url) is not None:
            continue
        out.append(
            {
                "title": _clean(item.get("title"), MAX_TITLE_CHARS),
                "domain": safety.domain_of(url),
                "url": url[: safety.MAX_URL_CHARS],
                "snippet": _clean(item.get("description") or item.get("snippet"), MAX_SNIPPET_CHARS),
            }
        )
        if len(out) >= count:
            break
    return out


class BraveSearch:
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, *, timeout_s: float = 8.0, transport: httpx.BaseTransport | None = None):
        self._key = api_key
        HIDE_URLS.add(self.endpoint)  # the HTTP library's request log line would print the query
        self._client = httpx.Client(
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
                "User-Agent": "OysterPocket-backend/0.1",
            },
            timeout=httpx.Timeout(float(timeout_s)),
            follow_redirects=False,
            verify=tls_verify(transport),
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def search(self, query: str, count: int) -> dict:
        if not self._key:
            return {"error": "not_configured", "provider": self.name}
        q = (query or "").strip()[:MAX_QUERY_CHARS]
        n = min(MAX_COUNT, max(1, int(count)))
        if not q:
            return {"error": "empty_query", "provider": self.name}
        log.info("web_search: query of %d characters, %d result(s) requested", len(q), n)
        try:
            resp = self._client.get(
                self.endpoint,
                params={"q": q, "count": n, "text_decorations": "false", "safesearch": "moderate"},
            )
        except httpx.TimeoutException:
            return {"error": "timeout", "provider": self.name}
        except httpx.HTTPError as e:
            log.info("web_search failed: %s", type(e).__name__)
            return {"error": "unreachable", "provider": self.name}
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}", "provider": self.name}
        if len(resp.content) > MAX_RESPONSE_BYTES:
            return {"error": "too_large", "provider": self.name}
        try:
            data = resp.json()
        except ValueError:
            return {"error": "bad_response", "provider": self.name}
        web = data.get("web") if isinstance(data, dict) else None
        items = web.get("results") if isinstance(web, dict) else None
        results = normalize_results(items if isinstance(items, list) else [], n)
        return {"results": results, "provider": self.name}


def make_provider(
    provider: str, api_key: str, *, timeout_s: float, transport: httpx.BaseTransport | None = None
) -> SearchProvider | None:
    """The configured provider, or None when the name is unknown (the tool then answers not_configured)."""
    if provider == "brave":
        return BraveSearch(api_key, timeout_s=timeout_s, transport=transport)
    return None
