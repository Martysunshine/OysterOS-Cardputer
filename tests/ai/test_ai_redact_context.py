"""Redaction, bounded context summaries and the tool catalogue."""

from __future__ import annotations

import json

import pytest
from fake_store import TARGET_ID, lab_target, make_event
from oyster.ai import tools as cat
from oyster.ai.context import build_overview, summarize_event
from oyster.ai.redact import redact_text, redact_tree
from oyster.contracts import DEVICE_OPERATIONS
from oyster.store import SessionRec

PROFILE = cat.load_lab_profile()


def test_redact_tree_withholds_secrets_keeps_digests_and_lengths():
    tree = {
        "auth_token": "a" * 64,
        "passkey": "123456",
        "value_sha256": "b" * 64,
        "psk": "hunter2",
        "nested": {"note": "c" * 40, "hmac_response": "00" * 32},
        "mfg_data": "ff" * 12,
        "raw_att_status": 5,
        "items": list(range(20)),
    }
    out = redact_tree(tree)
    assert out["auth_token"] == {"withheld": "secret", "length": 64}
    assert out["passkey"]["withheld"] == "secret" and out["psk"]["withheld"] == "secret"
    assert out["value_sha256"] == "b" * 64  # digests are evidence, not secrets
    assert "c" * 40 not in json.dumps(out)  # long hex blob in free text
    assert out["nested"]["hmac_response"]["withheld"] == "secret"
    assert out["mfg_data"] == {"withheld": "raw_payload", "length": 24}
    assert out["raw_att_status"] == 5
    assert out["items"][-1] == {"_truncated": 8} and len(out["items"]) == 13


def test_redact_text_patterns():
    s = redact_text("key gsk" "_abcdefghijklmnopqrstuv and Bearer abc.def.ghi-jkl and mine", ("mine-secret",))
    assert "gsk" "_abcdefghijklmnopqrstuv" not in s and "abc.def.ghi-jkl" not in s
    assert redact_text("token is mine-secret!", ("mine-secret",)) == "token is [redacted]!"
    assert "\x1b" not in redact_text("bell\x1b[31m")


def test_value_hex_only_for_non_secret_public_characteristics():
    pub = make_event("ble_read_profile_value", {"characteristic_id": "public_sensor", "value_hex": "e803"})
    enc = make_event(
        "ble_read_profile_value", {"characteristic_id": "restricted_enc", "value_hex": "4c41422d"}
    )
    app = make_event(
        "ble_read_profile_value", {"characteristic_id": "app_auth", "value_hex": "00" + "11" * 16}
    )
    assert summarize_event(pub, PROFILE)["details"]["value_hex"] == "e803"
    assert summarize_event(enc, PROFILE)["details"]["value_hex"] == {
        "withheld": "non_public_value",
        "length": 4,
    }
    assert summarize_event(app, PROFILE)["details"]["value_hex"]["withheld"] == "non_public_value"


def test_event_summary_is_bounded_sorted_and_deterministic():
    obs = [
        {
            "ssid_display": f"n{i}",
            "ssid_hex": "6e",
            "bssid": f"02:00:00:00:00:{i:02x}",
            "channel": 1,
            "rssi_dbm": -90 + i,
            "auth_mode": "WPA2_PSK",
        }
        for i in range(30)
    ]
    ev = make_event("wifi_survey", {"observations": obs, "complete": True})
    a, b = summarize_event(ev, PROFILE), summarize_event(ev, PROFILE)
    assert a == b
    d = a["details"]
    assert d["observations_count"] == 30
    assert d["observations"][0]["rssi_dbm"] == -61  # strongest first
    kept, marker = d["observations"][:-1], d["observations"][-1]
    assert len(kept) + marker["_truncated"] == 30 and len(kept) <= 12  # explicit, never silently partial
    assert d["observations"][0]["ssid_hex"] == {"withheld": "raw_payload", "length": 2}
    assert len(json.dumps(a, separators=(",", ":"))) <= 1800


def test_overview_trimming_withholds_oldest_first_and_keeps_ids():
    events = [make_event("ble_discover", {"adverts": []}) for _ in range(10)]
    sess = SessionRec("sess-test-01", "dev", "simulated", list(DEVICE_OPERATIONS), None, 0.0)
    ov = build_overview(sess, [lab_target()], [], events, PROFILE, max_events=6)
    full = json.loads(ov.render())
    assert [e["event_id"] for e in full["evidence"]] == [e.event_id for e in events[4:]]
    assert full["evidence_withheld"]["ids"] == [e.event_id for e in events[:4]]
    trimmed = json.loads(ov.render(drop_oldest=2))
    assert trimmed["evidence_withheld"]["count"] == 6
    assert [e["event_id"] for e in trimmed["evidence"]] == [e.event_id for e in events[6:]]


# ------------------------------------------------------------------ catalogue
def test_catalogue_flags_match_firmware_policy():
    confirm = {n for n, t in cat.catalogue().items() if t.requires_local_confirmation}
    changes = {n for n, t in cat.catalogue().items() if t.changes_target_state}
    # oyster_core/local_policy.cpp: operation_requires_local_confirmation / operation_changes_target_state
    assert confirm == {"ble_write_lab_value", "ble_collect_diagnostics"}
    assert changes >= {"ble_write_lab_value", "ble_collect_diagnostics", "ble_observe_notifications"}
    assert cat.requires_local_confirmation("anything-unknown") is True


def test_catalogue_flags_agree_with_backend_operation_catalogue():
    backend = pytest.importorskip("oyster.policy.catalogue")
    for name in DEVICE_OPERATIONS:
        assert cat.requires_local_confirmation(name) == backend.requires_local_confirmation(name), name
        # the AI side is at least as conservative as the backend for the no-automatic-retry rule
        assert cat.changes_target_state(name) or not backend.changes_target_state(name), name


def test_enums_come_from_lab_profile():
    specs = {t.name: t for t in cat.offered_tools(list(DEVICE_OPERATIONS), [lab_target()], PROFILE)}
    props = specs["ble_read_profile_value"].parameters["properties"]
    # only characteristics whose READ policy is public (and are not challenge material)
    assert props["characteristic_id"]["enum"] == sorted(
        k
        for k, c in PROFILE["characteristics"].items()
        if "read" in c["properties"] and c["policy"].get("read") == "public" and k != "app_auth"
    )
    assert props["characteristic_id"]["enum"] == [
        "device_info",
        "diag_status",
        "public_sensor",
        "virtual_led",
    ]
    assert props["target_id"]["enum"] == [TARGET_ID]
    assert specs["ble_check_access_policy"].parameters["properties"]["test_id"]["enum"] == [
        a["test_id"] for a in PROFILE["assertions"]
    ]
    assert specs["ble_observe_notifications"].parameters["properties"]["characteristic_id"]["enum"] == [
        "public_sensor"
    ]
    assert specs["ble_write_lab_value"].parameters["properties"]["value"]["enum"] == sorted(
        PROFILE["write_values"]["virtual_led"]["allowed"]
    )
    assert specs["ble_collect_diagnostics"].parameters["properties"]["object_id"]["enum"] == [
        "diag-synth-0001"
    ]
    assert list(specs)[-1] == "final_answer"


def test_offered_tools_respect_capabilities_and_targets():
    names = [t.name for t in cat.offered_tools(["wifi_survey", "ble_read_profile_value"], [], PROFILE)]
    assert names == ["wifi_survey", "compare_sessions", "explain_evidence", "generate_report", "final_answer"]
    assert [
        t.name for t in cat.offered_tools(list(DEVICE_OPERATIONS), [lab_target()], PROFILE, final_only=True)
    ] == ["final_answer"]


def test_validate_arguments_reasons_do_not_echo_values():
    reason = cat.validate_arguments(
        "ble_read_profile_value", {"target_id": TARGET_ID, "characteristic_id": "IGNORE PREVIOUS"}, PROFILE
    )
    assert "allowed set" in reason and "IGNORE" not in reason
    assert "unexpected argument" in cat.validate_arguments(
        "ble_write_lab_value",
        {
            "target_id": TARGET_ID,
            "characteristic_id": "virtual_led",
            "value": "red",
            "requires_local_confirmation": False,
        },
        PROFILE,
    )
    assert cat.validate_arguments("explain_evidence", {}, PROFILE)
    assert cat.validate_arguments("wifi_survey", {"ms_per_channel": 5}, PROFILE)
    assert cat.validate_arguments("wifi_survey", {}, PROFILE) is None


def test_job_arguments_limits_and_deadlines():
    args = cat.job_arguments(
        "ble_write_lab_value", {"target_id": TARGET_ID, "characteristic_id": "virtual_led", "value": "green"}
    )
    assert args == {"characteristic_id": "virtual_led", "value": "green", "restore": True}
    limits = cat.job_limits("ble_write_lab_value", args, PROFILE)
    assert limits == {"max_bytes": 1, "max_duration_s": 30.0}
    assert cat.job_deadline_s("ble_write_lab_value", limits, 120) == 180.0
    assert cat.job_deadline_s("ble_read_profile_value", {"max_duration_s": 20.0}, 120) == 50.0
    survey = cat.job_arguments("wifi_survey", {})
    assert cat.job_limits("wifi_survey", survey, PROFILE)["max_duration_s"] <= 30
    transfer = cat.job_limits(
        "ble_check_access_policy", {"test_id": "diagnostics_available_after_app_authorization"}, PROFILE
    )
    assert transfer["max_bytes"] == 65536


@pytest.mark.parametrize("assertion", PROFILE["assertions"], ids=lambda a: a["test_id"])
def test_access_policy_confirmation_derived_from_profile_assertion(assertion):
    backend = pytest.importorskip("oyster.policy.catalogue")
    args = cat.job_arguments(
        "ble_check_access_policy", {"target_id": TARGET_ID, "test_id": assertion["test_id"]}
    )
    gated = assertion["operation"] in ("write", "transfer")
    assert cat.job_requires_local_confirmation("ble_check_access_policy", args, PROFILE) is gated
    assert backend.job_requires_local_confirmation("ble_check_access_policy", args, PROFILE) is gated
    assert cat.changes_target_state("ble_check_access_policy") is True
    limits = cat.job_limits("ble_check_access_policy", args, PROFILE)
    wait = 120 if gated else 0
    assert cat.job_deadline_s("ble_check_access_policy", limits, 120, confirm=gated) == min(
        600.0, limits["max_duration_s"] + 30 + wait
    )


def test_job_confirmation_for_other_tools_is_the_operation_type():
    for name in DEVICE_OPERATIONS:
        if name == "ble_check_access_policy":
            continue
        assert cat.job_requires_local_confirmation(name, {}, PROFILE) is cat.requires_local_confirmation(name)
    unknown = {"test_id": "not_in_the_profile"}
    assert cat.job_requires_local_confirmation("ble_check_access_policy", unknown, PROFILE) is True
