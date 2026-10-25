"""Host registry and read-only health: allowlists, visibility, adapter faults, response bounds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _companion_support import OWNER, BlockingAdapter, BrokenAdapter, Clock, Rig
from oyster.companion.adapters import OVERSHARE_EXTRA, OVERSHARE_SERVICES, MockHostAdapter

ALL_METRICS = ["cpu_percent", "load_1m", "mem_used_percent", "disk_used_percent", "uptime_s"]


# --- enrolment ------------------------------------------------------------------------------------------
def test_enrol_list_replace_delete(rig: Rig, clock: Clock):
    first = rig.host("host-1", display_name="First")
    assert first["created"] is True
    clock.advance(60)
    again = rig.host("host-1", display_name="Renamed", metrics=["cpu_percent"])
    assert again["created"] is False
    assert again["host"]["created_at"] == first["host"]["created_at"]
    assert again["host"]["updated_at"] == first["host"]["updated_at"] + 60
    listed = rig.c.get("/v1/admin/hosts", headers=OWNER).json()["hosts"]
    assert [(h["host_id"], h["display_name"], h["metrics"]) for h in listed] == [
        ("host-1", "Renamed", ["cpu_percent"])
    ]
    assert rig.c.delete("/v1/admin/hosts/host-1", headers=OWNER).json() == {"deleted": "host-1"}
    assert rig.c.delete("/v1/admin/hosts/host-1", headers=OWNER).status_code == 404
    assert rig.c.get("/v1/admin/hosts", headers=OWNER).json() == {"hosts": []}


def test_host_cap_blocks_new_hosts_but_not_replacements(rig: Rig):
    for i in range(16):
        rig.host(f"h{i}")
    r = rig.c.post(
        "/v1/admin/hosts",
        headers=OWNER,
        json={"host_id": "h16", "display_name": "x", "metrics": ["cpu_percent"], "device_ids": []},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    assert rig.host("h3", display_name="still fine")["created"] is False


@pytest.mark.parametrize(
    "patch",
    [
        {"command": "id"},
        {"cmd": ["ls"]},
        {"shell": "sh -c true"},
        {"extra": 1},
        {"metrics": []},
        {"metrics": ["cpu_percent", "cpu_percent"]},
        {"metrics": ["cpu_percent", "swap_percent"]},
        {"metrics": ["services"], "services": []},  # services allowlisted but no names enrolled
        {"metrics": ["cpu_percent"], "services": ["nginx"]},  # names enrolled but not allowlisted
        {"metrics": ["services"], "services": ["nginx", "nginx"]},
        {"metrics": ["services"], "services": ["nginx; id"]},
        {"metrics": ["services"], "services": ["a b"]},
        {"metrics": ["services"], "services": ["$(id)"]},
        {"metrics": ["services"], "services": [f"s{i}" for i in range(17)]},
        {"host_id": "has space"},
        {"host_id": "../etc"},
        {"host_id": "x" * 41},
        {"display_name": ""},
        {"display_name": "x" * 65},
        {"display_name": "line\nbreak"},
        {"adapter": "ssh"},
        {"device_ids": ["bad id"]},
        {"device_ids": ["a", "a"]},
        {"device_ids": [f"d{i}" for i in range(17)]},
    ],
)
def test_enrol_rejects_bad_requests(rig: Rig, patch):
    body = {"host_id": "host-1", "display_name": "Host", "metrics": ["cpu_percent"], "device_ids": []}
    body.update(patch)
    r = rig.c.post("/v1/admin/hosts", headers=OWNER, json=body)
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "validation_failed"
    assert rig.c.get("/v1/admin/hosts", headers=OWNER).json() == {"hosts": []}


def test_enrol_rejects_non_objects_and_oversized_bodies(rig: Rig):
    assert rig.c.post("/v1/admin/hosts", headers=OWNER, json=["x"]).status_code == 422
    assert rig.c.post("/v1/admin/hosts", headers=OWNER, content=b"{").status_code == 422
    big = {"host_id": "h", "display_name": "x" * 20_000, "metrics": ["cpu_percent"]}
    assert rig.c.post("/v1/admin/hosts", headers=OWNER, json=big).status_code == 413


# --- visibility -----------------------------------------------------------------------------------------
def test_a_device_sees_only_hosts_shared_with_it(rig: Rig):
    mine, other = rig.device("dev-1"), rig.device("dev-2")
    rig.host("shared", device_ids=["dev-1", "dev-2"])
    rig.host("mine", device_ids=["dev-1"])
    rig.host("nobody", device_ids=[])  # deny by default: owner only
    got = rig.c.get("/v1/hosts", headers=mine).json()["hosts"]
    assert sorted(h["host_id"] for h in got) == ["mine", "shared"]
    assert [h["host_id"] for h in rig.c.get("/v1/hosts", headers=other).json()["hosts"]] == ["shared"]
    assert set(got[0]) == {"host_id", "display_name"}  # no allowlist or service names in the list


def test_unknown_and_foreign_ids_are_indistinguishable_404s(rig: Rig):
    other = rig.device("dev-2")
    rig.host("mine", device_ids=["dev-1"])
    foreign = rig.c.get("/v1/hosts/mine/health", headers=other)
    unknown = rig.c.get("/v1/hosts/nothing/health", headers=other)
    malformed = rig.c.get("/v1/hosts/not.an.id/health", headers=other)
    assert foreign.status_code == unknown.status_code == malformed.status_code == 404
    assert foreign.json() == unknown.json() == malformed.json()
    assert foreign.json()["error"] == "not_found"


def test_deleting_a_host_closes_its_health_endpoint(rig: Rig):
    dev = rig.device("dev-1")
    rig.host("host-1")
    assert rig.c.get("/v1/hosts/host-1/health", headers=dev).status_code == 200
    rig.c.delete("/v1/admin/hosts/host-1", headers=OWNER)
    assert rig.c.get("/v1/hosts/host-1/health", headers=dev).status_code == 404


def test_device_cannot_modify_hosts(rig: Rig):
    dev = rig.device("dev-1")
    rig.host("host-1")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r = rig.c.request(method, "/v1/hosts/host-1/health", headers=dev, json={})
        assert r.status_code == 405, method
    for method in ("POST", "PUT", "DELETE"):
        assert rig.c.request(method, "/v1/hosts", headers=dev, json={}).status_code == 405


# --- allowlist ------------------------------------------------------------------------------------------
def test_health_returns_exactly_the_allowlisted_metrics(rig: Rig):
    dev = rig.device("dev-1")
    rig.host("host-1", metrics=["cpu_percent", "uptime_s"])
    r = rig.c.get("/v1/hosts/host-1/health", headers=dev)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "ok" and body["host_id"] == "host-1" and body["missing"] == []
    assert body["metrics"] == {"cpu_percent": 12.5, "uptime_s": 86400}
    assert "services" not in body
    assert body["collected_at"] == 1_800_000_000


def test_metrics_outside_the_allowlist_are_never_requested_or_returned(rig: Rig):
    dev = rig.device("dev-1")
    rig.adapters["host-1"] = MockHostAdapter(overshare=True)
    rig.host("host-1", metrics=["cpu_percent"])
    r = rig.c.get("/v1/hosts/host-1/health", headers=dev)
    assert r.status_code == 200
    assert r.json()["metrics"] == {"cpu_percent": 12.5}
    text = r.text
    unlisted = ["load_1m", "disk_used_percent", "uptime_s", "mock-internal-name"]
    for leaked in [*OVERSHARE_EXTRA, *OVERSHARE_SERVICES, *unlisted]:
        assert leaked not in text
    assert rig.adapters["host-1"].requested == [frozenset({"cpu_percent"})]


def test_only_enrolled_services_are_returned(rig: Rig):
    dev = rig.device("dev-1")
    rig.adapters["host-1"] = MockHostAdapter(overshare=True)
    rig.host("host-1", metrics=["services"], services=["nginx", "backup", "ghost"])
    body = rig.c.get("/v1/hosts/host-1/health", headers=dev).json()
    assert body["services"] == [
        {"name": "nginx", "state": "active"},
        {"name": "backup", "state": "inactive"},
        {"name": "ghost", "state": "unknown"},  # enrolled but not reported by the adapter
    ]
    assert body["metrics"] == {}
    assert "sshd" not in json.dumps(body) and "vpn-secret-service" not in json.dumps(body)


def test_invalid_service_state_from_the_adapter_becomes_unknown(rig: Rig):
    dev = rig.device("dev-1")
    rig.adapters["host-1"] = MockHostAdapter(services={"nginx": "exploded; id"})
    rig.host("host-1", metrics=["services"], services=["nginx"])
    body = rig.c.get("/v1/hosts/host-1/health", headers=dev).json()
    assert body["services"] == [{"name": "nginx", "state": "unknown"}]


def test_out_of_range_or_mistyped_values_are_dropped_not_forwarded(rig: Rig):
    dev = rig.device("dev-1")
    rig.adapters["host-1"] = MockHostAdapter(
        fixture={
            "cpu_percent": 250,  # above range
            "load_1m": float("nan"),
            "mem_used_percent": "41",  # a string is not a number
            "disk_used_percent": True,  # nor is a bool
            "uptime_s": 86_400.5,  # not a whole number of seconds
        }
    )
    rig.host("host-1", metrics=["cpu_percent", "uptime_s"])
    r = rig.c.get("/v1/hosts/host-1/health", headers=dev)
    assert r.status_code == 502 and r.json()["state"] == "unreachable"  # nothing usable at all
    rig.adapters["host-1"] = MockHostAdapter(fixture={"cpu_percent": 250, "uptime_s": 7})
    body = rig.c.get("/v1/hosts/host-1/health", headers=dev).json()
    assert body["state"] == "partial" and body["missing"] == ["cpu_percent"]
    assert body["metrics"] == {"uptime_s": 7}


# --- adapter faults -------------------------------------------------------------------------------------
@pytest.mark.parametrize("fault", ["unreachable", "timeout"])
def test_adapter_faults_map_to_typed_502(rig: Rig, fault):
    dev = rig.device("dev-1")
    rig.adapters["host-1"] = MockHostAdapter(fault=fault)
    rig.host("host-1")
    r = rig.c.get("/v1/hosts/host-1/health", headers=dev)
    assert r.status_code == 502
    body = r.json()
    assert body["error"] == "host_unavailable"
    assert body["state"] == fault
    assert body["host_id"] == "host-1"
    assert set(body) == {"error", "detail", "state", "host_id"}
    assert "Traceback" not in r.text and "mock" not in r.text
    assert len(r.content) < 4096


def test_partial_data_is_a_200_with_a_missing_list(rig: Rig):
    dev = rig.device("dev-1")
    dropped = frozenset({"disk_used_percent", "services"})
    rig.adapters["host-1"] = MockHostAdapter(fault="partial", drop=dropped)
    rig.host("host-1", metrics=[*ALL_METRICS, "services"], services=["nginx"])
    r = rig.c.get("/v1/hosts/host-1/health", headers=dev)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "partial"
    assert body["missing"] == ["disk_used_percent", "services"]
    assert "disk_used_percent" not in body["metrics"] and "services" not in body
    assert body["metrics"]["cpu_percent"] == 12.5


def test_an_unexpected_adapter_error_is_a_502_and_never_echoed(rig: Rig):
    dev = rig.device("dev-1")
    rig.adapters["host-1"] = BrokenAdapter()
    rig.host("host-1")
    r = rig.c.get("/v1/hosts/host-1/health", headers=dev)
    assert r.status_code == 502
    assert r.json()["state"] == "unreachable"
    assert BrokenAdapter.SECRET not in r.text
    assert "RuntimeError" not in r.text and "Traceback" not in r.text
    assert BrokenAdapter.SECRET not in json.dumps(rig.audit())


def test_a_hanging_adapter_hits_the_deadline(tmp_path: Path, clock: Clock):
    rig = Rig(tmp_path, clock, deadline_s=0.05)
    blocking = BlockingAdapter()
    try:
        dev = rig.device("dev-1")
        rig.adapters["host-1"] = blocking
        rig.host("host-1")
        r = rig.c.get("/v1/hosts/host-1/health", headers=dev)
        assert r.status_code == 502
        assert r.json()["state"] == "timeout"
    finally:
        blocking.release.set()
        rig.close()


# --- bounds ---------------------------------------------------------------------------------------------
def test_health_response_stays_under_4_kib_at_maximum_size(rig: Rig):
    dev = rig.device("dev-1")
    names = [("s" * 63) + chr(ord("a") + i) for i in range(16)]
    rig.adapters["host-1"] = MockHostAdapter(services={n: "active" for n in names})
    rig.host("host-1", metrics=[*ALL_METRICS, "services"], services=names, display_name="x" * 64)
    r = rig.c.get("/v1/hosts/host-1/health", headers=dev)
    assert r.status_code == 200
    assert len(r.json()["services"]) == 16
    assert len(r.content) < 4096


def test_host_list_stays_under_4_kib_at_maximum_size(rig: Rig):
    dev = rig.device("dev-1")
    for i in range(16):
        rig.host(("h" * 38) + f"{i:02d}", display_name="n" * 64)
    r = rig.c.get("/v1/hosts", headers=dev)
    assert r.status_code == 200
    assert len(r.json()["hosts"]) == 16
    assert len(r.content) < 4096
