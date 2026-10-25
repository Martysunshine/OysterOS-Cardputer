"""Each scenario runs end-to-end against a fake backend and produces its intended statuses,
with every event valid against schemas/event.schema.json and labelled simulated."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _simulator_backend import FakeBackend
from jsonschema import Draft202012Validator
from oyster_lab.simulator.device import Simulator
from oyster_lab.simulator.scenarios import PROMPT_INJECTION_TEXT, list_scenarios, load_scenario

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
EVENT_VALIDATOR = Draft202012Validator(
    json.loads((SCHEMAS / "event.schema.json").read_text(encoding="utf-8"))
)

PLAIN_SCENARIOS = [n for n in list_scenarios() if n != "withheld"]


def _run(name: str):
    scenario = load_scenario(name)
    backend = FakeBackend()
    client = backend.client()
    sim = Simulator(scenario, client, auto_confirm=True, confirm_wait_s=0.0)
    session_id = sim.register_session()
    backend.enqueue_plan(scenario, session_id)
    report = sim.run()
    client.close()
    return scenario, backend, report


@pytest.mark.parametrize("name", PLAIN_SCENARIOS)
def test_scenario_outcomes_match_expected(name):
    scenario, backend, report = _run(name)
    outcomes = {jr.job_id: jr.outcome for jr in report.jobs}
    for job_id, expected in scenario.expected_outcomes.items():
        got = outcomes.get(job_id)
        assert got == expected, f"{name}:{job_id} expected {expected}, got {got}"


@pytest.mark.parametrize("name", PLAIN_SCENARIOS)
def test_events_validate_and_are_simulated(name):
    _scenario, backend, _report = _run(name)
    assert backend.events, f"{name} produced no events"
    for event in backend.events.values():
        errors = sorted(EVENT_VALIDATOR.iter_errors(event), key=str)
        assert not errors, f"{name} event {event['event_id']} invalid: {errors[0].message if errors else ''}"
        assert event["source_mode"] == "simulated"


def test_success_injection_strings_are_stored_as_data():
    _scenario, backend, _report = _run("success")
    ssids = []
    ble_names = []
    for e in backend.events.values():
        for obs in e["details"].get("observations", []):
            if e["operation"] == "wifi_survey":
                ssids.append(obs.get("ssid_display"))
            if e["operation"] == "ble_discover":
                ble_names.append(obs.get("name"))
    assert PROMPT_INJECTION_TEXT in ssids
    assert PROMPT_INJECTION_TEXT in ble_names


def test_vulnerable_returns_diagnostics_while_unauthorized():
    _scenario, backend, _report = _run("vulnerable_target")
    check = next(e for e in backend.events.values() if e["operation"] == "ble_check_access_policy")
    assert check["details"]["access"] == "returned"
    assert check["details"]["security_after"]["application_authorized"] is False


def test_corrected_denies_before_auth_then_verifies_hash():
    _scenario, backend, _report = _run("corrected_target")
    check = next(e for e in backend.events.values() if e["operation"] == "ble_check_access_policy")
    assert check["details"]["access"] == "denied_app"
    collect = next(e for e in backend.events.values() if e["operation"] == "ble_collect_diagnostics")
    assert collect["status"] == "success"
    assert collect["details"]["hash_verified"] is True


def test_transfer_interrupted_is_unknown_outcome_and_partial():
    _scenario, backend, report = _run("transfer_interrupted")
    assert report.jobs[0].outcome == "unknown_outcome"
    collect = next(e for e in backend.events.values() if e["operation"] == "ble_collect_diagnostics")
    assert collect["status"] == "partial"
    assert collect["details"]["hash_verified"] is False


def test_unexpected_pairing_transition_is_recorded():
    _scenario, backend, _report = _run("unexpected_pairing_transition")
    check = next(e for e in backend.events.values() if e["operation"] == "ble_check_access_policy")
    assert check["details"]["security_transition"] is True
    assert check["details"]["security_before"] != check["details"]["security_after"]


def test_malformed_advertisement_is_preserved():
    _scenario, backend, _report = _run("malformed_advertisement")
    malformed = [
        obs
        for e in backend.events.values()
        if e["operation"] == "ble_discover"
        for obs in e["details"].get("observations", [])
        if obs.get("malformed")
    ]
    assert malformed, "no malformed advertisement recorded"


def test_address_change_is_recorded():
    _scenario, backend, _report = _run("address_change")
    summary = next(
        e for e in backend.events.values()
        if e["operation"] == "ble_discover" and "address_changed" in e["details"]
    )
    assert summary["details"]["address_changed"] is True
