"""Lab hardening: the simulator must agree with the real fixture protocol, validate typed
arguments, never lose a job to an exception, and the fixture follows the profile (challenge TTL, ATT 0x0D)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from _simulator_backend import FakeBackend
from oyster.contracts import Event
from oyster.policy.evaluators import evaluate_assertion
from oyster_lab.fixture.profile import load_profile, parse_profile
from oyster_lab.fixture.protocol import (
    ATT_INSUFFICIENT_AUTHORIZATION,
    ATT_INVALID_ATTRIBUTE_VALUE_LENGTH,
    CTRL,
    FixtureState,
    auth_response,
)
from oyster_lab.simulator import policy
from oyster_lab.simulator.device import Simulator, VirtualCardputer
from oyster_lab.simulator.scenarios import load_scenario
from oyster_lab.synthetic import APP_DENIAL

PROFILE_JSON = json.loads(
    (Path(__file__).resolve().parents[2] / "schemas" / "lab-profile.json").read_text(encoding="utf-8")
)
PROFILE = load_profile()
TOKEN = b"lab-test-token-0123456789abcdef-not-a-real-secret"
TARGET = "target-lab-01"
SESSION = "sess-conf-001"
ASSERTIONS = [a["test_id"] for a in PROFILE_JSON["assertions"]]
LINK_ATT_CODE = {"link_encryption": 0x0F, "link_authentication": 0x05}


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _assertion(test_id: str) -> dict:
    return next(a for a in PROFILE_JSON["assertions"] if a["test_id"] == test_id)


def _policy_of(assertion: dict) -> str:
    op = "read" if assertion["operation"] == "transfer" else assertion["operation"]
    return PROFILE_JSON["characteristics"][assertion["characteristic"]]["policy"][op]


# ------------------------------------------------------------------ conformance with the fixture protocol
def fixture_outcome(test_id: str, mode: str) -> tuple[str, str | None, int | None]:
    """(allowed|denied, layer app|att|None, ATT code) the pure protocol gives for one assertion."""
    st = FixtureState(PROFILE, mode, TOKEN)
    a = _assertion(test_id)
    policy_name = _policy_of(a)
    if policy_name == "public":
        assert st.handle_read(a["characteristic"]).value is not None
        return "allowed", None, None
    if policy_name in LINK_ATT_CODE:
        # the state machine returns the value: the OS protection level (an ATT error) is the enforcer
        assert st.handle_read(a["characteristic"]).value is not None
        return "denied", "att", LINK_ATT_CODE[policy_name]
    # application authorization
    if a["characteristic"] == "virtual_led":
        res = st.handle_write("virtual_led", bytes([2]))
        assert res.att_error == ATT_INSUFFICIENT_AUTHORIZATION
        return "denied", "att", res.att_error
    if a["precondition"] == "app_authorized":
        challenge = st.handle_read("app_auth").value[1:]
        assert st.handle_write("app_auth", auth_response(TOKEN, challenge)).att_error is None
    manifest = st.handle_read("diag_manifest").value
    if manifest == APP_DENIAL:
        return "denied", "app", None
    assert manifest == st.manifest
    return "allowed", None, None


def simulator_event(test_id: str, mode: str) -> tuple[dict, str]:
    scenario = dataclasses.replace(load_scenario("corrected_target"), fixture_mode=mode)
    dev = VirtualCardputer(scenario, "sim-cardputer-01", "boot-conf0001")
    dev.set_session(SESSION)
    res = dev.execute(
        {
            "job_id": "job-conf-001",
            "operation": "ble_check_access_policy",
            "target_id": TARGET,
            "arguments": {"test_id": test_id},
        }
    )
    assert len(res.events) == 1 and res.outcome == "succeeded"
    return res.events[0], res.outcome


@pytest.mark.parametrize("mode", ["vulnerable", "corrected"])
@pytest.mark.parametrize("test_id", ASSERTIONS)
def test_simulator_agrees_with_the_fixture_protocol_on_every_assertion(test_id, mode):
    allowed, layer, att = fixture_outcome(test_id, mode)
    event, _ = simulator_event(test_id, mode)
    d = event["details"]
    assert d["characteristic_id"] == _assertion(test_id)["characteristic"]
    assert d["precondition"] == _assertion(test_id)["precondition"] and d["precondition_established"] is True
    if allowed == "allowed":
        assert d["access"] == "returned" and event["status"] == "success"
    else:
        assert d["access"] == f"denied_{layer}" and event["status"] == "denied"
        assert d["raw_att_status"] == att  # None for the application-level denial
        assert (d["app_status"] == "not_authorized") == (layer == "app")


@pytest.mark.parametrize("mode", ["vulnerable", "corrected"])
def test_simulator_events_classify_like_the_fixture_behaviour(mode):
    """The evaluator reads the simulator's events: only the unauthorised diagnostics read in vulnerable mode
    is a violation; the LED write is denied in both modes."""
    for test_id in ASSERTIONS:
        event, _ = simulator_event(test_id, mode)
        finding = evaluate_assertion(
            PROFILE_JSON, test_id, [Event.model_validate(event)], session_id=SESSION, target_id=TARGET
        )
        cls = finding.classification.value
        if _assertion(test_id)["informational"]:
            assert cls == "observation", test_id
        elif test_id == "restricted_diagnostics_requires_app_authorization" and mode == "vulnerable":
            assert cls == "confirmed", test_id
        else:
            assert cls == "passed", (test_id, cls)
        assert finding.source_mode.value == "simulated"


def test_led_write_is_denied_with_att_0x08_in_vulnerable_mode_too():
    scenario = dataclasses.replace(load_scenario("vulnerable_target"), fixture_mode="vulnerable")
    dev = VirtualCardputer(scenario, "sim-cardputer-01", "boot-conf0001")
    dev.set_session(SESSION)
    res = dev.execute(
        {
            "job_id": "job-led-001",
            "operation": "ble_write_lab_value",
            "target_id": TARGET,
            "arguments": {"characteristic_id": "virtual_led", "value": "green", "restore": True},
        }
    )
    (event,) = res.events
    assert event["status"] == "denied"
    assert event["details"]["observed_result"] == "denied"
    assert (
        event["details"]["raw_att_status"]
        == 0x08
        == PROFILE_JSON["write_values"]["virtual_led"]["denial_att_status"]
    )
    assert res.outcome == "succeeded"


# ------------------------------------------------------------------ typed argument validation
def _sim():
    backend = FakeBackend()
    client = backend.client()
    sim = Simulator(load_scenario("corrected_target"), client, auto_confirm=True, confirm_wait_s=0.0)
    session_id = sim.register_session()
    return backend, client, sim, session_id


def _job(job_id, session_id, operation, arguments, *, nonce=None, confirm=False):
    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "session_id": session_id,
        "device_id": "sim-cardputer-01",
        "target_id": TARGET,
        "operation": operation,
        "arguments": arguments,
        "scope_version": 1,
        "deadline_s": 60,
        "limits": {"max_bytes": 512, "max_duration_s": 30},
        "requires_local_confirmation": confirm,
        "nonce": nonce,
    }


BAD_ARGS = [
    ("ble_write_lab_value", {"characteristic_id": "virtual_led", "value": "purple", "restore": True}),
    ("ble_write_lab_value", {"characteristic_id": "virtual_led", "value": 3}),
    ("ble_write_lab_value", {"characteristic_id": "public_sensor", "value": "green"}),
    ("ble_write_lab_value", {"characteristic_id": "virtual_led", "value": "green", "restore": "yes"}),
    ("ble_read_profile_value", {"characteristic_id": "no_such_char"}),
    ("ble_read_profile_value", {"characteristic_id": ["public_sensor"]}),
    ("ble_read_profile_value", {}),
    ("ble_observe_notifications", {"characteristic_id": "device_info"}),
    ("ble_check_access_policy", {"test_id": "not_an_assertion"}),
    ("ble_collect_diagnostics", {"object_id": "../../etc/passwd"}),
    ("ble_collect_diagnostics", {"object_id": "x" * 17}),
    ("ble_collect_diagnostics", {"object_id": "other-object-1"}),
    ("wifi_survey", {"ms_per_channel": "fast"}),
    ("ble_discover", {"duration_ms": None}),
]


@pytest.mark.parametrize(("operation", "arguments"), BAD_ARGS)
def test_bad_typed_arguments_are_rejected_with_a_failed_result_not_an_exception(operation, arguments):
    backend, client, sim, session_id = _sim()
    needs_target = operation in policy._TARGET_OPS
    job = _job("job-bad-001", session_id, operation, arguments, nonce="n-bad-001")
    if not needs_target:
        job["target_id"] = None
    backend.enqueue(job)
    report = sim.run()
    client.close()
    (jr,) = report.jobs
    assert jr.verdict == "reject_bad_arguments" and jr.outcome == "failed" and jr.executed is False
    assert backend.result_by_job["job-bad-001"]["outcome"] == "failed"
    assert backend.events == {}  # nothing ran, nothing was asked of the owner
    assert ("job-bad-001", "awaiting_local_confirmation") not in backend.status_log


def test_valid_typed_arguments_still_pass():
    prof = PROFILE_JSON
    ok = policy.validate_arguments
    assert ok("ble_write_lab_value", {"characteristic_id": "virtual_led", "value": "white"}, prof) is None
    assert ok("ble_collect_diagnostics", {"object_id": "diag-synth-0001"}, prof) is None
    assert ok("ble_collect_diagnostics", {}, prof) is None
    assert ok("ble_read_profile_value", {"characteristic_id": "diag_manifest"}, prof) is None  # refused later
    assert policy.OBJECT_ID_RE.match("diag-synth-0001") and not policy.OBJECT_ID_RE.match("a b")


def test_protected_reads_are_refused_before_the_target_is_touched():
    backend, client, sim, session_id = _sim()
    for i, cid in enumerate(("diag_manifest", "chunk_data", "app_auth", "restricted_enc")):
        backend.enqueue(
            _job(f"job-prot-{i:03d}", session_id, "ble_read_profile_value", {"characteristic_id": cid})
        )
    report = sim.run()
    client.close()
    assert [jr.outcome for jr in report.jobs] == ["failed"] * 4
    for e in backend.events.values():
        assert e["status"] == "denied" and e["details"]["attempted"] is False
        assert "value_hex" not in e["details"] and "value_sha256" not in e["details"]


def test_exception_in_a_handler_yields_a_failed_result_and_the_loop_continues(monkeypatch):
    backend, client, sim, session_id = _sim()
    real = VirtualCardputer.execute
    calls = {"n": 0}

    def flaky(self, job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom with secret-looking text s3cr3t")
        return real(self, job)

    monkeypatch.setattr(VirtualCardputer, "execute", flaky)
    for i in (1, 2):
        backend.enqueue(
            _job(
                f"job-rd-{i:03d}",
                session_id,
                "ble_read_profile_value",
                {"characteristic_id": "public_sensor"},
            )
        )
    report = sim.run()
    client.close()
    assert [jr.outcome for jr in report.jobs] == ["failed", "succeeded"]
    first = backend.result_by_job["job-rd-001"]
    assert (
        first["outcome"] == "failed" and "RuntimeError" in first["error"] and "s3cr3t" not in first["error"]
    )
    assert backend.result_by_job["job-rd-002"]["outcome"] == "succeeded"


def test_exception_before_execution_still_gets_a_result(monkeypatch):
    backend, client, sim, session_id = _sim()
    monkeypatch.setattr(policy, "validate_arguments", lambda *a, **k: 1 / 0)
    backend.enqueue(
        _job("job-zero-001", session_id, "ble_read_profile_value", {"characteristic_id": "public_sensor"})
    )
    report = sim.run()
    client.close()
    assert report.jobs[0].outcome == "failed"
    assert backend.result_by_job["job-zero-001"]["outcome"] == "failed"


# ------------------------------------------------------------------ fixture follows the profile
def _state(profile=PROFILE, mode="corrected", clock=None):
    return FixtureState(profile, mode, TOKEN, clock=clock or Clock())


def test_challenge_lifetime_comes_from_the_profile_not_a_constant():
    assert PROFILE.challenge_ttl_s == PROFILE_JSON["app_auth"]["challenge_ttl_s"] == 60
    short = json.loads(json.dumps(PROFILE_JSON))
    short["app_auth"]["challenge_ttl_s"] = 5
    profile = parse_profile(short)
    assert profile.challenge_ttl_s == 5
    clock = Clock()
    st = _state(profile, clock=clock)
    challenge = st.handle_read("app_auth").value[1:]
    clock.t += 6  # beyond the profile's 5 s, well inside the old hard-coded 60 s
    st.handle_write("app_auth", auth_response(TOKEN, challenge))
    assert st.is_authorized() is False
    challenge = st.handle_read("app_auth").value[1:]
    clock.t += 4
    st.handle_write("app_auth", auth_response(TOKEN, challenge))
    assert st.is_authorized() is True


def test_profile_without_challenge_ttl_is_rejected():
    broken = json.loads(json.dumps(PROFILE_JSON))
    del broken["app_auth"]["challenge_ttl_s"]
    with pytest.raises(ValueError, match="challenge_ttl_s"):
        parse_profile(broken)


def test_short_plain_write_to_chunk_ctrl_is_an_invalid_length():
    st = _state()
    for n in (0, 1, CTRL.size - 1):
        assert st.handle_write("chunk_ctrl", bytes(n)).att_error == ATT_INVALID_ATTRIBUTE_VALUE_LENGTH, n
    # nothing was held: a following full-length request is processed on its own
    full = CTRL.pack(1, st.tag, 0, 10, 1)
    assert st.handle_write("chunk_ctrl", full).att_error is None


def test_short_write_to_app_auth_is_invalid_when_the_mtu_fits_it_but_held_for_a_long_write():
    st = _state()
    challenge = st.handle_read("app_auth").value[1:]
    resp = auth_response(TOKEN, challenge)
    assert (
        st.handle_write("app_auth", resp[:18], offset=0, mtu=247).att_error
        == ATT_INVALID_ATTRIBUTE_VALUE_LENGTH
    )
    # default MTU: 32 bytes do not fit one write, so a short first part starts a long write
    assert st.handle_write("app_auth", resp[:18], offset=0, mtu=23).att_error is None
    assert st.handle_write("app_auth", resp[18:], offset=18, mtu=23).att_error is None
    assert st.is_authorized() is True  # the rejected short write did not consume the challenge


# ------------------------------------------------------------------ timeouts
def _timeout_event(test_id):
    scenario = load_scenario("corrected_target")
    scenario = dataclasses.replace(scenario, faults={"ble_check_access_policy": "timeout"})
    dev = VirtualCardputer(scenario, "sim-cardputer-01", "boot-conf0001")
    dev.set_session(SESSION)
    return dev.execute(
        {
            "job_id": "job-to-001",
            "operation": "ble_check_access_policy",
            "target_id": TARGET,
            "arguments": {"test_id": test_id},
        }
    )


@pytest.mark.parametrize(
    ("test_id", "expected"),
    [
        ("virtual_led_write_requires_app_authorization", "unknown_outcome"),
        ("restricted_diagnostics_requires_app_authorization", "unknown_outcome"),
        ("diagnostics_available_after_app_authorization", "unknown_outcome"),
        ("restricted_value_requires_link_encryption", "failed"),
        ("public_sensor_is_public", "failed"),
    ],
)
def test_timeout_in_a_write_or_transfer_check_is_unknown_outcome(test_id, expected):
    res = _timeout_event(test_id)
    assert res.outcome == expected
    assert res.events[0]["status"] == "timeout" and res.events[0]["details"]["access"] == "timeout"


def test_direct_write_timeout_is_unknown_outcome():
    scenario = dataclasses.replace(
        load_scenario("corrected_target"), faults={"ble_write_lab_value": "timeout"}
    )
    dev = VirtualCardputer(scenario, "sim-cardputer-01", "boot-conf0001")
    dev.set_session(SESSION)
    res = dev.execute(
        {
            "job_id": "job-to-002",
            "operation": "ble_write_lab_value",
            "target_id": TARGET,
            "arguments": {"characteristic_id": "virtual_led", "value": "red"},
        }
    )
    assert res.outcome == "unknown_outcome" and res.events[0]["status"] == "timeout"
