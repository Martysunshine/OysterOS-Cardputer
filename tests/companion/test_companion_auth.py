"""Authentication on every companion route: 401 (missing/invalid), 403 (wrong role), 404 on the device app."""

from __future__ import annotations

import pytest
from _companion_support import BAD, OWNER

HOST_BODY = {
    "host_id": "host-1",
    "display_name": "Host",
    "adapter": "mock",
    "metrics": ["cpu_percent"],
    "services": [],
    "device_ids": ["dev-1"],
}

OWNER_ROUTES = [
    ("POST", "/v1/admin/hosts", HOST_BODY),
    ("GET", "/v1/admin/hosts", None),
    ("DELETE", "/v1/admin/hosts/host-1", None),
    ("POST", "/v1/admin/pairing", {}),
    ("GET", "/v1/admin/audit", None),
]
DEVICE_ROUTES = [
    ("GET", "/v1/hosts", None),
    ("GET", "/v1/hosts/host-1/health", None),
]


def call(client, method, path, headers=None, body=None):
    kw = {"headers": headers or {}}
    if body is not None:
        kw["json"] = body
    return client.request(method, path, **kw)


@pytest.mark.parametrize(("method", "path", "body"), OWNER_ROUTES + DEVICE_ROUTES)
def test_missing_or_invalid_token_is_401(rig, method, path, body):
    for headers in (None, BAD, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        r = call(rig.c, method, path, headers, body)
        assert r.status_code == 401, (headers, r.text)
        assert r.json()["error"] == "unauthorized"
        assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path", "body"), OWNER_ROUTES)
def test_owner_routes_refuse_a_device_token(rig, method, path, body):
    dev = rig.device("dev-1")
    r = call(rig.c, method, path, dev, body)
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"


@pytest.mark.parametrize(("method", "path", "body"), DEVICE_ROUTES)
def test_device_routes_refuse_the_owner_token(rig, method, path, body):
    r = call(rig.c, method, path, OWNER, body)
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"


def test_roles_that_are_allowed_get_through(rig):
    dev = rig.device("dev-1")
    assert call(rig.c, "POST", "/v1/admin/hosts", OWNER, HOST_BODY).status_code == 200
    assert call(rig.c, "GET", "/v1/admin/hosts", OWNER).status_code == 200
    assert call(rig.c, "GET", "/v1/admin/audit", OWNER).status_code == 200
    assert call(rig.c, "POST", "/v1/admin/pairing", OWNER, {}).status_code == 200
    assert call(rig.c, "GET", "/v1/hosts", dev).status_code == 200
    assert call(rig.c, "GET", "/v1/hosts/host-1/health", dev).status_code == 200
    assert call(rig.c, "DELETE", "/v1/admin/hosts/host-1", OWNER).status_code == 200


def test_authentication_happens_before_the_body_is_read(rig):
    json_type = {"Content-Type": "application/json"}
    r = rig.c.post("/v1/admin/hosts", content=b"not json at all", headers=json_type)
    assert r.status_code == 401
    r = rig.c.post("/v1/admin/pairing", content=b"x" * 100_000, headers=BAD)
    assert r.status_code == 401


def test_a_revoked_device_token_stops_working(rig):
    old = rig.device("dev-1")
    rig.host()
    assert rig.c.get("/v1/hosts", headers=old).status_code == 200
    rig.device("dev-1")  # re-enrolling rotates the token
    assert rig.c.get("/v1/hosts", headers=old).status_code == 401


def test_pairing_redemption_is_the_only_unauthenticated_route(rig):
    r = rig.c.post("/v1/pair", json={"code": "ABCDEFGH", "device_id": "dev-9"})
    assert r.status_code == 401  # a wrong code, not a missing token
    assert "www-authenticate" not in r.headers
    assert r.json()["error"] == "unauthorized"


def test_device_app_serves_no_owner_routes(device_rig):
    dev = device_rig.device("dev-1")
    for method, path, body in OWNER_ROUTES:
        for headers in (None, OWNER, dev):
            r = call(device_rig.c, method, path, headers, body)
            assert r.status_code == 404, (method, path, headers)


def test_device_app_serves_device_routes_and_refuses_the_owner_token(device_rig):
    dev = device_rig.device("dev-1")
    assert device_rig.c.get("/v1/hosts", headers=dev).status_code == 200
    assert device_rig.c.get("/v1/hosts", headers=OWNER).status_code == 403
    assert device_rig.c.post("/v1/pair", json={"code": "ABCDEFGH", "device_id": "dev-9"}).status_code == 401


def test_existing_health_route_is_untouched(rig):
    r = rig.c.get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "api": "v1"}
