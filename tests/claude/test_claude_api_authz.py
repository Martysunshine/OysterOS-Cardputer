"""Authorization matrix for every Claude Code link route, listener separation, input bounds and rate limits.

The hook side (create) is owner-only and never served on the LAN listener; the device side can list and
decide but cannot create. Owner calls that would start a long poll are only exercised with bad credentials
here (they are rejected before anything is created); the allowed paths are covered in test_claude_flow."""

from __future__ import annotations

import pytest
from oyster.claude.service import ClaudeConfig

APPR = "appr-0123456789abcdef"

# (name, method, path, body)
ROUTES = [
    ("list", "GET", "/v1/claude/approvals", None),
    (
        "decide",
        "POST",
        f"/v1/claude/approvals/{APPR}/decide",
        {"decision": "allow", "nonce": "nonce-00000001-abcdef"},
    ),
    ("get_status", "GET", "/v1/claude/status", None),
    (
        "create",
        "POST",
        "/v1/claude/approvals",
        {"session": "s", "tool": "Bash", "summary": "ls", "wait_s": 5},
    ),
    ("post_status", "POST", "/v1/claude/status", {"session": "s", "model": "Opus", "cost_usd": 0.1}),
    ("audit", "GET", "/v1/claude/audit", None),
]
DEVICE_ONLY = {"list", "decide"}
OWNER_ONLY = {"create", "post_status", "audit"}
ANY = {"get_status"}


def call(client, route, headers):
    _, method, path, body = route
    kw = {"json": body} if body is not None else {}
    return client.request(method, path, headers=headers or {}, **kw)


@pytest.mark.parametrize("route", ROUTES, ids=[r[0] for r in ROUTES])
def test_missing_token_is_401_on_every_route(kit, route):
    r = call(kit.client, route, None)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert r.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("route", ROUTES, ids=[r[0] for r in ROUTES])
@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer not-a-valid-token-0123456789"},
        {"Authorization": "Basic Zm9vOmJhcg=="},
        {"Authorization": "Bearer "},
        {"Authorization": "bearer"},
    ],
)
def test_invalid_credentials_are_401_on_every_route(kit, route, header):
    r = call(kit.client, route, header)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


@pytest.mark.parametrize("route", [r for r in ROUTES if r[0] in DEVICE_ONLY], ids=lambda r: r[0])
def test_the_owner_token_is_403_on_device_only_routes(kit, route):
    r = call(kit.client, route, kit.owner)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


@pytest.mark.parametrize("route", [r for r in ROUTES if r[0] in OWNER_ONLY], ids=lambda r: r[0])
def test_a_device_token_is_403_on_owner_only_routes(kit, route):
    r = call(kit.client, route, kit.dev_a)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert kit.events() == []  # nothing was created or recorded


def test_a_device_cannot_create_an_approval_even_with_valid_input(kit):
    r = kit.client.post("/v1/claude/approvals", headers=kit.dev_a, json=kit.body(wait_s=5))
    assert r.status_code == 403
    assert kit.listing(kit.dev_a) == []


def test_the_owner_cannot_decide_an_approval(kit):
    w = kit.hook()
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    r = kit.client.post(
        f"/v1/claude/approvals/{item['approval_id']}/decide",
        headers=kit.owner,
        json={"decision": "allow", "nonce": item["nonce"]},
    )
    assert r.status_code == 403
    assert kit.service.poll(item["approval_id"]) == "pending"
    kit.clock.advance(100)
    assert w.result().json()["decision"] == "timeout"


def test_status_is_readable_by_a_device_and_by_the_owner(kit):
    assert kit.client.get("/v1/claude/status", headers=kit.dev_a).json() == {"configured": False}
    assert kit.client.get("/v1/claude/status", headers=kit.owner).json() == {"configured": False}


def test_a_rotated_device_token_stops_working(kit):
    old = kit.dev_a
    kit.enroll("dev-a")
    assert kit.client.get("/v1/claude/approvals", headers=old).status_code == 401


def test_owner_reads_work_for_the_owner(kit):
    assert kit.client.get("/v1/claude/audit", headers=kit.owner).json() == {
        "entries": [],
        "next_before": None,
    }
    r = kit.client.post("/v1/claude/status", headers=kit.owner, json=ROUTES[4][3])
    assert r.status_code == 200 and r.json() == {"stored": True}


# --- the LAN-facing listener --------------------------------------------------------------------------


def test_device_listener_serves_device_routes_only(kit):
    lan = kit.device_app()
    assert lan.get("/v1/claude/approvals", headers=kit.dev_a).status_code == 200
    assert lan.get("/v1/claude/status", headers=kit.dev_a).status_code == 200
    for method, path in (
        ("POST", "/v1/claude/approvals"),
        ("POST", "/v1/claude/status"),
        ("GET", "/v1/claude/audit"),
    ):
        r = lan.request(method, path, headers=kit.dev_a, json={} if method == "POST" else None)
        assert r.status_code in (404, 405), (method, path, r.status_code)


def test_device_listener_refuses_the_owner_token_everywhere(kit):
    lan = kit.device_app()
    for route in ROUTES:
        r = call(lan, route, kit.owner)
        assert r.status_code in (403, 404, 405), (route[0], r.status_code)
    assert lan.get("/v1/claude/approvals", headers=kit.owner).status_code == 403
    assert lan.get("/v1/claude/status", headers=kit.owner).status_code == 403


def test_a_decision_made_on_the_lan_listener_ends_the_long_poll_on_the_loopback_one(kit):
    lan = kit.device_app()
    w = kit.hook()
    kit.wait_for_pending()
    items = lan.get("/v1/claude/approvals", headers=kit.dev_a).json()["approvals"]
    r = lan.post(
        f"/v1/claude/approvals/{items[0]['approval_id']}/decide",
        headers=kit.dev_a,
        json={"decision": "allow", "nonce": items[0]["nonce"]},
    )
    assert r.status_code == 200
    assert w.result().json()["decision"] == "allow"


# --- input bounds -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"wait_s": 0},
        {"wait_s": 121},
        {"wait_s": -5},
        {"wait_s": True},
        {"wait_s": "90"},
        {"wait_s": 90.5},
        {"session": ""},
        {"session": "s" * 65},
        {"session": "has space"},
        {"session": "semi;colon"},
        {"tool": ""},
        {"tool": "t" * 41},
        {"tool": "bad tool"},
        {"summary": "x" * 161},
        {"summary": 5},
        {"extra": 1},
        {"decision": "allow"},
        {"approved": True},
        {"nonce": "n"},
    ],
)
def test_create_rejects_out_of_bounds_input(kit, patch):
    body = {**kit.body(), **patch}
    r = kit.client.post("/v1/claude/approvals", headers=kit.owner, json=body)
    assert r.status_code == 422 and r.json()["error"] == "validation_failed"
    assert kit.events() == []


def test_create_accepts_the_maximum_sizes(kit):
    w = kit.hook(summary="x" * 160, tool="t" * 40, session="s" * 64, wait_s=120)
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    assert len(item["tool"]) == 40 and item["summary"].endswith("...") and len(item["summary"]) == 80
    assert item["expires_in_s"] == 120
    kit.clock.advance(200)
    assert w.result().status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "maybe", "nonce": "nonce-00000001-abcdef"},
        {"decision": "allow", "nonce": "short"},
        {"decision": "allow", "nonce": "n" * 129},
        {"decision": "allow"},
        {"nonce": "nonce-00000001-abcdef"},
        {"decision": True, "nonce": "nonce-00000001-abcdef"},
        {"decision": "allow", "nonce": "nonce-00000001-abcdef", "approved": True},
        ["allow"],
        "allow",
    ],
)
def test_decide_rejects_malformed_bodies(kit, body):
    r = kit.client.post(f"/v1/claude/approvals/{APPR}/decide", headers=kit.dev_a, json=body)
    assert r.status_code == 422 and r.json()["error"] == "validation_failed"


def test_bodies_are_size_bounded(kit):
    big = kit.client.post("/v1/claude/approvals", headers=kit.owner, content=b"{" + b" " * 3000 + b"}")
    assert big.status_code == 413
    big = kit.client.post(f"/v1/claude/approvals/{APPR}/decide", headers=kit.dev_a, content=b" " * 5000)
    assert big.status_code == 413
    big = kit.client.post("/v1/claude/status", headers=kit.owner, content=b" " * 2000)
    assert big.status_code == 413
    junk = kit.client.post("/v1/claude/approvals", headers=kit.owner, content=b"not json")
    assert junk.status_code == 422


def test_an_unauthenticated_request_never_has_its_body_read(kit):
    r = kit.client.post("/v1/claude/approvals", content=b" " * 9000)
    assert r.status_code == 401


def test_error_bodies_are_uniform_and_generic(kit):
    w = kit.hook()
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    bad = kit.decide(kit.dev_a, item["approval_id"], "wrong-nonce-0000")
    unknown = kit.decide(kit.dev_a, "appr-ffffffffffffffff", item["nonce"])
    for r in (bad, unknown):
        assert set(r.json()) == {"error", "detail"}
    assert bad.status_code == 403 and unknown.status_code == 404
    assert item["nonce"] not in bad.text and "wrong-nonce-0000" not in bad.text
    kit.clock.advance(100)
    w.result()


# --- rate limits --------------------------------------------------------------------------------------


def test_decide_attempts_are_rate_limited_per_device(kit_factory):
    kit = kit_factory(ClaudeConfig(decides_per_minute=3, poll_interval_s=0.01))
    a, b = kit.enroll("dev-a"), kit.enroll("dev-b")
    codes = [kit.decide(a, APPR, "nonce-00000001-abcdef").status_code for _ in range(5)]
    assert codes == [404, 404, 404, 429, 429]
    r = kit.decide(a, APPR, "nonce-00000001-abcdef")
    assert r.json()["error"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1
    assert (
        kit.decide(b, APPR, "nonce-00000001-abcdef").status_code == 404
    )  # another device has its own budget
    kit.clock.advance(61)
    assert kit.decide(a, APPR, "nonce-00000001-abcdef").status_code == 404


def test_list_requests_are_rate_limited_per_device(kit_factory):
    kit = kit_factory(ClaudeConfig(lists_per_minute=2, poll_interval_s=0.01))
    a = kit.enroll("dev-a")
    codes = [kit.client.get("/v1/claude/approvals", headers=a).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_hook_requests_are_rate_limited(kit_factory):
    kit = kit_factory(ClaudeConfig(creates_per_minute=2, max_pending=10, poll_interval_s=0.01))
    waiters = [kit.hook(summary=f"c{i}", wait_s=5) for i in range(2)]
    kit.wait_for_pending(2)
    r = kit.client.post("/v1/claude/approvals", headers=kit.owner, json=kit.body())
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    kit.clock.advance(10)
    for w in waiters:
        assert w.result().json()["decision"] == "timeout"
