"""Contract tests: schemas/examples validate under jsonschema AND parse into the contracts.py models,
and every wire shape the backend emits validates against the same schemas."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from oyster import contracts
from oyster.api.common import wire_job
from oyster.evidence.validate import (
    EXAMPLE_KINDS,
    EXAMPLES_DIR,
    SCHEMAS_DIR,
    SchemaValidationError,
    example_kind,
    parse_event,
    schema_errors,
    validate_all,
    validate_example,
)
from oyster.policy import catalogue

EXAMPLES = sorted(EXAMPLES_DIR.glob("*.json"))


def test_examples_exist_for_every_kind():
    kinds = {example_kind(p) for p in EXAMPLES}
    assert None not in kinds, "every example file maps to a schema"
    assert set(EXAMPLE_KINDS) <= kinds


@pytest.mark.parametrize("schema_path", sorted(SCHEMAS_DIR.glob("*.schema.json")), ids=lambda p: p.name)
def test_schemas_are_valid_draft_2020_12(schema_path):
    Draft202012Validator.check_schema(json.loads(schema_path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_validates_and_parses(path):
    assert validate_example(path) == []
    schema_name, model = EXAMPLE_KINDS[example_kind(path)]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert schema_errors(schema_name, data) == []
    obj = model.model_validate(data)
    # round trip: the model's own JSON form still satisfies the schema (absent optional limits omitted)
    dumped = (
        wire_job(obj) if isinstance(obj, contracts.Job) else obj.model_dump(mode="json", exclude_unset=True)
    )
    assert schema_errors(schema_name, dumped) == []


def test_lab_profile_validates():
    results = validate_all()
    assert results["schemas/lab-profile.json"] == []
    assert all(v == [] for v in results.values()), {k: v for k, v in results.items() if v}


def test_schema_and_model_reject_the_same_bad_event():
    good = json.loads((EXAMPLES_DIR / "event.ble-read-denied.json").read_text(encoding="utf-8"))
    for mutate in (
        lambda e: e.update(source_mode="real"),
        lambda e: e.update(event_id="evt/../x"),
        lambda e: e.update(sequence=-1),
        lambda e: e.update(extra_field=1),
    ):
        bad = json.loads(json.dumps(good))
        mutate(bad)
        assert schema_errors("event.schema.json", bad), bad
        with pytest.raises(ValueError):
            contracts.Event.model_validate(bad)


def test_schema_and_model_reject_the_same_bad_run():
    good = json.loads((EXAMPLES_DIR / "run.succeeded.json").read_text(encoding="utf-8"))
    assert contracts.Run.model_validate(good).status is contracts.RunStatus.succeeded
    for mutate in (
        lambda r: r.update(status="done"),
        lambda r: r.update(run_id="run/../x"),
        lambda r: r.update(extra_field=1),
        lambda r: r.pop("pending_action"),
        lambda r: r["provider"].pop("is_fake"),
        lambda r: r["steps"][0].update(kind="thinking"),
        lambda r: r["steps"].extend([r["steps"][0]] * 32),
        lambda r: r["answer"].update(source_modes=["real"]),
        lambda r: r.update(error="x" * 501),
    ):
        bad = json.loads(json.dumps(good))
        mutate(bad)
        assert schema_errors("run.schema.json", bad), bad
        with pytest.raises(ValueError):
            contracts.Run.model_validate(bad)


def test_incoming_events_are_schema_checked_before_lax_model_coercion():
    """pydantic (lax mode) coerces "yes" to True; the backend therefore validates the JSON schema first."""
    bad = json.loads((EXAMPLES_DIR / "event.ble-read-denied.json").read_text(encoding="utf-8"))
    bad["details"]["security_state"] = {"encrypted": "yes"}
    assert contracts.Event.model_validate(bad).details.security_state.encrypted is True
    with pytest.raises(SchemaValidationError):
        parse_event(bad)


def test_job_without_limits_serializes_schema_valid():
    job = contracts.Job(
        job_id="job-1",
        session_id="sess-1",
        operation="wifi_survey",
        scope_version=1,
        deadline_s=30,
        requires_local_confirmation=False,
    )
    assert schema_errors("job.schema.json", job.model_dump(mode="json")), "nulls in limits violate the schema"
    assert schema_errors("job.schema.json", wire_job(job)) == []


def test_catalogue_matches_contracts_and_schemas():
    job_ops = json.loads((SCHEMAS_DIR / "job.schema.json").read_text())["properties"]["operation"]["enum"]
    assert tuple(catalogue.OPERATIONS) == contracts.DEVICE_OPERATIONS == tuple(job_ops)
    target_ops = json.loads((SCHEMAS_DIR / "target-profile.schema.json").read_text())["properties"][
        "allowed_operations"
    ]["items"]["enum"]
    assert {op for op, spec in catalogue.OPERATIONS.items() if spec.requires_target} == set(target_ops)
    # ADR-005: confirmation derived from the operation type: writes and diagnostic transfer only
    assert {op for op, s in catalogue.OPERATIONS.items() if s.requires_local_confirmation} == {
        "ble_write_lab_value",
        "ble_collect_diagnostics",
    }
    for op, spec in catalogue.OPERATIONS.items():
        Draft202012Validator.check_schema(spec.arguments_schema)
        Draft202012Validator.check_schema(catalogue.tool_parameters(op))
        assert set(spec.default_limits) <= set(spec.limit_bounds)
        assert catalogue.resolve_limits(op, {}) == spec.default_limits
        assert 0 < spec.default_deadline_s <= 600
        if spec.requires_local_confirmation:
            assert spec.changes_target_state


def test_example_jobs_pass_backend_argument_checks():
    profile = catalogue.load_profile("oyster-lab-v1")
    for name in ("job.ble-read.json", "job.write-lab-value.json"):
        job = contracts.Job.model_validate_json((EXAMPLES_DIR / name).read_text(encoding="utf-8"))
        op = job.operation.value
        catalogue.validate_arguments(op, job.arguments, profile)
        catalogue.resolve_limits(op, job.limits.model_dump(exclude_none=True))
        assert job.requires_local_confirmation == catalogue.requires_local_confirmation(op)
