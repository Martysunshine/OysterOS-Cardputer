"""Authorization matrix for every Control Deck route, plus listener separation and install behaviour."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from oyster.deck.mount import install
from oyster.main import create_app

KEY = {"Idempotency-Key": "key-authz-0001"}

# (name, method, path, body) - every route; {job} is replaced by a job owned by dev-a
ROUTES = [
    ("list_profiles", "GET", "/v1/deck/profiles", None),
    ("run", "POST", "/v1/deck/profiles/dev/slots/1/run", None),
    ("confirm", "POST", "/v1/deck/jobs/{job}/confirm", "confirm"),
    ("cancel", "POST", "/v1/deck/jobs/{job}/cancel", None),
    ("get_job", "GET", "/v1/deck/jobs/{job}", None),
    ("history", "GET", "/v1/deck/history", None),
    ("put_profile", "PUT", "/v1/deck/profiles/dev", "profile"),
    ("get_profile", "GET", "/v1/deck/profiles/dev", None),
    ("delete_profile", "DELETE", "/v1/deck/profiles/nonexistent", None),
    ("list_jobs", "GET", "/v1/deck/jobs", None),
    ("audit", "GET", "/v1/deck/audit", None),
]
OWNER_ONLY = {"put_profile", "get_profile", "delete_profile", "list_jobs", "audit"}
DEVICE_ONLY = {"run", "confirm", "cancel"}


@pytest.fixture
def awaiting_job(kit):
    return kit.run(kit.dev_a, 3, "key-authz-job01").json()


def request(kit, route, headers, job, make_profile, client=None):
    name, method, path, body = route
    kw = {}
    if body == "confirm":
        kw["json"] = {"job_id": job, "nonce": "nonce-placeholder-0", "approved": False}
    elif body == "profile":
        kw["json"] = make_profile()
    h = dict(headers or {})
    if name == "run":
        h.update(KEY)
    return (client or kit.client).request(method, path.format(job=job), headers=h, **kw)


@pytest.mark.parametrize("route", ROUTES, ids=[r[0] for r in ROUTES])
def test_missing_token_is_401_on_every_route(kit, awaiting_job, make_profile, route):
    r = request(kit, route, None, awaiting_job["job_id"], make_profile)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert r.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("route", ROUTES, ids=[r[0] for r in ROUTES])
@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer not-a-valid-token-0123456789"},
        {"Authorization": "Basic Zm9vOmJhcg=="},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer " + "x" * 600},
    ],
    ids=["unknown", "wrong-scheme", "empty", "oversized"],
)
def test_invalid_token_is_401_on_every_route(kit, awaiting_job, make_profile, route, header):
    r = request(kit, route, header, awaiting_job["job_id"], make_profile)
    assert r.status_code == 401
    assert "not-a-valid-token" not in r.text  # the credential is never echoed


@pytest.mark.parametrize("route", [r for r in ROUTES if r[0] in DEVICE_ONLY], ids=lambda r: r[0])
def test_owner_token_is_403_on_device_only_routes(kit, awaiting_job, make_profile, route):
    r = request(kit, route, kit.owner, awaiting_job["job_id"], make_profile)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


@pytest.mark.parametrize("route", [r for r in ROUTES if r[0] in OWNER_ONLY], ids=lambda r: r[0])
def test_device_token_is_403_on_owner_only_routes(kit, awaiting_job, make_profile, route):
    r = request(kit, route, kit.dev_a, awaiting_job["job_id"], make_profile)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_owner_routes_work_for_the_owner(kit, awaiting_job, make_profile):
    expected = {"put_profile": 200, "get_profile": 200, "delete_profile": 404, "list_jobs": 200, "audit": 200}
    for route in ROUTES:
        if route[0] in expected:
            r = request(kit, route, kit.owner, awaiting_job["job_id"], make_profile)
            assert r.status_code == expected[route[0]], (route[0], r.text)


def test_both_roles_can_read_what_they_may(kit, awaiting_job, make_profile):
    for name in ("list_profiles", "get_job", "history"):
        route = next(r for r in ROUTES if r[0] == name)
        for headers in (kit.owner, kit.dev_a):
            assert request(kit, route, headers, awaiting_job["job_id"], make_profile).status_code == 200


def test_a_device_that_is_not_bound_gets_404_not_403(kit):
    r = kit.run(kit.dev_b, 1, "key-authz-b0001")
    assert r.status_code == 404 and r.json() == {"error": "not_found", "detail": "unknown id"}
    unknown = kit.run(kit.dev_b, 1, "key-authz-b0002", profile="no-such-profile")
    assert unknown.status_code == 404 and unknown.json() == r.json()  # indistinguishable
    assert kit.drain() == 0 and kit.executor.calls == []
    denied = [row[7] for row in kit.audit_rows() if row[5] == "request.denied"]
    assert denied == ["not_bound", "unknown_profile"]


def test_a_foreign_job_id_is_404_for_every_job_route(kit, make_profile):
    kit.put_profile(make_profile(device_ids=["dev-a", "dev-b"]))
    mine = kit.run(kit.dev_a, 3, "key-authz-own01").json()
    assert kit.job(kit.dev_b, mine["job_id"]).status_code == 404
    assert kit.cancel(kit.dev_b, mine["job_id"]).status_code == 404
    assert kit.confirm(kit.dev_b, mine["job_id"], mine["nonce"]).status_code == 404
    unknown = kit.job(kit.dev_b, "dj-ffffffffffffffff")
    assert unknown.status_code == 404 and unknown.json() == kit.job(kit.dev_b, mine["job_id"]).json()
    assert kit.job(kit.dev_a, mine["job_id"]).status_code == 200


def test_a_device_sees_no_foreign_profiles_or_history(kit, make_profile):
    kit.put_profile(make_profile(profile_id="other", device_ids=["dev-b"]))
    kit.run(kit.dev_b, 1, "key-authz-oth01", profile="other")
    assert [
        p["profile_id"] for p in kit.client.get("/v1/deck/profiles", headers=kit.dev_a).json()["profiles"]
    ] == ["dev"]
    assert kit.history(kit.dev_a).json()["entries"] == []
    assert kit.history(kit.dev_a, device_id="dev-b").status_code == 404


def test_a_rotated_token_stops_working(kit):
    old = kit.dev_a
    kit.enroll("dev-a")  # re-enrolling rotates the token
    assert kit.run(old, 1, "key-authz-rot01").status_code == 401
    assert (
        kit.run({"Authorization": f"Bearer {kit.tokens['dev-a']}"}, 1, "key-authz-rot02").status_code == 200
    )


def test_error_bodies_are_uniform_and_generic(kit):
    for r in (
        kit.run(kit.dev_b, 1, "key-authz-gen01"),
        kit.job(kit.dev_a, "dj-nope"),
        kit.confirm(kit.dev_a, "dj-nope", "nonce-nonce-nonce"),
    ):
        assert set(r.json()) == {"error", "detail"}
        assert kit.tokens["dev-a"] not in r.text and kit.tokens["dev-b"] not in r.text


def test_failed_authentications_are_throttled_like_the_rest_of_the_api(kit):
    for _ in range(11):
        kit.client.get("/v1/deck/profiles", headers={"Authorization": "Bearer " + "invalid"})
    blocked = kit.client.get("/v1/deck/profiles", headers=kit.dev_a)
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers


def test_device_listener_serves_device_routes_only(kit):
    app = create_app(kit.settings, store=kit.core, worker=None, audience="device", deck_runtime=kit.runtime)
    client = TestClient(app)
    ok = client.get("/v1/deck/profiles", headers=kit.dev_a)
    assert ok.status_code == 200
    run = client.post("/v1/deck/profiles/dev/slots/1/run", headers={**kit.dev_a, **KEY})
    assert run.status_code == 200
    # the owner token is refused on this listener, and owner routes do not exist on it
    assert client.get("/v1/deck/profiles", headers=kit.owner).status_code == 403
    for method, path in (
        ("PUT", "/v1/deck/profiles/dev"),
        ("GET", "/v1/deck/profiles/dev"),
        ("DELETE", "/v1/deck/profiles/dev"),
        ("GET", "/v1/deck/jobs"),
        ("GET", "/v1/deck/audit"),
    ):
        r = client.request(method, path, headers=kit.dev_a)
        assert r.status_code in (404, 405), (method, path, r.status_code)
        assert client.request(method, path, headers=kit.owner).status_code in (403, 404, 405)


def test_install_is_idempotent_and_returns_the_same_runtime(kit):
    before = len(kit.app.routes)
    assert install(kit.app) is kit.runtime
    assert install(kit.app, kit.runtime) is kit.runtime
    assert len(kit.app.routes) == before
    assert kit.run(kit.dev_a, 1, "key-authz-ins01").status_code == 200


def test_two_apps_can_share_one_runtime_and_its_lifecycle(kit):
    loopback = create_app(kit.settings, store=kit.core, worker=None, deck_runtime=kit.runtime)
    device = create_app(
        kit.settings, store=kit.core, worker=None, audience="device", deck_runtime=kit.runtime
    )
    assert loopback.state.deck is device.state.deck is kit.runtime
    with TestClient(loopback), TestClient(device):  # both lifespans start/stop the shared runner
        assert kit.runtime.runner._thread is not None and kit.runtime.runner._thread.is_alive()
    assert kit.runtime.runner._thread is None


def test_default_install_shares_one_runtime_per_data_directory(kit, tmp_path):
    import dataclasses

    from oyster.deck import mount

    settings = dataclasses.replace(kit.settings, data_dir=tmp_path / "shared")
    one = create_app(settings, store=kit.core, worker=None)
    two = create_app(settings, store=kit.core, worker=None, audience="device")
    try:
        rt = install(one)
        assert install(two) is rt and one.state.deck is two.state.deck
        assert (tmp_path / "shared" / "deck.db").exists()
        other = mount.shared_runtime(tmp_path / "elsewhere")
        assert other is not rt
        other.close()
    finally:
        rt.close()
        for key in [k for k in mount._shared if k.parent == tmp_path.resolve()]:
            mount._shared.pop(key)
