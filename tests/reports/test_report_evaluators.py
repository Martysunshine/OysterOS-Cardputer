"""Deterministic evaluator fixtures: public-readable, protected-correct, vulnerable,
unknown state and unestablished preconditions, with no false confirmed findings."""

from __future__ import annotations

import pytest
from oyster.evidence.validate import schema_errors
from oyster.policy.evaluators import evaluate_assertion

ENC = "restricted_value_requires_link_encryption"
MITM = "restricted_value_requires_authenticated_pairing"
DIAG_DENY = "restricted_diagnostics_requires_app_authorization"
DIAG_ALLOW = "diagnostics_available_after_app_authorization"
LED = "virtual_led_write_requires_app_authorization"
PUBLIC = "public_sensor_is_public"


def ev_(profile, test_id, events, **kw):
    f = evaluate_assertion(profile, test_id, events, session_id="sess-r-01", target_id="target-lab-01", **kw)
    assert schema_errors("finding.schema.json", f.model_dump(mode="json")) == []
    return f


def test_public_readable_is_only_ever_an_observation(profile, read_event, check_event):
    f = ev_(profile, PUBLIC, [read_event("evt-000001")])
    assert (f.classification.value, f.severity, f.confidence) == ("observation", "informational", "high")
    assert f.evidence_ids == ["evt-000001"] and "2 bytes" in f.observed_behavior
    # even a "returned while unencrypted" check result stays informational
    f = ev_(profile, PUBLIC, [check_event("evt-000002", PUBLIC, encrypted=False)])
    assert f.classification.value == "observation"
    # reads of other characteristics are not evidence for this assertion
    with pytest.raises(ValueError):
        ev_(profile, PUBLIC, [read_event("evt-000003", characteristic="device_info")])


@pytest.mark.parametrize(
    "test_id, denial",
    [(ENC, "denied_att"), (MITM, "denied_att"), (DIAG_DENY, "denied_app"), (LED, "denied_app")],
)
def test_protected_correct_is_passed(profile, check_event, test_id, denial):
    f = ev_(profile, test_id, [check_event("evt-000010", test_id, access=denial)])
    assert (f.classification.value, f.severity, f.confidence) == ("passed", "informational", "medium")
    f = ev_(
        profile,
        test_id,
        [
            check_event("evt-000010", test_id, access=denial),
            check_event("evt-000011", test_id, access=denial, seq=2),
        ],
    )
    assert f.confidence == "high"


@pytest.mark.parametrize(
    "test_id, state",
    [
        (ENC, {"encrypted": False}),
        (MITM, {"encrypted": True, "authenticated": False}),
        (DIAG_DENY, {"authorized": False}),
        (LED, {"authorized": False}),
    ],
)
def test_protected_vulnerable_is_confirmed(profile, check_event, test_id, state):
    one = [check_event("evt-000020", test_id, **state)]
    f = ev_(profile, test_id, one)
    sev = next(a["severity_if_violated"] for a in profile["assertions"] if a["test_id"] == test_id)
    assert (f.classification.value, f.severity, f.confidence) == ("confirmed", sev, "medium")
    two = one + [check_event("evt-000021", test_id, seq=2, **state)]
    assert ev_(profile, test_id, two).confidence == "high"


def test_unknown_security_state_is_candidate(profile, check_event):
    f = ev_(profile, MITM, [check_event("evt-000030", MITM, encrypted=True, authenticated=None)])
    assert (f.classification.value, f.severity, f.confidence) == ("candidate", "medium", "low")
    assert any("authenticated_pairing" in x for x in f.limitations)


@pytest.mark.parametrize("established", [False, None])
def test_precondition_not_established_is_inconclusive(profile, check_event, established):
    f = ev_(profile, ENC, [check_event("evt-000040", ENC, established=established, encrypted=False)])
    assert (f.classification.value, f.severity) == ("inconclusive", "not_assessed")


@pytest.mark.parametrize("access", ["timeout", "error", "not_attempted"])
def test_timeouts_and_errors_are_never_bypasses(profile, check_event, access):
    f = ev_(profile, DIAG_DENY, [check_event("evt-000050", DIAG_DENY, access=access)])
    assert (f.classification.value, f.severity) == ("inconclusive", "not_assessed")


def test_escalated_link_security_passes_with_the_transition_recorded(profile, check_event):
    f = ev_(profile, ENC, [check_event("evt-000060", ENC, encrypted=True, transition=True)])
    assert f.classification.value == "passed"
    assert any("Link security changed" in x for x in f.limitations)


def test_expected_allowed_transfer(profile, check_event):
    ok = ev_(
        profile, DIAG_ALLOW, [check_event("evt-000070", DIAG_ALLOW, authorized=True, hash_verified=True)]
    )
    assert ok.classification.value == "passed"
    denied = ev_(
        profile, DIAG_ALLOW, [check_event("evt-000071", DIAG_ALLOW, access="denied_app", authorized=True)]
    )
    assert (denied.classification.value, denied.severity) == ("candidate", "low")
    bad_hash = ev_(
        profile, DIAG_ALLOW, [check_event("evt-000072", DIAG_ALLOW, authorized=True, hash_verified=False)]
    )
    assert bad_hash.classification.value == "candidate"
    unverified = ev_(profile, DIAG_ALLOW, [check_event("evt-000073", DIAG_ALLOW, authorized=True)])
    assert unverified.classification.value == "inconclusive"


def test_contradicting_observations_are_not_confirmed(profile, check_event):
    f = ev_(
        profile,
        DIAG_DENY,
        [
            check_event("evt-000080", DIAG_DENY, authorized=False),
            check_event("evt-000081", DIAG_DENY, access="denied_app", seq=2),
        ],
    )
    assert f.classification.value == "candidate" and any("Inconsistent" in x for x in f.limitations)


def test_unsupported_route_is_unsupported(profile, check_event):
    f = ev_(profile, MITM, [check_event("evt-000090", MITM, access="not_attempted", status="unsupported")])
    assert (f.classification.value, f.severity) == ("unsupported", "not_assessed")


def test_source_mode_and_scoping(profile, check_event):
    sim = ev_(profile, ENC, [check_event("evt-000100", ENC, access="denied_att")])
    assert sim.source_mode.value == "simulated" and any("Simulated" in x for x in sim.limitations)
    live = ev_(profile, ENC, [check_event("evt-000101", ENC, access="denied_att", source_mode="live")])
    assert live.source_mode.value == "live" and not any("Simulated" in x for x in live.limitations)
    # events of another session, another target or another assertion are ignored
    noise = [
        check_event("evt-000102", ENC, session_id="sess-other", encrypted=False),
        check_event("evt-000103", ENC, target_id="target-other", encrypted=False),
        check_event("evt-000104", MITM, encrypted=True, authenticated=False),
    ]
    f = ev_(profile, ENC, noise + [check_event("evt-000105", ENC, access="denied_att")])
    assert f.classification.value == "passed" and f.evidence_ids == ["evt-000105"]
    with pytest.raises(ValueError):
        ev_(profile, ENC, noise[:2])
    with pytest.raises(ValueError):
        ev_(profile, "made_up_test", [check_event("evt-000106", ENC)])


def test_finding_ids_are_deterministic_and_retests_link(profile, check_event):
    events = [check_event("evt-000110", DIAG_DENY, authorized=False)]
    first = ev_(profile, DIAG_DENY, events)
    assert ev_(profile, DIAG_DENY, events).finding_id == first.finding_id
    retest = ev_(
        profile,
        DIAG_DENY,
        [check_event("evt-000111", DIAG_DENY, access="denied_app")],
        retest_of=first.finding_id,
    )
    assert retest.retest_of == first.finding_id and retest.classification.value == "passed"


def test_corrected_fixture_never_yields_confirmed(profile, check_event):
    """Every assertion under corrected-fixture behaviour, with every ambiguous state: no confirmed result."""
    seen = set()
    for a in profile["assertions"]:
        t = a["test_id"]
        variants = [
            {"access": "denied_app"},
            {"access": "denied_att"},
            {"access": "timeout"},
            {"access": "error"},
            {"access": "not_attempted"},
            {"established": False, "encrypted": False, "authorized": False},
            {"established": None},
            {"encrypted": None, "authenticated": None, "authorized": None},
            {"encrypted": True, "authenticated": True, "authorized": True, "hash_verified": True},
        ]
        for i, v in enumerate(variants):
            f = ev_(profile, t, [check_event(f"evt-{i:06d}", t, **v)])
            seen.add(f.classification.value)
            assert f.classification.value != "confirmed", (t, v)
    assert {"passed", "inconclusive", "candidate", "observation"} <= seen


# --- a denial counts only from the layer that enforces the documented policy ---
@pytest.mark.parametrize(
    "test_id, access, att",
    [
        (ENC, "denied_att", 0x05),
        (ENC, "denied_att", 0x0F),
        (ENC, "denied_att", 0x0C),
        (MITM, "denied_att", 0x05),
        (MITM, "denied_att", 0x0F),
        (DIAG_DENY, "denied_app", None),
        (DIAG_DENY, "denied_att", 0x08),
        (LED, "denied_app", None),
        (LED, "denied_att", 0x08),
    ],
)
def test_denial_from_the_enforcing_layer_passes(profile, check_event, test_id, access, att):
    f = ev_(profile, test_id, [check_event("evt-000200", test_id, access=access, raw_att=att)])
    assert f.classification.value == "passed"


@pytest.mark.parametrize(
    "test_id, access, att",
    [
        (ENC, "denied_app", None),  # application denial never proves link-layer enforcement
        (ENC, "denied_app", 0x0F),
        (ENC, "denied_att", None),  # ATT denial without a code
        (ENC, "denied_att", 0x0A),  # attribute not found: not an auth/encryption denial
        (ENC, "denied_att", 0x08),  # authorization is the application layer
        (MITM, "denied_app", None),
        (MITM, "denied_att", None),
        (MITM, "denied_att", 0x0C),  # key size is not an authentication denial
        (MITM, "denied_att", 0x0A),
        (DIAG_DENY, "denied_att", None),
        (DIAG_DENY, "denied_att", 0x0F),  # link-layer code for an application policy
        (DIAG_DENY, "denied_att", 0x0A),
        (LED, "denied_att", 0x05),
    ],
)
def test_denial_from_the_wrong_layer_is_inconclusive(profile, check_event, test_id, access, att):
    f = ev_(profile, test_id, [check_event("evt-000210", test_id, access=access, raw_att=att)])
    assert (f.classification.value, f.severity) == ("inconclusive", "not_assessed")
    assert f.limitations and any("denial" in x or "denied" in x for x in f.limitations)


def test_wrong_layer_denial_cannot_offset_a_violation_into_a_pass(profile, check_event):
    f = ev_(
        profile,
        ENC,
        [
            check_event("evt-000220", ENC, access="denied_app"),  # inconclusive, not a pass
            check_event("evt-000221", ENC, encrypted=False, seq=2),  # violation
        ],
    )
    assert f.classification.value == "confirmed"


# --- envelope status and identity fields gate every verdict ---
@pytest.mark.parametrize(
    "status", ["error", "cancelled", "timeout", "unknown_outcome", "partial", "inconclusive"]
)
def test_non_driving_envelope_status_is_inconclusive(profile, check_event, status):
    # details alone would read as a violation (value returned, encrypted=false)
    f = ev_(profile, ENC, [check_event("evt-000230", ENC, encrypted=False, status=status)])
    assert (f.classification.value, f.severity) == ("inconclusive", "not_assessed")
    assert any(status in x for x in f.limitations)


def test_unsupported_envelope_status_is_unsupported(profile, check_event):
    f = ev_(profile, ENC, [check_event("evt-000231", ENC, encrypted=False, status="unsupported")])
    assert f.classification.value == "unsupported"


@pytest.mark.parametrize(
    "override",
    [{"characteristic": "device_info"}, {"characteristic": None}, {"precondition": "encrypted"}],
)
def test_identity_mismatch_cannot_drive_a_finding(profile, check_event, override):
    f = ev_(profile, ENC, [check_event("evt-000240", ENC, encrypted=False, **override)])
    assert (f.classification.value, f.severity) == ("inconclusive", "not_assessed")
    assert f.evidence_ids == ["evt-000240"]


def test_identity_mismatch_cannot_pass_either(profile, check_event):
    f = ev_(profile, ENC, [check_event("evt-000241", ENC, access="denied_att", characteristic="device_info")])
    assert f.classification.value == "inconclusive"


@pytest.mark.parametrize(
    "access, status", [("returned", "denied"), ("denied_app", "success"), ("denied_att", "success")]
)
def test_access_must_agree_with_the_envelope_status(profile, check_event, access, status):
    f = ev_(profile, DIAG_DENY, [check_event("evt-000250", DIAG_DENY, access=access, status=status)])
    assert f.classification.value == "inconclusive"
