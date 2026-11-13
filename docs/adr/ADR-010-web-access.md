[OysterOS](../../README.md) / [Docs](../README.md) / ADR-010

# ADR-010: Web access for the assistant (web_search, web_fetch)

**Status:** proposed (2026-10-26). Becomes accepted when a run with `AI_WEB_ACCESS=on` has been tried against
the real search provider. Until then the adapter is tested against a fake provider only (`tests/web/`,
`tests/ai/test_ai_web_tools.py`); the owner has no search API key yet.

**Decision.** Two backend tools in the AI catalogue, `web_search` (one query to a search API) and `web_fetch`
(one public page as plain text), implemented in `backend/oyster/web/`. They are **off by default** and are
offered to the model only when `backend/.env` says `AI_WEB_ACCESS=on`; calling them while off is refused like
any tool that is not offered. The one provider in this version is Brave Search (`WEB_SEARCH_PROVIDER=brave`,
`WEB_SEARCH_API_KEY`), behind a small `SearchProvider` interface so another can be added without touching the
orchestrator. Every call is one action of the run and one of `WEB_MAX_CALLS_PER_RUN` (default 4) web calls.

**Why off by default.** Everything else the assistant does stays on the PC and the LAN (ADR-002 sends only the
redacted session to the AI provider). A web search sends a model-written query to a third party and a fetch
contacts an arbitrary host; both are new data flows the owner must choose, not discover. The switch is a
single line the owner writes, and `docs/WEB_ACCESS.md` and `docs/PRIVACY.md` say what leaves the PC once it is
on. Nothing in the firmware or on the device changes; the device never sees these tools.

**Why the SSRF rules.** `web_fetch` takes a URL written by the model, and the model reads untrusted text
(search results, pages), so a page could steer it towards `http://127.0.0.1:8000/v1/...`, the Home
Assistant address or a cloud metadata endpoint. The backend is exactly the kind of host such a request
would reach. Therefore (`oyster/web/safety.py`): only http/https, no credentials in the URL; the host is
resolved and refused unless **every** address is globally routable (`ipaddress.is_global`, with IPv4 embedded
in IPv6 unwrapped first), which excludes loopback, private, link-local (169.254/16, which holds the cloud
metadata address), multicast, unspecified, shared address space and the other special ranges; `localhost` is refused by
name. Redirects are followed by hand so the same check runs on every hop (at most 3). Non-text content types
are refused, the download stops at 1 MB, one timeout covers the request, and the environment's proxy settings
are ignored so the check sees the real target. Residual risk, stated plainly: the address is checked with one
lookup and the HTTP library performs its own, so a resolver that answers differently on the second lookup
(DNS rebinding) is not stopped; pinning the connection to the checked address is a possible follow-up.

**Why the query is redacted.** The model composes the query from the conversation, which contains session
data. Before it leaves the PC the query passes the existing secret redaction plus masks for e-mail addresses,
IPv4 addresses, MAC addresses and every SSID observed in the session. The query is logged as its length only;
the key travels in a request header only and is scrubbed from everything stored or sent to the AI provider
(`WEB_SEARCH_API_KEY` ends in `_KEY`, so `redact.env_secrets` already covers it).

**Why the results are data.** Search results and page text go into the conversation only as JSON tool
results, like device evidence (same "untrusted data" rule in the system prompt, repeated in each result's
`note`). They never enter the system or user role. The existing final-answer rule (no links) stays; the model
names sources as plain `title (domain)` text, which the answer guard keeps. When the input budget is tight,
the compact form of a search result keeps title and domain of every result, so sources stay nameable.

**Provider choice.** Brave Search has a documented REST API with a plain key header and JSON results and no
account-wide side effects. The adapter is written from the provider's public documentation and has **not** been
exercised against the live service in this repository. The interface (`SearchProvider.search(query, count)`)
is the only thing the rest of the code depends on.

**Alternatives considered.**
- *Always on*: rejected; see "why off by default".
- *Let the model fetch any URL with the HTTP library's redirect following*: rejected; the redirect target would
  not be checked.
- *An allowlist of domains*: safer, but the use case is general questions, where an allowlist is either useless or
  too wide to be meaningful. The address rules plus the call budget were chosen instead.
- *Pinning the connection to the checked address*: deferred (complexity in TLS name verification and IPv6
  literals); noted as the residual risk above.

**Consequences.** New settings (`AI_WEB_ACCESS`, `WEB_SEARCH_PROVIDER`, `WEB_SEARCH_API_KEY`,
`WEB_FETCH_MAX_CHARS`, `WEB_TIMEOUT_S`, `WEB_MAX_CALLS_PER_RUN`), a new package with its own tests, two tool
names that the contract list `contracts.AI_TOOLS` does not include (they are kept in `ai/tools.py` as
`WEB_TOOLS`, because they are not device operations and the device never sees them). A web call that fails
is a clean error result (`not_configured`, `timeout`, `refused_private_address`, `http_<status>`,
`too_large`, ...), never an exception that ends the run.
