"""web_search through the Brave adapter against an httpx.MockTransport. The key must reach the provider in
the header and nowhere else; the query is never logged; failures are result dicts."""

from __future__ import annotations

import json
import logging

import httpx
from oyster.web import compact_result
from oyster.web.search import BraveSearch, make_provider, normalize_results

KEY = "fake-search-key-0123456789abcdefghij"  # must never appear in a result or a log line
QUERY = "unique-needle-octopus-hearts"
ANSWER = {
    "type": "search",
    "web": {
        "results": [
            {
                "title": "Octopus - facts",
                "url": "https://example.com/octopus",
                "description": "Three hearts.",
            },
            {"title": "Ignored", "url": "ftp://files.example/x", "description": "not http"},
            {"title": "Long", "url": "https://other.example/p?q=1", "description": "s " * 400},
            {"title": "Fourth", "url": "https://fourth.example/", "description": "d4"},
        ]
    },
}


class Api:
    def __init__(self, response: httpx.Response | Exception | None = None):
        self.response = response or httpx.Response(200, json=ANSWER)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make(api: Api, key: str = KEY) -> BraveSearch:
    return BraveSearch(key, timeout_s=3, transport=httpx.MockTransport(api))


def test_search_happy_path_sends_the_key_in_the_header_only():
    api = Api()
    out = make(api).search(QUERY, 3)
    assert out["provider"] == "brave" and "error" not in out
    assert [r["domain"] for r in out["results"]] == ["example.com", "other.example", "fourth.example"]
    first = out["results"][0]
    assert first == {
        "title": "Octopus - facts",
        "domain": "example.com",
        "url": "https://example.com/octopus",
        "snippet": "Three hearts.",
    }
    assert len(out["results"][1]["snippet"]) <= 300 and out["results"][1]["snippet"].endswith("...[clipped]")
    req = api.requests[0]
    assert req.headers["x-subscription-token"] == KEY
    assert req.url.host == "api.search.brave.com" and req.url.params["q"] == QUERY
    assert req.url.params["count"] == "3"
    assert KEY not in str(req.url)
    assert KEY not in json.dumps(out)


def test_count_is_clamped_and_the_query_is_bounded():
    api = Api()
    s = make(api)
    assert len(s.search(QUERY, 50)["results"]) == 3  # the fixture has three http(s) results
    assert api.requests[-1].url.params["count"] == "5"
    s.search("x" * 500, 0)
    assert api.requests[-1].url.params["count"] == "1"
    assert len(api.requests[-1].url.params["q"]) == 200
    assert s.search("   ", 3) == {"error": "empty_query", "provider": "brave"}


def test_missing_key_is_not_configured_and_sends_nothing():
    api = Api()
    assert make(api, key="").search(QUERY, 3) == {"error": "not_configured", "provider": "brave"}
    assert api.requests == []
    assert make_provider("unknown-provider", KEY, timeout_s=3) is None


def test_provider_failures_become_error_codes():
    cases = [
        (httpx.Response(429, json={"message": "rate"}), "http_429"),
        (httpx.Response(401, text="no"), "http_401"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("down"), "unreachable"),
        (
            httpx.Response(200, content=b"not json", headers={"content-type": "application/json"}),
            "bad_response",
        ),
    ]
    for response, code in cases:
        out = make(Api(response)).search(QUERY, 2)
        assert out == {"error": code, "provider": "brave"}, code
    # an answer without the expected shape is simply an empty result list
    assert make(Api(httpx.Response(200, json={"type": "search"}))).search(QUERY, 2)["results"] == []
    assert make(Api(httpx.Response(200, json=[1, 2]))).search(QUERY, 2)["results"] == []


def test_neither_key_nor_query_reach_the_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        make(Api()).search(QUERY, 3)
        make(Api(httpx.Response(500, text=KEY))).search(QUERY, 3)
    assert KEY not in caplog.text
    assert QUERY not in caplog.text
    assert "query of %d characters" in caplog.text or "query of 28 characters" in caplog.text


def test_normalize_results_ignores_junk_items():
    items = [
        None,
        "str",
        {"url": 5},
        {"url": "http://user:pw@example.com/"},
        {"url": "https://ok.example/", "title": None},
    ]
    out = normalize_results(items, 5)
    assert out == [{"title": "", "domain": "ok.example", "url": "https://ok.example/", "snippet": ""}]


def test_compact_form_keeps_title_and_domain_per_result():
    results = make(Api()).search(QUERY, 3)["results"]
    payload = {"kind": "web_search", "note": "n", "provider": "brave", "results": results}
    compact = compact_result("web_search", payload)
    assert compact["results"] == [
        {"title": "Octopus - facts", "domain": "example.com"},
        {"title": "Long", "domain": "other.example"},
        {"title": "Fourth", "domain": "fourth.example"},
    ]
    assert "note" not in compact and compact["provider"] == "brave"
    fetched = {
        "kind": "web_fetch",
        "note": "n",
        "url": "https://example.com/",
        "domain": "example.com",
        "title": "T",
        "text": "x" * 500,
        "truncated": False,
    }
    c = compact_result("web_fetch", fetched)
    assert c["text"] == "withheld to fit the input budget"
    assert c["title"] == "T" and c["domain"] == "example.com" and "note" not in c
    err = {"kind": "web_fetch", "error": "timeout", "domain": "example.com"}
    assert compact_result("web_fetch", err) == err
