"""The simulator must only read argument names the backend catalogue defines for each operation, and
the scenario plans must only send those names. This guards against the device reading arguments the
backend/firmware never send (names like max_ms_per_channel, duration_ms for notifications, or an
`authorize` flag on diagnostics are not in the catalogue)."""

from __future__ import annotations

from oyster.policy import catalogue  # backend catalogue (imported by the TEST, never by the simulator)
from oyster_lab.simulator import events, policy
from oyster_lab.simulator.device import ARGUMENT_NAMES_READ
from oyster_lab.simulator.scenarios import list_scenarios, load_scenario


def _catalogue_props(operation: str) -> set[str]:
    return set(catalogue.OPERATIONS[operation].arguments_schema.get("properties", {}))


def test_every_operation_has_a_read_set():
    assert set(ARGUMENT_NAMES_READ) == set(policy.DEVICE_OPERATIONS)


def test_simulator_only_reads_catalogue_argument_names():
    for operation, names in ARGUMENT_NAMES_READ.items():
        props = _catalogue_props(operation)
        unknown = set(names) - props
        assert not unknown, f"{operation} reads argument(s) not in the catalogue: {sorted(unknown)}"


def test_required_arguments_are_read():
    for operation, spec in catalogue.OPERATIONS.items():
        required = set(spec.arguments_schema.get("required", []))
        read = set(ARGUMENT_NAMES_READ[operation])
        missing = required - read
        assert not missing, f"{operation} does not read required argument(s): {sorted(missing)}"


def test_scenario_plan_arguments_match_catalogue():
    for name in list_scenarios():
        if name == "withheld":
            continue
        scenario = load_scenario(name)
        for job in scenario.plan:
            op = job["operation"]
            props = _catalogue_props(op)
            extra = set(job.get("arguments", {})) - props
            assert not extra, f"{name}:{job['job_id']} sends non-catalogue argument(s): {sorted(extra)}"


def test_diagnostics_has_no_authorize_argument():
    # Authorization is decided by the device's own app-auth exchange / fixture state, not a job arg.
    assert "authorize" not in _catalogue_props("ble_collect_diagnostics")
    assert "authorize" not in ARGUMENT_NAMES_READ["ble_collect_diagnostics"]


def test_confirmation_and_state_change_agree_with_backend_for_every_operation_and_test_id():
    """The device derives the local-confirmation gate independently (it never imports backend code);
    both sides, and the AI tool catalogue, must reach the same answer for every job they can see."""
    from oyster.ai import tools as ai_tools  # AI side (also imported by the TEST only)

    profile = catalogue.load_profile("oyster-lab-v1")
    assert profile == events.load_profile()  # both read the same schemas/lab-profile.json
    for operation in policy.DEVICE_OPERATIONS:
        device_gate = policy.operation_requires_local_confirmation(operation)
        assert device_gate == catalogue.requires_local_confirmation(operation), operation
        assert policy.operation_changes_target_state(operation) == catalogue.changes_target_state(operation)
        assert ai_tools.changes_target_state(operation) == catalogue.changes_target_state(operation)
        if operation != "ble_check_access_policy":
            assert policy.job_requires_local_confirmation(operation, {}, profile) == (
                catalogue.job_requires_local_confirmation(operation, {}, profile)
            ), operation
    for a in profile["assertions"]:
        args = {"test_id": a["test_id"]}
        device = policy.job_requires_local_confirmation("ble_check_access_policy", args, profile)
        backend = catalogue.job_requires_local_confirmation("ble_check_access_policy", args, profile)
        ai = ai_tools.job_requires_local_confirmation("ble_check_access_policy", args, profile)
        assert device == backend == ai == (a["operation"] in ("write", "transfer")), a["test_id"]
