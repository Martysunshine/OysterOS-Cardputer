"""The Windows executor against a FAKE operating-system layer: every action type, refusals, elevation, the
Wake-on-LAN allowlist, failures and timeouts, and the full job path through the runner.

Nothing here presses a key, locks a screen, restarts a service or sends a packet: the real layer
(`os_windows.py`) is constructed only in the last section, and only its read-only calls are made.
"""

from __future__ import annotations

import sys
import threading
import types
from dataclasses import dataclass

import pytest
from oyster.deck import mount
from oyster.deck.actions import (
    ALLOWLIST,
    MEDIA_KEYS,
    ActionOutcomeUnknown,
    ActionRejected,
    ActionResult,
    MockExecutor,
    parse_action,
)
from oyster.deck.executor_windows import (
    ExecutorConfigError,
    HostHealth,
    OsFailure,
    OsLayer,
    WindowsExecutor,
    WindowsExecutorConfig,
    normalize_mac,
    parse_service_list,
    parse_wol_hosts,
)
from oyster.deck.mount import DeckRuntime, executor_from_env, shared_runtime
from oyster.deck.seed import ensure_desk_profile

MAC = "aa:bb:cc:dd:ee:ff"


class FakeOs:
    """Records every call; can be told to fail, to report an unknown outcome or to block."""

    def __init__(self, *, elevated: bool = False, services: dict[str, str] | None = None):
        self.calls: list[tuple] = []
        self.elevated = elevated
        self.services = {"spooler": "running"} if services is None else services
        self.raises: dict[str, BaseException] = {}
        self.block: dict[str, threading.Event] = {}
        self.health = HostHealth(cpu_percent=12.4, memory_percent=63.2, disk_percent=71.0)

    def _enter(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        exc = self.raises.pop(name, None)
        if exc is not None:
            raise exc
        gate = self.block.get(name)
        if gate is not None:
            gate.wait(5)

    def send_media_key(self, key: str) -> None:
        self._enter("send_media_key", key)

    def lock_workstation(self) -> None:
        self._enter("lock_workstation")

    def host_health(self) -> HostHealth:
        self._enter("host_health")
        return self.health

    def service_status(self, name: str) -> str | None:
        self._enter("service_status", name)
        return self.services.get(name.lower())

    def wake_on_lan(self, mac: str) -> None:
        self._enter("wake_on_lan", mac)

    def is_elevated(self) -> bool:
        self._enter("is_elevated")
        return self.elevated

    def restart_service(self, name: str) -> None:
        self._enter("restart_service", name)

    def effects(self) -> list[tuple]:
        """Calls that change something (everything except the read-only probes)."""
        return [c for c in self.calls if c[0] not in ("is_elevated", "host_health", "service_status")]


def make_executor(**kw) -> tuple[WindowsExecutor, FakeOs]:
    fake = FakeOs(elevated=kw.pop("elevated", False))
    config = WindowsExecutorConfig(
        wol_hosts=kw.pop("wol_hosts", {"nas": MAC}),
        restart_services=kw.pop("restart_services", frozenset({"spooler"})),
        **kw,
    )
    return WindowsExecutor(fake, config), fake


def run(ex: WindowsExecutor, **action) -> ActionResult:
    return ex.execute(parse_action(action))


def test_the_fake_layer_implements_the_protocol_and_the_executor_names_itself():
    ex, fake = make_executor()
    assert isinstance(fake, OsLayer)
    assert ex.kind == "windows"


# --- media keys ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", MEDIA_KEYS)
def test_every_media_key_is_sent_once_to_this_pc(key):
    ex, fake = make_executor()
    r = run(ex, type="media_key", host_id="this-pc", key=key)
    assert r.ok and r.code == "ok"
    assert fake.calls == [("send_media_key", key)]
    assert key in r.summary and "not checked" in r.summary  # a key press is not a proven effect
    assert len(r.summary) <= 120


def test_a_media_key_for_another_host_is_refused_without_touching_the_os():
    ex, fake = make_executor()
    r = run(ex, type="media_key", host_id="other-pc", key="mute")
    assert not r.ok and r.code == "host_not_local" and "this PC" in r.summary
    assert fake.calls == []


def test_the_local_host_id_is_configurable():
    ex, fake = make_executor(local_host_id="desk-pc")
    assert run(ex, type="media_key", host_id="desk-pc", key="next").ok
    assert not run(ex, type="media_key", host_id="this-pc", key="next").ok
    assert fake.calls == [("send_media_key", "next")]


def test_a_blocked_key_press_is_a_known_failure():
    ex, fake = make_executor()
    fake.raises["send_media_key"] = OsFailure("input_blocked", "Windows blocked the key press")
    r = run(ex, type="media_key", host_id="this-pc", key="play_pause")
    assert not r.ok and r.code == "input_blocked" and "blocked" in r.summary


# --- lock ---------------------------------------------------------------------------------------------


def test_lock_workstation_locks_this_pc_only():
    ex, fake = make_executor()
    r = run(ex, type="lock_workstation", host_id="this-pc")
    assert r.ok and "accepted" in r.summary and fake.calls == [("lock_workstation",)]
    refused = run(ex, type="lock_workstation", host_id="other-pc")
    assert not refused.ok and refused.code == "host_not_local"
    assert fake.calls == [("lock_workstation",)]


def test_a_refused_lock_is_a_known_failure_and_a_lost_result_is_unknown():
    ex, fake = make_executor()
    fake.raises["lock_workstation"] = OsFailure("lock_failed", "Windows refused the lock request")
    r = run(ex, type="lock_workstation", host_id="this-pc")
    assert not r.ok and r.code == "lock_failed"
    fake.raises["lock_workstation"] = ActionOutcomeUnknown("lost")
    with pytest.raises(ActionOutcomeUnknown):
        run(ex, type="lock_workstation", host_id="this-pc")
    fake.raises["lock_workstation"] = RuntimeError("anything unexpected is not swallowed")
    with pytest.raises(RuntimeError):
        run(ex, type="lock_workstation", host_id="this-pc")


# --- read-only actions --------------------------------------------------------------------------------


def test_noop_succeeds_without_touching_the_os():
    ex, fake = make_executor()
    assert run(ex, type="noop").ok and fake.calls == []


def test_host_health_reports_cpu_memory_and_disk():
    ex, fake = make_executor()
    r = run(ex, type="host_health_check", host_id="this-pc")
    assert r.ok and r.summary == "cpu 12% mem 63% disk 71%"
    refused = run(ex, type="host_health_check", host_id="nas")
    assert not refused.ok and refused.code == "host_not_local"
    assert [c[0] for c in fake.calls] == ["host_health"]


def test_service_status_reports_the_state_or_that_the_service_is_missing():
    ex, fake = make_executor()
    r = run(ex, type="service_status", host_id="this-pc", service="Spooler")
    assert r.ok and r.summary == "Spooler: running"
    missing = run(ex, type="service_status", host_id="this-pc", service="nosuchsvc")
    assert not missing.ok and missing.code == "service_not_found"
    assert not run(ex, type="service_status", host_id="nas", service="Spooler").ok
    assert len(fake.calls) == 2  # the foreign host never reached the OS


def test_an_access_denied_status_read_is_a_known_failure():
    ex, fake = make_executor()
    fake.raises["service_status"] = OsFailure("access_denied", "Windows did not allow reading this service")
    r = run(ex, type="service_status", host_id="this-pc", service="Spooler")
    assert not r.ok and r.code == "access_denied"


# --- Wake-on-LAN --------------------------------------------------------------------------------------


def test_wake_on_lan_uses_the_mac_from_the_allowlist_only():
    ex, fake = make_executor(wol_hosts={"nas": MAC})
    r = run(ex, type="wake_on_lan", host_id="nas")
    assert r.ok and "not verified" in r.summary
    assert fake.calls == [("wake_on_lan", MAC)]


def test_a_host_that_is_not_on_the_wol_allowlist_is_refused():
    ex, fake = make_executor(wol_hosts={"nas": MAC})
    for host in ("this-pc", "other", "NAS"):
        r = run(ex, type="wake_on_lan", host_id=host)
        assert not r.ok and r.code == "host_not_allowed"
    assert fake.calls == []


def test_no_request_field_can_carry_a_mac_address():
    for extra in ({"mac": MAC}, {"address": MAC}, {"broadcast": "255.255.255.255"}, {"host_id": MAC}):
        with pytest.raises(ActionRejected):
            parse_action({"type": "wake_on_lan", "host_id": "nas", **extra})
    assert "mac" not in "".join(ALLOWLIST["wake_on_lan"].model_fields)


def test_the_wol_allowlist_is_a_read_only_copy():
    source = {"nas": MAC}
    config = WindowsExecutorConfig(wol_hosts=source)
    source["evil"] = "11:22:33:44:55:66"
    assert dict(config.wol_hosts) == {"nas": MAC}
    with pytest.raises(TypeError):
        config.wol_hosts["evil"] = "11:22:33:44:55:66"  # type: ignore[index]


def test_a_failed_send_is_a_known_failure():
    ex, fake = make_executor()
    fake.raises["wake_on_lan"] = OsFailure("wol_send_failed", "could not send the magic packet")
    r = run(ex, type="wake_on_lan", host_id="nas")
    assert not r.ok and r.code == "wol_send_failed"


# --- service restart ----------------------------------------------------------------------------------


def test_restart_without_administrator_rights_fails_honestly_and_restarts_nothing():
    ex, fake = make_executor(elevated=False)
    r = run(ex, type="service_restart", host_id="this-pc", service="Spooler")
    assert not r.ok and r.code == "needs_admin"
    assert "administrator" in r.summary and "nothing was restarted" in r.summary
    assert fake.effects() == []
    assert ("is_elevated",) in fake.calls


def test_restart_when_elevated_restarts_an_allowlisted_service_once():
    ex, fake = make_executor(elevated=True)
    r = run(ex, type="service_restart", host_id="this-pc", service="SPOOLER")  # names are case-insensitive
    assert r.ok and "restarted" in r.summary
    assert fake.effects() == [("restart_service", "SPOOLER")]


def test_restart_of_a_service_outside_the_executor_allowlist_is_refused_even_when_elevated():
    ex, fake = make_executor(elevated=True)
    r = run(ex, type="service_restart", host_id="this-pc", service="wuauserv")
    assert not r.ok and r.code == "service_not_allowed"
    assert fake.calls == []  # the elevation is not even probed


def test_the_default_restart_allowlist_is_empty():
    fake = FakeOs(elevated=True)
    ex = WindowsExecutor(fake)  # default config
    r = run(ex, type="service_restart", host_id="this-pc", service="Spooler")
    assert not r.ok and r.code == "service_not_allowed" and fake.calls == []


def test_restart_for_another_host_is_refused():
    ex, fake = make_executor(elevated=True)
    r = run(ex, type="service_restart", host_id="nas", service="Spooler")
    assert not r.ok and r.code == "host_not_local" and fake.calls == []


def test_a_restart_that_stopped_the_service_but_could_not_start_it_is_a_known_failure():
    ex, fake = make_executor(elevated=True)
    fake.raises["restart_service"] = OsFailure("restart_incomplete", "service was stopped but not started")
    r = run(ex, type="service_restart", host_id="this-pc", service="Spooler")
    assert not r.ok and r.code == "restart_incomplete"
    fake.raises["restart_service"] = ActionOutcomeUnknown("did not report running in time")
    with pytest.raises(ActionOutcomeUnknown):
        run(ex, type="service_restart", host_id="this-pc", service="Spooler")


# --- configuration ------------------------------------------------------------------------------------


def test_macs_are_normalised():
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == MAC
    assert normalize_mac("aa:bb:cc:dd:ee:ff") == MAC
    for bad in ("", "aabbccddeeff", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:gg", "aa:bb:cc:dd:ee:ff:00", "aa bb"):
        with pytest.raises(ValueError):
            normalize_mac(bad)


def test_wol_hosts_are_parsed_from_host_equals_mac_pairs():
    assert parse_wol_hosts("") == {}
    assert parse_wol_hosts(" nas = AA:BB:CC:DD:EE:FF ; media=11-22-33-44-55-66 ;") == {
        "nas": MAC,
        "media": "11:22:33:44:55:66",
    }


@pytest.mark.parametrize("text", ["nas", "nas=", "=aa:bb:cc:dd:ee:ff", "bad host=aa:bb:cc:dd:ee:ff", "n=zz"])
def test_invalid_wol_entries_are_rejected_without_echoing_the_value(text):
    with pytest.raises(ExecutorConfigError) as e:
        parse_wol_hosts(text)
    assert "zz" not in str(e.value) and "bad host" not in str(e.value)


def test_service_lists_are_validated_and_lowercased():
    assert parse_service_list("") == frozenset()
    assert parse_service_list("Spooler, wuauserv ,") == frozenset({"spooler", "wuauserv"})
    with pytest.raises(ExecutorConfigError):
        parse_service_list("good,bad name;rm")


def test_config_from_env_has_safe_defaults_and_validates():
    cfg = WindowsExecutorConfig.from_env({})
    assert (
        cfg.local_host_id == "this-pc" and dict(cfg.wol_hosts) == {} and cfg.restart_services == frozenset()
    )
    cfg = WindowsExecutorConfig.from_env(
        {
            "OYSTER_DECK_LOCAL_HOST_ID": "desk",
            "OYSTER_DECK_WOL_HOSTS": f"nas={MAC}",
            "OYSTER_DECK_RESTART_SERVICES": "Spooler",
            "OYSTER_DECK_WOL_BROADCAST": "203.0.113.255",
        }
    )
    assert cfg.local_host_id == "desk" and dict(cfg.wol_hosts) == {"nas": MAC}
    assert cfg.restart_services == {"spooler"} and cfg.wol_broadcast == "203.0.113.255"
    for bad in (
        {"OYSTER_DECK_LOCAL_HOST_ID": "has space"},
        {"OYSTER_DECK_WOL_BROADCAST": "not-an-address"},
        {"OYSTER_DECK_WOL_BROADCAST": "::1"},
    ):
        with pytest.raises(ExecutorConfigError):
            WindowsExecutorConfig.from_env(bad)


def test_executor_selection_defaults_to_the_mock():
    assert isinstance(executor_from_env({}), MockExecutor)
    assert isinstance(executor_from_env({"OYSTER_DECK_EXECUTOR": ""}), MockExecutor)
    assert isinstance(executor_from_env({"OYSTER_DECK_EXECUTOR": " MOCK "}), MockExecutor)


def test_executor_selection_builds_the_windows_executor_with_the_given_layer():
    fake = FakeOs()
    env = {
        "OYSTER_DECK_EXECUTOR": "windows",
        "OYSTER_DECK_LOCAL_HOST_ID": "desk",
        "OYSTER_DECK_WOL_HOSTS": f"nas={MAC}",
    }
    ex = executor_from_env(env, os_layer=fake)
    assert isinstance(ex, WindowsExecutor) and ex.os is fake and ex.kind == "windows"
    assert ex.config.local_host_id == "desk" and dict(ex.config.wol_hosts) == {"nas": MAC}


def test_an_unknown_executor_name_stops_start_up_instead_of_falling_back():
    with pytest.raises(ExecutorConfigError) as e:
        executor_from_env({"OYSTER_DECK_EXECUTOR": "linux-shell"})
    assert "linux-shell" not in str(e.value)


def test_a_test_run_never_builds_the_real_windows_layer_on_its_own():
    """The owner's backend/.env may select `windows`; load_settings() puts it into the process environment."""
    ex = executor_from_env({"OYSTER_DECK_EXECUTOR": "windows"})  # no os_layer given
    assert isinstance(ex, MockExecutor)


def test_the_shared_runtime_reads_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("OYSTER_DECK_EXECUTOR", raising=False)
    rt = shared_runtime(tmp_path / "a")
    try:
        assert rt.executor.kind == "mock"
    finally:
        mount._shared.pop((tmp_path / "a").resolve(), None)
        rt.close()
    monkeypatch.setenv("OYSTER_DECK_EXECUTOR", "bogus")
    with pytest.raises(ExecutorConfigError):
        shared_runtime(tmp_path / "b")
    assert (tmp_path / "b").resolve() not in mount._shared


# --- the whole job path with the Windows executor -----------------------------------------------------


@dataclass
class Rig:
    os: FakeOs
    rt: DeckRuntime
    executor: WindowsExecutor

    def run(self, slot: int, key: str, device: str = "dev-a"):
        job, _ = self.rt.service.run_slot(device, "desk", slot, key)
        return job

    def finish(self, job_id: str):
        self.rt.runner.run_pending()
        return self.rt.service.get_job(job_id)

    def confirm(self, job, approved: bool = True, device: str = "dev-a"):
        return self.rt.service.confirm(device, job.job_id, job.job_id, job.nonce, approved)


@pytest.fixture
def rig(tmp_path):
    executor, fake = make_executor()
    rt = DeckRuntime(tmp_path, executor=executor, autostart=False, synchronous="OFF")
    assert ensure_desk_profile(rt, ["dev-a"]) == "created"
    yield Rig(fake, rt, executor)
    rt.close()


def test_jobs_are_labelled_with_the_windows_executor_and_run_media_keys_without_confirmation(rig):
    job = rig.run(2, "key-media-0001")
    assert job.executor == "windows" and job.state == "queued" and not job.impactful
    done = rig.finish(job.job_id)
    assert done.state == "succeeded" and done.steps[0]["outcome"] == "succeeded"
    assert "play_pause" in done.steps[0]["summary"]
    assert rig.os.calls == [("send_media_key", "play_pause")]


def test_the_desk_slots_press_the_documented_keys(rig):
    expected = {2: "play_pause", 3: "next", 4: "prev", 5: "vol_up", 6: "vol_down", 7: "mute"}
    for slot in expected:
        rig.finish(rig.run(slot, f"key-desk-slot{slot}").job_id)
    assert rig.os.calls == [("send_media_key", k) for k in expected.values()]


def test_the_health_slot_reports_real_looking_numbers_from_the_layer(rig):
    done = rig.finish(rig.run(1, "key-health-001").job_id)
    assert done.state == "succeeded" and done.steps[0]["summary"] == "cpu 12% mem 63% disk 71%"


def test_locking_the_pc_waits_for_the_confirmation_and_runs_once(rig):
    job = rig.run(8, "key-lock-00001")
    assert job.state == "awaiting_confirmation" and job.impactful and job.nonce
    assert rig.rt.runner.run_pending() == 0 and rig.os.calls == []  # nothing happens before the approval
    rig.confirm(job)
    done = rig.finish(job.job_id)
    assert done.state == "succeeded" and rig.os.calls == [("lock_workstation",)]
    replay, duplicate = rig.rt.service.run_slot("dev-a", "desk", 8, "key-lock-00001")
    assert duplicate and replay.state == "succeeded"
    assert rig.rt.runner.run_pending() == 0 and rig.os.calls == [("lock_workstation",)]  # a key never re-runs


def test_a_rejected_lock_never_reaches_the_os(rig):
    job = rig.run(8, "key-lock-00002")
    assert rig.confirm(job, approved=False).state == "cancelled"
    assert rig.rt.runner.run_pending() == 0 and rig.os.calls == []


def test_a_refused_lock_ends_the_job_failed_with_the_reason(rig):
    rig.os.raises["lock_workstation"] = OsFailure("lock_failed", "Windows refused the lock request")
    job = rig.run(8, "key-lock-00003")
    rig.confirm(job)
    done = rig.finish(job.job_id)
    assert done.state == "failed" and done.reason == "lock_failed"
    assert done.steps[0]["summary"] == "Windows refused the lock request"


def test_a_lost_lock_result_ends_unknown_outcome_and_is_not_retried(rig):
    rig.os.raises["lock_workstation"] = ActionOutcomeUnknown("lost")
    job = rig.run(8, "key-lock-00004")
    rig.confirm(job)
    done = rig.finish(job.job_id)
    assert done.state == "unknown_outcome" and done.reason == "lost_result"
    assert rig.rt.runner.run_pending() == 0 and rig.os.calls == [("lock_workstation",)]


def test_a_hanging_media_key_is_cut_off_by_the_runner_and_fails(rig):
    rig.rt.runner.timeout_of = lambda action: 0.05
    gate = rig.os.block["send_media_key"] = threading.Event()
    try:
        done = rig.finish(rig.run(2, "key-hang-0001").job_id)
    finally:
        gate.set()
    assert done.state == "failed" and done.reason == "timeout"


def test_a_hanging_lock_is_cut_off_by_the_runner_and_is_unknown(rig):
    rig.rt.runner.timeout_of = lambda action: 0.05
    gate = rig.os.block["lock_workstation"] = threading.Event()
    job = rig.run(8, "key-hang-0002")
    rig.confirm(job)
    try:
        done = rig.finish(job.job_id)
    finally:
        gate.set()
    assert done.state == "unknown_outcome" and done.reason == "timeout"


def test_an_unexpected_os_error_is_unknown_for_lock_and_hides_its_text(rig):
    rig.os.raises["lock_workstation"] = RuntimeError("secret detail from the OS")
    job = rig.run(8, "key-err-00001")
    rig.confirm(job)
    done = rig.finish(job.job_id)
    assert done.state == "unknown_outcome" and done.reason == "executor_error"
    assert "secret" not in str(done)


def test_a_slot_for_a_foreign_host_fails_without_effect(tmp_path):
    """A profile may name any host it lists; the executor still only controls this PC."""
    executor, fake = make_executor()
    rt = DeckRuntime(tmp_path, executor=executor, autostart=False, synchronous="OFF")
    try:
        ensure_desk_profile(rt, ["dev-a"], local_host_id="other-pc")  # profile names a host this PC is not
        job, _ = rt.service.run_slot("dev-a", "desk", 2, "key-foreign-001")
        rt.runner.run_pending()
        done = rt.service.get_job(job.job_id)
        assert done.state == "failed" and done.reason == "host_not_local"
        assert fake.calls == []
    finally:
        rt.close()


# --- the real layer: constructed and probed READ-ONLY, never used to press, lock or restart -------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_the_win32_structures_have_the_sizes_sendinput_expects():
    import ctypes

    from oyster.deck import os_windows as w

    assert ctypes.sizeof(w.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert ctypes.sizeof(w.KEYBDINPUT) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
    assert ctypes.sizeof(w.SERVICE_STATUS) == 28
    event = w._key_input(w.VK_BY_KEY["play_pause"], w.KEYEVENTF_EXTENDEDKEY)
    assert (event.type, event.ki.wVk, event.ki.dwFlags) == (1, 0xB3, 1)  # built, not sent


def test_the_virtual_key_table_covers_every_media_key_exactly():
    from oyster.deck import os_windows as w

    assert set(w.VK_BY_KEY) == set(MEDIA_KEYS)
    assert w.VK_BY_KEY == {
        "mute": 0xAD,
        "vol_down": 0xAE,
        "vol_up": 0xAF,
        "next": 0xB0,
        "prev": 0xB1,
        "play_pause": 0xB3,
    }


def test_the_real_layer_refuses_to_exist_off_windows(monkeypatch):
    from oyster.deck import os_windows as w

    monkeypatch.setattr(w, "sys", types.SimpleNamespace(platform="linux"))
    with pytest.raises(ExecutorConfigError):
        w.WindowsOsLayer()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_the_real_layer_answers_read_only_questions():
    from oyster.deck.os_windows import WindowsOsLayer

    layer = WindowsOsLayer()
    assert isinstance(layer, OsLayer)
    assert isinstance(layer.is_elevated(), bool)
    health = layer.host_health()
    assert all(0 <= v <= 100 for v in (health.cpu_percent, health.memory_percent, health.disk_percent))
    assert layer.service_status("oyster-test-no-such-service-9f3a") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_restarting_a_service_that_does_not_exist_changes_nothing():
    """Exercises the service-control bindings with a name no service has; nothing can be stopped."""
    from oyster.deck.os_windows import WindowsOsLayer

    with pytest.raises(OsFailure) as e:
        WindowsOsLayer().restart_service("oyster-test-no-such-service-9f3a")
    assert e.value.code == "service_not_found"
