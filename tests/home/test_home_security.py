"""Smart Home security: who may call what, the audit trail, and the rule that the Home Assistant URL and token
never leave the backend (responses, headers, logs, files, owner views)."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing

import pytest
from _home_support import FAKE_TOKEN, HA_URL, HomeKit

DEVICE_ROUTES = [
    ("GET", "/v1/home/rooms", None),
    ("GET", "/v1/home/rooms/living/devices", None),
    ("POST", "/v1/home/actions", {"entity_id": "light.living_ceiling", "action": "toggle"}),
]
OWNER_ROUTES = [("GET", "/v1/home/status"), ("GET", "/v1/home/audit")]
KEY = {"Idempotency-Key": "authz-key-0001"}


def call(kit: HomeKit, method: str, path: str, headers: dict | None, body=None):
    h = {**(headers or {}), **(KEY if method == "POST" else {})}
    return kit.client.request(method, path, headers=h, json=body)


# ---- who may call what ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), DEVICE_ROUTES)
def test_device_routes_require_a_token(kit: HomeKit, method, path, body):
    assert call(kit, method, path, None, body).status_code == 401
    assert call(kit, method, path, {"Authorization": "Bearer nonsense"}, body).status_code == 401
    assert call(kit, method, path, {"Authorization": "Basic abc"}, body).status_code == 401
    assert kit.ha.requests == []


@pytest.mark.parametrize(("method", "path"), OWNER_ROUTES)
def test_owner_routes_require_the_owner_token(kit: HomeKit, method, path):
    assert call(kit, method, path, None).status_code == 401
    assert call(kit, method, path, kit.dev_a).status_code == 403
    assert call(kit, method, path, kit.owner).status_code == 200


def test_the_owner_token_can_read_but_not_switch(kit: HomeKit):
    assert kit.rooms(headers=kit.owner).status_code == 200
    assert kit.devices("living", headers=kit.owner).status_code == 200
    r = kit.act("light.living_ceiling", "toggle", headers=kit.owner)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert kit.ha.service_calls == []  # the confirmation is a key press on a device: the owner cannot give it


def test_the_lan_facing_app_serves_device_routes_only(kit_factory):
    kit = kit_factory(audience="device")
    assert kit.rooms().status_code == 200
    assert kit.devices("living").status_code == 200
    assert kit.act("light.living_ceiling", "toggle").status_code == 200
    for method, path in OWNER_ROUTES:
        assert call(kit, method, path, kit.owner).status_code in (403, 404)
        assert call(kit, method, path, kit.dev_a).status_code == 404  # the route does not exist there
    # the owner token is refused on every route of the device listener
    for method, path, body in DEVICE_ROUTES:
        assert call(kit, method, path, kit.owner, body).status_code == 403


def test_repeated_bad_tokens_get_the_existing_throttle(kit: HomeKit):
    statuses = {
        call(kit, "GET", "/v1/home/rooms", {"Authorization": "Bearer bad"}).status_code for _ in range(14)
    }
    assert statuses == {401, 429}


# ---- audit ----------------------------------------------------------------------------------------------


def test_every_action_leaves_a_row_without_secrets(kit: HomeKit):
    kit.act("light.living_ceiling", "toggle", key="audit-key-0001")
    kit.act("lock.front_door", "turn_off", key="audit-key-0002")  # refused: no confirmation
    kit.act("lock.front_door", "turn_off", confirmed=True, key="audit-key-0003")
    kit.act("light.nowhere", "toggle", key="audit-key-0004")
    rows = kit.audit_rows()
    assert rows == [
        ("action", "pending", None, "dev-a", "living", "light.living_ceiling", "toggle", 0),
        ("action", "ok", None, "dev-a", "living", "light.living_ceiling", "toggle", 0),
        ("denied", "denied", "confirmation_required", "dev-a", "hall", "lock.front_door", "turn_off", None),
        ("action", "pending", None, "dev-a", "hall", "lock.front_door", "turn_off", 1),
        ("action", "ok", None, "dev-a", "hall", "lock.front_door", "turn_off", 1),
        ("denied", "denied", "not_found", "dev-a", None, "light.nowhere", "toggle", None),
    ]
    with closing(sqlite3.connect(kit.tmp_path / "home.db")) as conn:
        dump = "\n".join(str(r) for r in conn.execute("SELECT * FROM audit"))
        dump += "\n".join(str(r) for r in conn.execute("SELECT * FROM replays"))
    for secret in (FAKE_TOKEN, HA_URL, "192.168.1.50", "audit-key-0001", "audit-key-0003"):
        assert secret not in dump  # the key is stored as a hash only


def test_the_audit_table_refuses_changes(kit: HomeKit):
    kit.act("light.living_ceiling", "toggle")
    with closing(sqlite3.connect(kit.tmp_path / "home.db")) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE audit SET outcome='ok2'")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM audit")


def test_owner_reads_the_audit_newest_first_in_pages(kit: HomeKit):
    for _ in range(3):
        kit.act("light.living_ceiling", "toggle")
    r = kit.client.get("/v1/home/audit", headers=kit.owner, params={"limit": 4})
    body = r.json()
    assert r.status_code == 200 and len(body["entries"]) == 4
    seqs = [e["seq"] for e in body["entries"]]
    assert seqs == sorted(seqs, reverse=True) and body["next_before"] == seqs[-1]
    e = body["entries"][0]
    assert (
        e["actor"] == "device:dev-a"
        and e["entity_id"] == "light.living_ceiling"
        and "idempotency_key_hash" in e
    )
    rest = kit.client.get("/v1/home/audit", headers=kit.owner, params={"before": body["next_before"]}).json()
    assert [x["seq"] for x in rest["entries"]] == [seqs[-1] - 1, seqs[-1] - 2]
    assert rest["next_before"] is None
    only_b = kit.client.get("/v1/home/audit", headers=kit.owner, params={"device_id": "dev-b"}).json()
    assert only_b["entries"] == []
    assert kit.client.get("/v1/home/audit", headers=kit.owner, params={"limit": 51}).status_code == 422


def test_no_file_is_created_until_something_is_audited(kit: HomeKit):
    kit.rooms()
    kit.devices("living")
    assert not (kit.tmp_path / "home.db").exists()


# ---- secrets --------------------------------------------------------------------------------------------


def test_the_url_and_token_never_appear_anywhere(kit: HomeKit, caplog):
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []

    def keep(r):
        seen.append(r.text + str(dict(r.headers)))

    for mode in (None, "down", "timeout", "tls", "401", "400", "500", "redirect", "huge", "garbage"):
        kit.ha.fail = mode
        kit.runtime.service._probed_until = 0.0
        keep(kit.rooms())
        keep(kit.devices("living"))
        keep(kit.act("light.living_ceiling", "toggle"))
        keep(kit.act("lock.front_door", "turn_off"))
        keep(kit.act("light.nowhere", "toggle"))
        keep(kit.client.get("/v1/home/status", headers=kit.owner))
        keep(kit.client.get("/v1/home/audit", headers=kit.owner))
    kit.ha.fail = None
    keep(kit.rooms())
    blob = "\n".join(seen)
    for secret in (FAKE_TOKEN, HA_URL, "192.168.1.50", "203.0.113.9"):
        assert secret not in blob
        assert secret not in caplog.text
    assert secret not in repr(kit.runtime.config) and FAKE_TOKEN not in repr(
        kit.runtime.service.__dict__.get("_cfg")
    )


def test_the_owner_status_names_what_is_set_but_not_the_values(kit: HomeKit):
    s = kit.client.get("/v1/home/status", headers=kit.owner).json()
    assert s == {
        "configured": True,
        "reason": None,
        "url_set": True,
        "token_set": True,
        "tls": False,
        "ca_file_set": False,
        "allowlist_file": "home_allowlist.json",
        "rooms": 3,
        "entities": 7,
        "impactful": 3,
        "problems": [],
    }


def test_the_owner_status_explains_a_bad_allowlist(kit_factory):
    kit = kit_factory(allowlist={"rooms": [{"id": "A B", "name": "x", "entities": []}]})
    s = kit.client.get("/v1/home/status", headers=kit.owner).json()
    assert s["configured"] is False and s["reason"] == "bad_allowlist" and s["problems"]
    kit2 = kit_factory(url="", token="")
    s2 = kit2.client.get("/v1/home/status", headers=kit2.owner).json()
    assert (s2["reason"], s2["url_set"], s2["token_set"]) == ("no_url", False, False)


def test_http_without_tls_is_flagged_for_the_owner(kit_factory, caplog):
    caplog.set_level(logging.WARNING)
    kit = kit_factory()
    assert kit.client.get("/v1/home/status", headers=kit.owner).json()["tls"] is False
    assert "plain http" in caplog.text
    assert "192.168.1.50" not in caplog.text


def test_install_is_idempotent_and_reads_the_environment(tmp_path, monkeypatch):
    from _home_support import make_settings
    from oyster.db.sqlite_store import SqliteStore
    from oyster.home import mount
    from oyster.main import create_app

    monkeypatch.setattr(mount, "_shared", {})
    monkeypatch.setenv("HOME_ASSISTANT_URL", "https://ha.example.test:8123/")
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("OYSTER_HOME_ALLOWLIST", "lists/home.json")
    core = SqliteStore(":memory:")
    app = create_app(make_settings(tmp_path), store=core, worker=None)
    rt = app.state.home
    assert rt.config.url == "https://ha.example.test:8123" and rt.config.tls
    assert rt.config.allowlist_path == tmp_path / "lists" / "home.json"
    assert mount.install(app) is rt  # a second call changes nothing
    app2 = create_app(make_settings(tmp_path), store=core, worker=None, audience="device")
    assert app2.state.home is rt  # both listeners of one process share the runtime
    core.close()
    rt.close()


def test_existing_routes_are_not_shadowed(kit: HomeKit):
    assert kit.client.get("/v1/health").status_code == 200
    assert kit.client.get("/v1/deck/profiles", headers=kit.dev_a).status_code == 200
