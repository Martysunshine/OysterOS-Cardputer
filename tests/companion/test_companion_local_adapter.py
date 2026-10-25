"""The read-only `local` adapter (this PC through psutil) and the seed helper that enrols it.

Real psutil is used in exactly one test, which only asserts types and ranges; every other test installs a
fake so no value depends on the machine running the tests.
"""

from __future__ import annotations

import inspect
import os
import time
import types
from pathlib import Path

import oyster.companion.adapters as adapters
import pytest
from _companion_support import OWNER, Clock, Rig
from oyster.companion.adapters import HostAdapter, LocalHostAdapter, build_adapter
from oyster.companion.models import METRIC_NAMES, SERVICE_STATES, HostRecord
from oyster.companion.seed import LOCAL_HOST_METRICS, ensure_local_host
from oyster.companion.store import CompanionStore
from oyster.store import Conflict

ALL = frozenset(METRIC_NAMES)
LOCAL_METRICS = ["cpu_percent", "mem_used_percent", "disk_used_percent", "uptime_s"]


class FakePsutil:
    """Just enough of psutil for the adapter. `services` maps name -> status text or an exception to raise;
    None leaves `win_service_get` out entirely (a non-Windows psutil). `fail` names reads that raise."""

    def __init__(self, *, services=None, fail=(), cpu=12.5, mem=41.0, disk=63.2, boot=None):
        self.calls: list[tuple] = []
        self._fail, self._cpu, self._mem, self._disk, self._boot = set(fail), cpu, mem, disk, boot
        self._services = services
        if services is not None:
            self.win_service_get = self._win_service_get

    def _maybe_fail(self, what: str) -> None:
        if what in self._fail:
            raise OSError(f"{what} failed with C:/secret/path")

    def cpu_percent(self, interval=None):
        self.calls.append(("cpu", interval))
        self._maybe_fail("cpu")
        return self._cpu

    def virtual_memory(self):
        self.calls.append(("mem",))
        self._maybe_fail("mem")
        return types.SimpleNamespace(percent=self._mem)

    def disk_usage(self, path):
        self.calls.append(("disk", path))
        self._maybe_fail("disk")
        return types.SimpleNamespace(percent=self._disk)

    def boot_time(self):
        self.calls.append(("boot",))
        self._maybe_fail("boot")
        return time.time() - 86_400 if self._boot is None else self._boot

    def _win_service_get(self, name):
        self.calls.append(("service", name))
        result = self._services[name]  # KeyError for a name the fake does not know
        if isinstance(result, Exception):
            raise result
        return types.SimpleNamespace(status=lambda: result)

    def called(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def fake(monkeypatch) -> FakePsutil:
    f = FakePsutil(services={"Spooler": "running", "W32Time": "stopped"})
    monkeypatch.setattr(adapters, "psutil", f)
    return f


@pytest.fixture
def lrig(tmp_path: Path, clock: Clock):
    """An app whose adapters are built by the real factory (so `local` hosts get a LocalHostAdapter)."""
    r = Rig(tmp_path, clock, adapter_factory=build_adapter)
    yield r
    r.close()


def local_host(rig: Rig, host_id: str = "this-pc", **over) -> dict:
    body = {
        "host_id": host_id,
        "display_name": "This PC",
        "adapter": "local",
        "metrics": LOCAL_METRICS,
        "services": [],
        "device_ids": ["dev-1"],
    }
    body.update(over)
    r = rig.c.post("/v1/admin/hosts", headers=OWNER, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def adapter(**kw) -> LocalHostAdapter:
    kw.setdefault("cpu_sample_s", 0.0)
    return LocalHostAdapter(**kw)


# --- the adapter itself ------------------------------------------------------------------------------------
def test_real_psutil_gives_typed_values_in_range():
    reading = LocalHostAdapter(["no-such-service-0123"], cpu_sample_s=0.05).health(ALL)
    for name in ("cpu_percent", "mem_used_percent", "disk_used_percent"):
        assert isinstance(reading.metrics[name], float)
        assert 0.0 <= reading.metrics[name] <= 100.0
    assert isinstance(reading.metrics["uptime_s"], int) and reading.metrics["uptime_s"] > 0
    assert ("load_1m" in reading.metrics) == hasattr(os, "getloadavg")
    assert set(reading.metrics) <= set(METRIC_NAMES)
    assert set(reading.services) == {"no-such-service-0123"}
    assert set(reading.services.values()) <= set(SERVICE_STATES)


def test_values_come_from_the_expected_reads(monkeypatch):
    monkeypatch.setattr(
        adapters, "psutil", FakePsutil(boot=1_000.0, services={"Spooler": "running", "W32Time": "stopped"})
    )
    reading = adapter(clock=lambda: 1_600.0, services=["Spooler", "W32Time"]).health(
        frozenset({"cpu_percent", "mem_used_percent", "disk_used_percent", "uptime_s", "services"})
    )
    assert reading.metrics == {
        "cpu_percent": 12.5,
        "mem_used_percent": 41.0,
        "disk_used_percent": 63.2,
        "uptime_s": 600,
    }
    assert reading.services == {"Spooler": "active", "W32Time": "inactive"}


def test_only_the_requested_metrics_are_read(fake: FakePsutil):
    reading = adapter(services=["Spooler"]).health(frozenset({"mem_used_percent"}))
    assert reading.metrics == {"mem_used_percent": 41.0}
    assert reading.services == {}
    assert [c[0] for c in fake.calls] == ["mem"]


def test_unknown_metric_names_are_ignored(fake: FakePsutil):
    reading = adapter().health(frozenset({"mem_used_percent", "swap_percent", "hostname", "; id"}))
    assert reading.metrics == {"mem_used_percent": 41.0}
    assert [c[0] for c in fake.calls] == ["mem"]


def test_cpu_uses_a_short_blocking_sample_by_default(monkeypatch):
    f = FakePsutil()
    monkeypatch.setattr(adapters, "psutil", f)
    LocalHostAdapter().health(frozenset({"cpu_percent"}))
    adapter(cpu_sample_s=0.0).health(frozenset({"cpu_percent"}))
    assert f.called("cpu") == [("cpu", 0.2), ("cpu", None)]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("running", "active"),
        ("stopped", "inactive"),
        ("paused", "inactive"),
        ("start_pending", "unknown"),
        ("stop_pending", "unknown"),
        ("something-new", "unknown"),
    ],
)
def test_windows_service_states_map_to_the_four_api_states(monkeypatch, status, expected):
    monkeypatch.setattr(adapters, "psutil", FakePsutil(services={"Spooler": status}))
    assert adapter(services=["Spooler"]).health(frozenset({"services"})).services == {"Spooler": expected}


def test_a_missing_or_unreadable_service_is_unknown_and_never_raises(monkeypatch):
    f = FakePsutil(services={"Spooler": "running", "Locked": PermissionError("denied")})
    monkeypatch.setattr(adapters, "psutil", f)
    reading = adapter(services=["Spooler", "Locked", "Gone"]).health(frozenset({"services"}))
    assert reading.services == {"Spooler": "active", "Locked": "unknown", "Gone": "unknown"}


def test_without_a_service_api_every_state_is_unknown(monkeypatch):
    monkeypatch.setattr(adapters, "psutil", FakePsutil(services=None))  # no win_service_get
    reading = adapter(services=["sshd", "cron"]).health(frozenset({"services"}))
    assert reading.services == {"sshd": "unknown", "cron": "unknown"}


def test_only_well_formed_enrolled_service_names_reach_the_operating_system(fake: FakePsutil):
    names = ["Spooler", "a b", "x; id", "$(whoami)", "W32Time", "", "../x", "n" * 65]
    reading = adapter(services=names).health(frozenset({"services"}))
    assert [c[1] for c in fake.called("service")] == ["Spooler", "W32Time"]
    assert set(reading.services) == {"Spooler", "W32Time"}


def test_a_failing_read_drops_only_that_metric_and_never_leaks_its_error(monkeypatch, caplog):
    monkeypatch.setattr(adapters, "psutil", FakePsutil(fail={"mem"}))
    with caplog.at_level("DEBUG", logger="oyster.companion"):
        reading = adapter().health(frozenset({"cpu_percent", "mem_used_percent", "uptime_s"}))
    assert set(reading.metrics) == {"cpu_percent", "uptime_s"}
    assert "secret" not in caplog.text and "OSError" in caplog.text


def test_load_is_missing_where_the_os_has_no_load_average(fake, monkeypatch):
    monkeypatch.delattr(os, "getloadavg", raising=False)
    assert "load_1m" not in adapter().health(frozenset({"load_1m", "cpu_percent"})).metrics


def test_load_is_a_real_one_minute_average_where_available(fake, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (1.5, 1.0, 0.5), raising=False)
    assert adapter().health(frozenset({"load_1m"})).metrics == {"load_1m": 1.5}


def test_the_system_drive_is_validated_before_it_is_used():
    pick = LocalHostAdapter._system_disk
    assert pick("win32", {"SystemDrive": "D:"}) == "D:\\"
    assert pick("win32", {}) == "C:\\"
    for bad in ("C:\\evil", "", "CD:", "C", "1:", "C:\n"):
        assert pick("win32", {"SystemDrive": bad}) == "C:\\"
    assert pick("linux", {"SystemDrive": "D:"}) == "/"


def test_disk_usage_is_read_from_the_system_drive(fake: FakePsutil):
    local = adapter()
    local.health(frozenset({"disk_used_percent"}))
    assert fake.called("disk") == [("disk", local._disk_path)]


def test_the_adapter_is_read_only_and_has_no_command_surface():
    local = LocalHostAdapter()
    info = local.describe()
    assert (info.kind, info.read_only) == ("local", True)
    assert info.supported_metrics == frozenset(METRIC_NAMES)
    assert isinstance(local, HostAdapter)
    callables = {n for n in dir(LocalHostAdapter) if not n.startswith("_") and callable(getattr(local, n))}
    assert callables == {"describe", "health"}
    assert list(inspect.signature(LocalHostAdapter.health).parameters) == ["self", "metrics"]


def test_the_factory_builds_local_adapters_with_the_enrolled_services():
    rec = HostRecord(
        host_id="this-pc",
        display_name="This PC",
        adapter="local",
        metrics=["services"],
        services=["Spooler"],
        device_ids=[],
        created_at=0.0,
        updated_at=0.0,
    )
    built = build_adapter(rec)
    assert isinstance(built, LocalHostAdapter)
    assert built._services == ("Spooler",)


# --- through the API ----------------------------------------------------------------------------------------
def test_a_local_host_can_be_enrolled_and_read_by_its_device(lrig: Rig, fake: FakePsutil, clock: Clock):
    fake._boot = time.time() - 183_600  # 2 d 3 h before now
    dev = lrig.device("dev-1")
    enrolled = local_host(lrig)
    assert enrolled["created"] is True and enrolled["host"]["adapter"] == "local"
    assert lrig.c.get("/v1/hosts", headers=dev).json() == {
        "hosts": [{"host_id": "this-pc", "display_name": "This PC"}]
    }
    r = lrig.c.get("/v1/hosts/this-pc/health", headers=dev)
    assert r.status_code == 200, r.text
    body = r.json()
    assert 183_600 <= body["metrics"].pop("uptime_s") <= 183_700
    assert body == {
        "host_id": "this-pc",
        "state": "ok",
        "collected_at": int(clock()),
        "metrics": {"cpu_percent": 12.5, "mem_used_percent": 41.0, "disk_used_percent": 63.2},
        "missing": [],
    }


def test_services_are_reported_for_the_enrolled_names_only(lrig: Rig, fake: FakePsutil):
    dev = lrig.device("dev-1")
    local_host(lrig, metrics=["services"], services=["Spooler", "W32Time"])
    body = lrig.c.get("/v1/hosts/this-pc/health", headers=dev).json()
    assert body["services"] == [
        {"name": "Spooler", "state": "active"},
        {"name": "W32Time", "state": "inactive"},
    ]
    assert [c[1] for c in fake.called("service")] == ["Spooler", "W32Time"]
    assert fake.called("cpu") == [] and fake.called("mem") == []  # not allowlisted, not even read


def test_load_1m_on_a_platform_without_it_is_partial_not_faked(lrig: Rig, fake, monkeypatch):
    monkeypatch.delattr(os, "getloadavg", raising=False)
    dev = lrig.device("dev-1")
    local_host(lrig, metrics=["cpu_percent", "load_1m"])
    body = lrig.c.get("/v1/hosts/this-pc/health", headers=dev).json()
    assert body["state"] == "partial" and body["missing"] == ["load_1m"]
    assert body["metrics"] == {"cpu_percent": 12.5}


def test_a_metric_that_cannot_be_read_makes_the_report_partial(lrig: Rig, monkeypatch):
    monkeypatch.setattr(adapters, "psutil", FakePsutil(fail={"disk"}))
    dev = lrig.device("dev-1")
    local_host(lrig)
    body = lrig.c.get("/v1/hosts/this-pc/health", headers=dev).json()
    assert body["state"] == "partial" and body["missing"] == ["disk_used_percent"]
    assert "disk_used_percent" not in body["metrics"]


def test_when_nothing_can_be_read_the_answer_is_a_clean_502(lrig: Rig, monkeypatch):
    monkeypatch.setattr(adapters, "psutil", FakePsutil(fail={"cpu", "mem", "disk", "boot"}))
    dev = lrig.device("dev-1")
    local_host(lrig)
    r = lrig.c.get("/v1/hosts/this-pc/health", headers=dev)
    assert r.status_code == 502
    assert r.json() == {
        "error": "host_unavailable",
        "detail": "host could not be reached",
        "state": "unreachable",
        "host_id": "this-pc",
    }
    assert "secret" not in r.text


def test_the_largest_local_report_stays_under_4_kib(lrig: Rig, monkeypatch):
    names = [f"Service.Name-{i:02d}" + "x" * 40 for i in range(16)]
    monkeypatch.setattr(adapters, "psutil", FakePsutil(services={n: "running" for n in names}))
    dev = lrig.device("dev-1")
    local_host(lrig, metrics=[*METRIC_NAMES], services=names)
    r = lrig.c.get("/v1/hosts/this-pc/health", headers=dev)
    assert r.status_code == 200 and len(r.content) < 4096
    assert len(r.json()["services"]) == 16


def test_enrolment_still_applies_every_allowlist_rule_to_local_hosts(lrig: Rig):
    base = {"host_id": "this-pc", "display_name": "x", "adapter": "local", "metrics": ["cpu_percent"]}
    for patch in (
        {"adapter": "ssh"},
        {"metrics": ["cpu_percent", "swap_percent"]},
        {"metrics": ["services"], "services": []},
        {"metrics": ["cpu_percent"], "services": ["Spooler"]},
        {"metrics": ["services"], "services": ["a b"]},
        {"command": "id"},
    ):
        r = lrig.c.post("/v1/admin/hosts", headers=OWNER, json={**base, **patch})
        assert r.status_code == 422, (patch, r.text)


def test_the_device_app_cannot_enrol_a_local_host(tmp_path: Path, clock: Clock):
    rig = Rig(tmp_path, clock, audience="device", adapter_factory=build_adapter)
    try:
        body = {"host_id": "h", "display_name": "x", "adapter": "local", "metrics": ["cpu_percent"]}
        assert rig.c.post("/v1/admin/hosts", headers=OWNER, json=body).status_code in (403, 404)
        assert rig.c.post("/v1/admin/hosts", headers=rig.device(), json=body).status_code in (403, 404)
    finally:
        rig.close()


# --- the seed helper ----------------------------------------------------------------------------------------
def test_seed_enrols_the_local_pc_for_the_given_devices(lrig: Rig, fake: FakePsutil):
    result = ensure_local_host(lrig.companion.registry, ["dev-1"], display_name="Desk PC")
    assert (result.created, result.changed) == (True, True)
    rec = result.host
    assert (rec.host_id, rec.display_name, rec.adapter) == ("this-pc", "Desk PC", "local")
    assert rec.metrics == list(LOCAL_HOST_METRICS) and rec.services == [] and rec.device_ids == ["dev-1"]
    r = lrig.c.get("/v1/hosts/this-pc/health", headers=lrig.device("dev-1"))
    assert r.status_code == 200 and r.json()["state"] == "ok"
    assert lrig.c.get("/v1/hosts/this-pc/health", headers=lrig.device("dev-2")).status_code == 404


def test_seed_is_idempotent_and_replaces_only_what_differs(lrig: Rig, clock: Clock):
    reg = lrig.companion.registry
    first = ensure_local_host(reg, ["dev-1", "dev-2"])
    clock.advance(60)
    again = ensure_local_host(reg, ["dev-2", "dev-1"])  # same set, other order
    assert (again.created, again.changed) == (False, False)
    assert again.host == first.host and again.host.updated_at == first.host.updated_at

    clock.advance(60)
    moved = ensure_local_host(reg, ["dev-1"], services=["Spooler"])
    assert (moved.created, moved.changed) == (False, True)
    assert moved.host.created_at == first.host.created_at
    assert moved.host.updated_at == first.host.updated_at + 120
    assert moved.host.metrics == [*LOCAL_HOST_METRICS, "services"] and moved.host.services == ["Spooler"]
    assert len(reg.all_hosts()) == 1


def test_seed_works_on_a_bare_store_and_can_audit(tmp_path: Path, clock: Clock):
    store = CompanionStore(tmp_path / "companion.db", clock=clock)
    try:
        result = ensure_local_host(store, ["dev-1"], "pc-2", "Second PC", clock=clock)
        assert result.created and [h.host_id for h in store.list_hosts()] == ["pc-2"]
        assert not ensure_local_host(store, ["dev-1"], "pc-2", "Second PC", clock=clock).changed
    finally:
        store.close()


def test_seed_audit_entry_is_written_only_when_something_changed(lrig: Rig):
    comp = lrig.companion
    ensure_local_host(comp.registry, ["dev-1"], audit=comp.audit)
    ensure_local_host(comp.registry, ["dev-1"], audit=comp.audit)
    entries = [e for e in lrig.audit() if e["action"] == "host_enrolled"]
    assert len(entries) == 1
    assert entries[0]["target"] == "this-pc" and "by seed" in entries[0]["detail"]


@pytest.mark.parametrize(
    "kw",
    [
        {"host_id": "has space"},
        {"host_id": "x" * 41},
        {"display_name": ""},
        {"display_name": "line\nbreak"},
        {"services": ["a b"]},
        {"services": [f"s{i}" for i in range(17)]},
        {"services": ["dup", "dup"]},
    ],
)
def test_seed_refuses_what_the_api_would_refuse(lrig: Rig, kw):
    with pytest.raises(ValueError):  # pydantic's ValidationError is a ValueError
        ensure_local_host(lrig.companion.registry, ["dev-1"], **kw)
    assert lrig.companion.registry.all_hosts() == []


def test_seed_refuses_device_lists_the_api_would_refuse(lrig: Rig):
    for bad in (["bad id"], ["a", "a"], [f"d{i}" for i in range(17)]):
        with pytest.raises(ValueError):
            ensure_local_host(lrig.companion.registry, bad)


def test_seed_respects_the_host_cap(lrig: Rig):
    for i in range(16):
        lrig.host(f"h{i}")
    with pytest.raises(Conflict):
        ensure_local_host(lrig.companion.registry, ["dev-1"])
    assert len(lrig.companion.registry.all_hosts()) == 16
