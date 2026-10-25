"""Hardening of the AI layer: readable data, prose checks, compare scope, usage caps and secret masking."""

from __future__ import annotations

import argparse
import json
import ssl

import httpx
import pytest
import truststore
from fake_store import (
    DEVICE_ID,
    SESSION_ID,
    TARGET_ID,
    AutoDevice,
    FakeStore,
    fake_evaluator,
    make_event,
    make_settings,
    setup_lab,
)
from oyster.ai import cli as ai_cli
from oyster.ai import context as ctx
from oyster.ai import fake as F
from oyster.ai import orchestrator as orch_mod
from oyster.ai import tools as cat
from oyster.ai.openai_compat import OpenAICompatProvider
from oyster.ai.orchestrator import SIMULATED_LIMITATION
from oyster.ai.redact import IdentifierMasker, env_secrets
from oyster.contracts import Finding
from oyster.settings import RunBudgets
from test_ai_orchestrator import READ_PUBLIC, WRITE_GREEN, Env, cite_last, make_env

PROFILE = cat.load_lab_profile()
CHECK_LED = {"target_id": TARGET_ID, "test_id": "virtual_led_write_requires_app_authorization"}
CHECK_ENC = {"target_id": TARGET_ID, "test_id": "restricted_value_requires_link_encryption"}


def cite_job(messages, tools):
    """Final answer citing the evidence ids of the (last) job_result tool message."""
    job_msgs = [m for m in messages if m.role == "tool" and '"kind":"job_result"' in m.content]
    return F.final("Result explained.", F.evidence_ids_in(job_msgs, last_only=True))


def _finding(
    finding_id, session_id, test_id, *, target_id=TARGET_ID, classification="confirmed", ev="evt-x1"
):
    return Finding(
        finding_id=finding_id,
        session_id=session_id,
        target_id=target_id,
        profile_id="oyster-lab-v1",
        test_id=test_id,
        classification=classification,
        title="t",
        expected_policy="denied",
        observed_behavior="returned",
        evidence_ids=[ev],
        severity="medium",
        confidence="medium",
        source_mode="simulated",
    )


def _add_session(env: Env, session_id: str, *, source_mode="simulated", device_id=DEVICE_ID):
    setup_lab(env.store, session_id=session_id, device_id=device_id, source_mode=source_mode)


def _wifi_event(session_id, bssid, ssid="lab-net", channel=6):
    return make_event(
        "wifi_survey",
        {
            "observations": [
                {
                    "bssid": bssid,
                    "ssid_display": ssid,
                    "channel": channel,
                    "rssi_dbm": -50,
                    "auth_mode": "wpa2_psk",
                }
            ]
        },
        session_id=session_id,
    )


# ------------------------------------------------------------------ model-readable characteristics
def test_protected_and_challenge_characteristics_are_not_readable_by_the_model():
    assert cat.readable_characteristics(PROFILE) == [
        "device_info",
        "diag_status",
        "public_sensor",
        "virtual_led",
    ]
    for protected in ("diag_manifest", "chunk_data", "app_auth", "restricted_enc", "restricted_mitm"):
        reason = cat.validate_arguments(
            "ble_read_profile_value", {"target_id": TARGET_ID, "characteristic_id": protected}, PROFILE
        )
        assert reason and "allowed set" in reason, protected
    assert cat.validate_arguments("ble_read_profile_value", READ_PUBLIC, PROFILE) is None


def test_model_request_for_diag_manifest_creates_no_job(tmp_path):
    env = make_env(
        tmp_path,
        [
            F.tool_call(
                "ble_read_profile_value", {"target_id": TARGET_ID, "characteristic_id": "diag_manifest"}
            ),
            F.final("I cannot read that directly.", []),
        ],
    )
    run = env.execute()
    assert env.store.create_job_calls == []
    assert env.store.jobs == {}
    assert any(s["kind"] == "validation_error" for s in run.steps)
    assert run.status == "succeeded"


def test_job_creation_refuses_when_validation_is_bypassed(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "validate_arguments", lambda tool, arguments, profile: None)
    env = make_env(
        tmp_path,
        [
            F.tool_call("ble_read_profile_value", {"target_id": TARGET_ID, "characteristic_id": "app_auth"}),
            F.final("Refused.", []),
        ],
    )
    run = env.execute()
    assert env.store.create_job_calls == []
    refused = [s for s in run.steps if s["outcome"] == "refused"]
    assert refused and "not public-read" in refused[0]["summary"]


# ------------------------------------------------------------------ non-gated jobs and the confirmation pause
def test_non_gated_job_in_awaiting_state_is_not_a_confirmation_pause(tmp_path):
    def odd_device(store, rec):
        if rec.status == "queued":
            rec.status = "delivered"
            store.jobs[rec.job.job_id].status = "awaiting_local_confirmation"  # not gated: invalid state
            rec.status = "awaiting_local_confirmation"

    env = make_env(
        tmp_path,
        [F.tool_call("ble_read_profile_value", READ_PUBLIC), cite_last],
        device=odd_device,
        budgets=RunBudgets(max_duration_s=180, max_confirmation_wait_s=120),
    )
    run = env.execute()
    assert env.store.create_job_calls[0]["requires_local_confirmation"] is False
    assert run.paused_s == 0  # time on a non-gated job counts against the duration budget
    assert run.status != "awaiting_local_confirmation"


# ------------------------------------------------------------------ ids and markup in model prose
def test_prose_naming_unknown_ids_is_rejected_then_repaired(tmp_path):
    env = make_env(
        tmp_path,
        [
            F.tool_call("ble_read_profile_value", READ_PUBLIC),
            F.final("See evt-999999 and finding-deadbeef01 from job-000777 in run-000099.", []),
            cite_job,
        ],
    )
    run = env.execute()
    assert run.status == "succeeded", run.error
    rejected = [p for p in env.tool_payloads(2) if p.get("error") == "invalid_final_answer"]
    assert rejected and "evt-999999" in rejected[0]["detail"]
    assert "evt-999999" not in run.answer["answer_markdown"]


def test_unknown_id_in_limitations_or_suggestions_twice_fails_the_run(tmp_path):
    env = make_env(
        tmp_path,
        [
            F.final("ok", [], limitations=["based on evt-424242"]),
            F.final("ok", [], suggestions=["retest finding-0000feed"]),
        ],
    )
    run = env.execute()
    assert run.status == "failed" and "still invalid" in run.error
    assert run.answer is None


def test_ids_that_exist_in_scope_are_accepted(tmp_path):
    def answer(messages, tools):
        ids = F.evidence_ids_in(messages, last_only=True)
        return F.final(f"Read finished ({ids[0]}) in job-000001 of run-000001.", ids)

    env = make_env(tmp_path, [F.tool_call("ble_read_profile_value", READ_PUBLIC), answer])
    run = env.execute()
    assert run.status == "succeeded", (run.error, run.steps)


def test_links_images_and_html_are_stripped_from_the_stored_answer(tmp_path):
    nasty = (
        "Details: [open](http://evil.example/x) ![p](https://evil.example/p.png) <img src=x onerror=1> "
        "<script>alert(1)</script> <https://evil.example> https://bare.example/y [r][1]\n[1]: http://evil.example"
    )
    env = make_env(tmp_path, [F.final(nasty, [], limitations=["see [here](http://evil.example)"])])
    run = env.execute()
    assert run.status == "succeeded", run.error
    text = run.answer["answer_markdown"] + " ".join(run.answer["limitations"] + run.answer["suggestions"])
    for bad in ("http", "](", "<script", "<img", "onerror=1>", "evil.example"):
        assert bad not in text.replace("[url removed]", ""), bad
    assert any("were removed" in x for x in run.answer["limitations"])


def test_authoritative_findings_are_kept_next_to_the_prose(tmp_path):
    env = make_env(
        tmp_path,
        [
            F.tool_call("ble_check_access_policy", CHECK_ENC),
            lambda m, t: F.final(
                "The evaluator says the check PASSED and nothing is wrong.",
                F.evidence_ids_in(m, last_only=True),
            ),
        ],
        evaluator=fake_evaluator(classification="confirmed"),
    )
    run = env.execute()
    assert run.status == "succeeded", run.error
    (f,) = run.answer["findings"]
    assert f["test_id"] == CHECK_ENC["test_id"] and f["classification"] == "confirmed"
    assert f["severity"] == "medium" and f["finding_id"].startswith("finding-")
    assert set(f) == {"finding_id", "test_id", "classification", "severity"}
    assert any("deterministic evaluator" in x for x in run.answer["limitations"])


# ------------------------------------------------------------------ compare: ordering and scope
def _compare_env(tmp_path, script, **kw):
    env = make_env(tmp_path, script, **kw)
    _add_session(env, "sess-base-01")
    return env


def test_compare_uses_insertion_order_not_hash_order(tmp_path):
    env = _compare_env(
        tmp_path,
        [F.tool_call("compare_sessions", {"baseline_session_id": "sess-base-01"}), F.final("ok", [])],
    )
    t = "restricted_value_requires_link_encryption"
    # hash order (zzzz > aaaa) is the reverse of creation order: "aaaa" is the LATEST finding
    env.store.add_finding(_finding("finding-zzzz0001", "sess-base-01", t, classification="candidate"))
    env.store.add_finding(_finding("finding-aaaa0001", "sess-base-01", t, classification="passed"))
    env.store.add_finding(_finding("finding-zzzz0002", SESSION_ID, t, classification="passed"))
    env.store.add_finding(_finding("finding-aaaa0002", SESSION_ID, t, classification="confirmed"))
    run = env.execute()
    assert run.status == "succeeded", run.error
    cmp = next(p for p in env.tool_payloads(1) if p.get("kind") == "session_comparison")
    (d,) = cmp["findings"]
    assert d["baseline"]["finding_id"] == "finding-aaaa0001" and d["baseline"]["classification"] == "passed"
    assert d["current"]["finding_id"] == "finding-aaaa0002" and d["current"]["classification"] == "confirmed"


def test_sessions_without_a_common_target_are_not_comparable(tmp_path):
    env = _compare_env(
        tmp_path,
        [F.tool_call("compare_sessions", {"baseline_session_id": "sess-base-01"}), F.final("ok", [])],
    )
    other = "target-other-01"
    env.store.upsert_target(env.store.get_target(TARGET_ID).model_copy(update={"target_id": other}))
    env.store.add_finding(_finding("finding-aaaa0001", "sess-base-01", "t_one", target_id=other))
    env.store.add_finding(_finding("finding-bbbb0001", SESSION_ID, "t_one", target_id=TARGET_ID))
    run = env.execute()
    refused = [s for s in run.steps if s["outcome"] == "refused"]
    assert refused and "share no enrolled target" in refused[0]["summary"]


def test_compare_other_device_session_is_refused(tmp_path):
    env = make_env(
        tmp_path,
        [F.tool_call("compare_sessions", {"baseline_session_id": "sess-else-01"}), F.final("ok", [])],
    )
    setup_lab(env.store, session_id="sess-else-01", device_id="other-device-01")
    run = env.execute()
    assert any("not found for this device" in s["summary"] for s in run.steps if s["outcome"] == "refused")


def test_answer_may_cite_compared_evidence_only_if_the_compare_result_returned_it(tmp_path):
    base_visible = _wifi_event("sess-base-01", "aa:bb:cc:00:00:01", ssid="only-old")
    base_hidden = make_event("ble_discover", {"adverts": []}, session_id="sess-base-01")

    def cite_hidden(messages, tools):
        return F.final("Also see the old discovery.", [base_hidden.event_id])

    def cite_visible(messages, tools):
        return F.final("The old AP is gone.", [base_visible.event_id])

    for step, ok in ((cite_hidden, False), (cite_visible, True)):
        env = _compare_env(
            tmp_path, [F.tool_call("compare_sessions", {"baseline_session_id": "sess-base-01"}), step, step]
        )
        env.store.add_events(DEVICE_ID, "sess-base-01", [base_visible, base_hidden])
        env.store.add_events(
            DEVICE_ID, SESSION_ID, [_wifi_event(SESSION_ID, "aa:bb:cc:00:00:02", ssid="new")]
        )
        run = env.execute()
        if ok:
            assert run.status == "succeeded", run.error
            assert run.answer["cited_evidence_ids"] == [base_visible.event_id]
            # (a) the extra session that was read is recorded
            assert any("sess-base-01" in x for x in run.answer["limitations"])
        else:
            assert run.status == "failed" and "not found in this session" in run.error


def test_explain_cannot_reach_uncompared_baseline_events(tmp_path):
    env = _compare_env(
        tmp_path, [F.tool_call("explain_evidence", {"evidence_ids": ["evt-base0001"]}), F.final("ok", [])]
    )
    env.store.add_events(
        DEVICE_ID,
        "sess-base-01",
        [make_event("ble_discover", {"adverts": []}, session_id="sess-base-01", event_id="evt-base0001")],
    )
    run = env.execute()
    payload = next(p for p in env.tool_payloads(1) if p.get("kind") == "evidence_explanation")
    assert payload["not_found"] == ["evt-base0001"] and payload["evidence"] == []
    assert run.status == "succeeded"


# ------------------------------------------------------------------ simulated-session label
def test_simulated_session_without_citations_keeps_the_label(tmp_path):
    env = make_env(tmp_path, [F.final("Nothing was measured yet.", [])])
    run = env.execute()
    assert run.status == "succeeded", run.error
    assert run.answer["cited_evidence_ids"] == []
    assert run.answer["source_modes"] == ["simulated"]
    assert SIMULATED_LIMITATION in run.answer["limitations"]


def test_live_session_has_no_simulated_label(tmp_path):
    clock_env = make_env(tmp_path, [F.final("Nothing was measured yet.", [])])
    clock_env.store.sessions[SESSION_ID].source_mode = "live"
    run = clock_env.execute()
    assert run.answer["source_modes"] == ["live"] and SIMULATED_LIMITATION not in run.answer["limitations"]


def test_label_is_not_lost_when_model_limitations_fill_the_list(tmp_path):
    env = make_env(tmp_path, [F.final("x", [], limitations=[f"model limitation {i}" for i in range(16)])])
    run = env.execute()
    assert len(run.answer["limitations"]) == 16
    assert SIMULATED_LIMITATION in run.answer["limitations"]


# ------------------------------------------------------------------ overview truncation
def test_overview_reads_the_newest_events_and_says_when_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(ctx, "EVENT_READ_LIMIT", 20)
    events = [make_event("ble_discover", {"adverts": [], "n": i}) for i in range(35)]
    env = make_env(tmp_path, [F.final("ok", [])], events=events)
    run = env.execute()
    overview = next(p for p in env.tool_payloads(0) if p.get("kind") == "session_overview")
    shown = {e["event_id"] for e in overview["evidence"]}
    assert events[-1].event_id in shown and events[0].event_id not in shown
    assert overview["events_truncated"]["limit"] == 20
    assert any("newest 20 events" in x for x in run.answer["limitations"])


def test_no_truncation_note_for_small_sessions(tmp_path):
    env = make_env(tmp_path, [F.final("ok", [])], events=[make_event("ble_discover", {"adverts": []})])
    env.execute()
    overview = next(p for p in env.tool_payloads(0) if p.get("kind") == "session_overview")
    assert "events_truncated" not in overview


def test_fake_store_newest_semantics():
    store = FakeStore()
    setup_lab(store)
    evs = [make_event("ble_discover", {"adverts": []}) for _ in range(6)]
    store.add_events(DEVICE_ID, SESSION_ID, evs)
    got = store.get_events(SESSION_ID, limit=3, newest=True)
    assert [e.event_id for e in got] == [e.event_id for e in evs[-3:]]
    assert [e.event_id for e in store.get_events(SESSION_ID, limit=3)] == [e.event_id for e in evs[:3]]


# ------------------------------------------------------------------ provider-reported token usage
def test_provider_reported_input_tokens_over_the_cap_end_the_run(tmp_path):
    call = F.tool_call("ble_read_profile_value", READ_PUBLIC)
    call["_usage"] = {"prompt_tokens": 12001, "completion_tokens": 10}
    env = make_env(tmp_path, [call, cite_last], ai_max_input_tokens=12000)
    run = env.execute()
    assert run.status == "budget_exhausted" and "12001" in run.error
    assert env.store.create_job_calls == []  # nothing from that response was executed
    assert run.input_tokens == 12001


def test_usage_under_the_cap_continues(tmp_path):
    call = F.tool_call("ble_read_profile_value", READ_PUBLIC)
    call["_usage"] = {"prompt_tokens": 900, "completion_tokens": 10}
    env = make_env(tmp_path, [call, cite_last], ai_max_input_tokens=12000)
    assert env.execute().status == "succeeded"


# ------------------------------------------------------------------ retest_of validation
def _retest_env(tmp_path, retest_of, calls):
    env = make_env(
        tmp_path,
        [
            F.tool_call("ble_check_access_policy", dict(CHECK_ENC, retest_of=retest_of)),
            lambda m, t: F.final("done", F.evidence_ids_in(m, last_only=True)),
        ],
        evaluator=fake_evaluator(calls),
    )
    env.store.add_finding(
        _finding("finding-aaaa1111", SESSION_ID, "restricted_value_requires_link_encryption")
    )
    env.store.add_finding(
        _finding("finding-bbbb2222", SESSION_ID, "restricted_value_requires_authenticated_pairing")
    )
    env.store.add_finding(
        _finding(
            "finding-cccc3333",
            SESSION_ID,
            "restricted_value_requires_link_encryption",
            target_id="target-zzz-99",
        )
    )
    return env


def test_retest_of_with_same_test_and_target_is_kept(tmp_path):
    calls: list = []
    run = _retest_env(tmp_path, "finding-aaaa1111", calls).execute()
    assert run.status == "succeeded", run.error
    assert calls[0]["retest_of"] == "finding-aaaa1111"
    assert not any("retest_of was ignored" in x for x in run.answer["limitations"])


@pytest.mark.parametrize("rid", ["finding-bbbb2222", "finding-cccc3333", "finding-nope0000"])
def test_retest_of_for_another_test_target_or_unknown_is_dropped_with_limitation(tmp_path, rid):
    calls: list = []
    run = _retest_env(tmp_path, rid, calls).execute()
    assert run.status == "succeeded", run.error
    assert calls[0]["retest_of"] is None
    assert any("retest_of was ignored" in x for x in run.answer["limitations"])


# ------------------------------------------------------------------ credential and identifier masking
def test_env_credentials_are_collected_and_scrubbed(tmp_path, monkeypatch):
    token = "f" * 64
    monkeypatch.setenv("LAB_FIXTURE_AUTH_TOKEN", token)
    monkeypatch.setenv("SOME_SERVICE_KEY", "service-key-value-123")
    monkeypatch.setenv("AI_MAX_INPUT_TOKENS", "12000")
    secrets = env_secrets()
    assert token in secrets and "service-key-value-123" in secrets and "12000" not in secrets
    ev = _wifi_event(SESSION_ID, "aa:bb:cc:00:00:01", ssid="net-service-key-value-123-x")
    ev2 = make_event("ble_discover", {"adverts": [], "note": f"fixture token {token}"})
    env = make_env(tmp_path, [F.final("ok", [])], events=[ev, ev2])
    env.execute()
    blob = json.dumps([m.content for m in env.messages(0)])
    assert token not in blob and "service-key-value-123" not in blob


def test_nested_value_hex_is_withheld_at_any_depth():
    ev = make_event(
        "ble_read_profile_value",
        {
            "characteristic_id": "public_sensor",
            "value_hex": "e803",
            "deep": {"items": [{"characteristic_id": "restricted_enc", "value_hex": "4c41422d454e43"}]},
            "notifications": [{"value_hex": "0102"}],
        },
        target_id=TARGET_ID,
    )
    out = ctx.summarize_event(ev, PROFILE)["details"]
    assert out["value_hex"] == "e803"  # public, top level
    assert out["deep"]["items"][0]["value_hex"]["withheld"] == "non_public_value"
    assert out["notifications"][0]["value_hex"] == "0102"  # inherits the enclosing public characteristic
    assert "4c41422d454e43" not in json.dumps(out)
    bare = make_event(
        "ble_observe_notifications", {"notifications": [{"value_hex": "0102"}]}, target_id=TARGET_ID
    )
    assert ctx.summarize_event(bare, PROFILE)["details"]["notifications"][0]["value_hex"]["withheld"]


def test_app_auth_value_is_withheld_even_nested():
    ev = make_event(
        "ble_read_profile_value",
        {"items": [{"characteristic_id": "app_auth", "value_hex": "00" + "ab" * 16}]},
        target_id=TARGET_ID,
    )
    out = ctx.summarize_event(ev, PROFILE)["details"]
    assert out["items"][0]["value_hex"]["withheld"] == "non_public_value"


def test_neighbour_identifiers_are_masked_with_stable_aliases(tmp_path):
    ev = _wifi_event(SESSION_ID, "AA:BB:CC:11:22:33", ssid="neighbour")
    adv = make_event("ble_discover", {"adverts": [{"address": "de:ad:be:ef:12:34", "rssi_dbm": -60}]})
    env = make_env(tmp_path, [F.final("ok", [])], events=[ev, adv])
    env.execute()
    blob = " ".join(m.content for m in env.messages(0))
    assert "AA:BB:CC:xx:xx:xx#ap-" in blob and "de:ad:be:xx:xx:xx#dev-" in blob
    for full in ("11:22:33", "ef:12:34"):
        assert full not in blob
    alias = IdentifierMasker(SESSION_ID).alias("aa:bb:cc:11:22:33", "ap")
    assert alias in blob  # derived from session + address only, so it is the same in every run
    assert IdentifierMasker("sess-other-01").alias("aa:bb:cc:11:22:33", "ap") != alias


def test_full_identifiers_only_with_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_SEND_FULL_IDENTIFIERS", "true")
    env = make_env(
        tmp_path, [F.final("ok", [])], events=[_wifi_event(SESSION_ID, "aa:bb:cc:11:22:33", ssid="neighbour")]
    )
    env.execute()
    assert "aa:bb:cc:11:22:33" in " ".join(m.content for m in env.messages(0))


def test_ask_refuses_to_send_the_admin_token_off_host(tmp_path, monkeypatch):
    sent: list = []
    monkeypatch.setattr(ai_cli, "run_ask", lambda base, token, *a, **kw: sent.append((base, token)) or 0)
    settings = make_settings(tmp_path)

    def ask(backend, allow=False):
        ns = argparse.Namespace(
            session="sess-a", question="q", backend=backend, timeout=1.0, poll=0.1, allow_remote_admin=allow
        )
        return ai_cli.cmd_ask(ns, settings)

    assert ask("http://192.168.1.50:8000") == 2 and sent == []
    assert ask("http://evil.example.com") == 2 and ask("https://evil.example.com") == 2 and sent == []
    assert ask("ftp://127.0.0.1") == 2 and sent == []
    assert (
        ask("http://127.0.0.1:8000") == 0
        and ask("http://localhost:8000") == 0
        and ask("http://[::1]:8000") == 0
    )
    assert len(sent) == 3
    assert ask("https://owner-box.example.com", allow=True) == 0
    assert sent[-1][0] == "https://owner-box.example.com"


def test_ask_parser_has_allow_remote_admin_flag():
    p = argparse.ArgumentParser()
    ai_cli.register(p.add_subparsers())
    ns = p.parse_args(["ask", "--session", "sess-a", "--allow-remote-admin", "q"])
    assert ns.allow_remote_admin is True
    assert p.parse_args(["ask", "--session", "sess-a", "q"]).allow_remote_admin is False


# ------------------------------------------------------------------ mutation guards
def _real_provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url="https://api.groq.com/openai/v1", api_key="gsk" "_unit_test_key_0123456789ab", model="m"
    )


def test_groq_client_verifies_certificates_and_hostnames():
    p = _real_provider()
    try:
        pool = p._client._transport._pool
        context = pool._ssl_context
        assert isinstance(context, truststore.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
    finally:
        p.close()


def test_groq_client_never_follows_redirects():
    p = _real_provider()
    try:
        assert p._client.follow_redirects is False
    finally:
        p.close()
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    q = OpenAICompatProvider(
        base_url="https://api.groq.com/openai/v1",
        api_key="gsk" "_unit_test_key_0123456789ab",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(Exception) as ei:
        q.propose_action([], [])
    assert "302" in str(ei.value) and len(seen) == 1 and "evil.example" not in seen[0][0]


def test_confirmation_wait_is_clamped_to_the_devices_120_s(tmp_path):
    env = make_env(
        tmp_path,
        [F.tool_call("ble_write_lab_value", WRITE_GREEN)],
        device=AutoDevice(mode="never"),
        budgets=RunBudgets(max_duration_s=600, max_confirmation_wait_s=600),
    )
    run = env.execute()
    assert orch_mod.MAX_CONFIRMATION_WAIT_S == 120
    assert run.status == "expired"
    assert 120 < run.paused_s <= 122  # not 600


def test_overview_lists_findings_in_insertion_order_and_keeps_the_newest(tmp_path):
    env = make_env(tmp_path, [F.final("ok", [])])
    n = ctx.MAX_FINDINGS + 3
    # ids whose hash order is the reverse of creation order
    for i in range(n):
        env.store.add_finding(_finding(f"finding-{n - i:04d}", SESSION_ID, "t_one"))
    env.execute()
    overview = next(p for p in env.tool_payloads(0) if p.get("kind") == "session_overview")
    ids = [f["finding_id"] for f in overview["findings"]]
    assert ids == [f"finding-{n - i:04d}" for i in range(3, n)]  # the newest MAX_FINDINGS, oldest first
    assert overview["findings_omitted_older"] == 3


def test_configured_secrets_never_reach_the_stored_answer(tmp_path, monkeypatch):
    token = "9" * 64
    monkeypatch.setenv("LAB_FIXTURE_AUTH_TOKEN", token)
    env = make_env(
        tmp_path,
        [
            F.final(
                f"Token {token} and the admin token admin-token-for-tests-0123456789", [], suggestions=[token]
            )
        ],
    )
    run = env.execute()
    assert run.status == "succeeded", run.error
    blob = json.dumps(run.answer)
    assert token not in blob and "admin-token-for-tests" not in blob


def test_ask_prints_the_evaluator_findings(capsys):
    lines: list[str] = []
    run = {
        "provider": {"name": "fake", "model": None, "is_fake": True},
        "answer": {
            "answer_markdown": "ok",
            "cited_evidence_ids": [],
            "limitations": [],
            "suggestions": [],
            "source_modes": ["simulated"],
            "findings": [
                {
                    "finding_id": "finding-aa11",
                    "test_id": "t_one",
                    "classification": "confirmed",
                    "severity": "medium",
                }
            ],
        },
        "usage": {},
        "budget": {},
    }
    ai_cli._print_result(run, lines.append)
    assert "finding: t_one -> confirmed (medium) [finding-aa11]" in lines
