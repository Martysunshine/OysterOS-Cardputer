"""Reports: schema-valid JSON, visible simulated label, citations only from the session or linked baseline."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from oyster.contracts import Event, Finding
from oyster.evidence.validate import EXAMPLES_DIR, schema_errors
from oyster.main import create_app
from oyster.policy.evaluators import evaluate_assertion
from oyster.reports.render import CitationError, build_report, invalid_citations, render_markdown
from oyster.settings import Settings
from oyster.store import Conflict

DIAG = "restricted_diagnostics_requires_app_authorization"


def _session(store, device="sim-01", mode="simulated") -> str:
    rec, _ = store.register_session(device, None, mode, ["ble_check_access_policy"], "bench")
    return rec.session_id


def _ev(check_event, eid, sid, **kw) -> Event:
    return check_event(eid, DIAG, session_id=sid, **kw)


def _finding(profile, sid, events) -> Finding:
    return evaluate_assertion(profile, DIAG, events, session_id=sid, target_id="target-lab-01")


def test_report_json_is_schema_valid_and_simulated_label_is_visible(store, profile, check_event):
    sid = _session(store)
    events = [_ev(check_event, "evt-000021", sid, authorized=False)]
    store.add_events("sim-01", sid, events)
    store.add_finding(_finding(profile, sid, events))
    run = store.create_run(sid, "Assess diagnostics", {"name": "fake", "model": None, "is_fake": True})
    run.status, run.answer = (
        "succeeded",
        {
            "answer_markdown": "Diagnostics leaked (evt-000021).\n<script>x</script>",
            "cited_evidence_ids": ["evt-000021"],
            "limitations": [],
        },
    )
    store.save_run(run)
    rep = build_report(store, sid)
    assert schema_errors("report.schema.json", rep) == []
    assert rep["source_modes"] == ["simulated"] and rep["limitations"][0].startswith("SIMULATED DATA")
    assert [f["classification"] for f in rep["findings"]] == ["confirmed"]
    assert rep["runs"][0]["cited_evidence_ids"] == ["evt-000021"]
    md = render_markdown(rep)
    assert md.splitlines()[2].startswith("> **SIMULATED DATA")
    assert "confirmed **[SIMULATED]**" in md and "simulated **[SIMULATED]**" in md
    assert "<script>" not in md and "‹script›" in md  # inert text inside the plain-text block
    assert "`evt-000021`" in md


def test_report_rejects_evidence_ids_from_another_session(store, profile, check_event):
    a, b = _session(store), _session(store, device="sim-02")
    store.add_events("sim-01", a, [_ev(check_event, "evt-000001", a, access="denied_app")])
    foreign = [_ev(check_event, "evt-000099", b, authorized=False, device_id="sim-02")]
    store.add_events("sim-02", b, foreign)
    # the store refuses a finding for A that cites B's evidence
    stolen = _finding(profile, b, foreign).model_copy(update={"session_id": a})
    with pytest.raises(Conflict) as e:
        store.add_finding(stolen)
    assert e.value.code == "events_missing"
    assert invalid_citations(store, a, ["evt-000001", "evt-000099", "evt-404"]) == ["evt-000099", "evt-404"]
    # an AI answer citing another session's evidence loses those citations in the report
    run = store.create_run(a, "q", {"name": "fake", "model": None, "is_fake": True})
    run.status, run.answer = (
        "succeeded",
        {"answer_markdown": "see evt-000099", "cited_evidence_ids": ["evt-000001", "evt-000099"]},
    )
    store.save_run(run)
    rep = build_report(store, a)
    assert rep["runs"][0]["cited_evidence_ids"] == ["evt-000001"]
    assert any("evt-000099" in x and run.run_id in x for x in rep["limitations"])
    assert all(e["session_id"] == a for e in rep["evidence_index"])
    with pytest.raises(CitationError) as ce:
        build_report(store, a, strict=True)
    assert ce.value.invalid == ["evt-000099"]


class _InjectingStore:
    """Wraps a store and injects a finding that bypassed add_finding validation."""

    def __init__(self, inner, finding):
        self._inner, self._finding = inner, finding

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def list_findings(self, session_id):
        return self._inner.list_findings(session_id) + (
            [self._finding] if session_id == self._finding.session_id else []
        )


def test_findings_citing_foreign_evidence_are_omitted(store, profile, check_event):
    a, b = _session(store), _session(store, device="sim-02")
    store.add_events("sim-01", a, [_ev(check_event, "evt-000001", a, access="denied_app")])
    store.add_events("sim-02", b, [_ev(check_event, "evt-000077", b, authorized=False, device_id="sim-02")])
    bad = Finding.model_validate(
        {
            **json.loads((EXAMPLES_DIR / "finding.confirmed-simulated.json").read_text()),
            "session_id": a,
            "evidence_ids": ["evt-000077"],
        }
    )
    wrapped = _InjectingStore(store, bad)
    rep = build_report(wrapped, a)
    assert rep["findings"] == [] and any(bad.finding_id in x for x in rep["limitations"])
    with pytest.raises(CitationError):
        build_report(wrapped, a, strict=True)


def test_linked_baseline_evidence_may_be_cited(store, profile, check_event):
    vulnerable, corrected = _session(store), _session(store)
    v_events = [_ev(check_event, "evt-000010", vulnerable, authorized=False)]
    c_events = [_ev(check_event, "evt-000011", corrected, access="denied_app")]
    store.add_events("sim-01", vulnerable, v_events)
    store.add_events("sim-01", corrected, c_events)
    first = _finding(profile, vulnerable, v_events)
    store.add_finding(first)
    retest = evaluate_assertion(
        profile, DIAG, c_events, session_id=corrected, target_id="target-lab-01", retest_of=first.finding_id
    )
    store.add_finding(retest)
    # before linking, the corrected session may not cite the vulnerable session's evidence
    assert invalid_citations(store, corrected, ["evt-000010"]) == ["evt-000010"]
    store.link_baseline(corrected, vulnerable)
    assert invalid_citations(store, corrected, ["evt-000010", "evt-000011"]) == []
    rep = build_report(store, corrected)
    assert schema_errors("report.schema.json", rep) == []
    assert rep["linked_baseline_session_id"] == vulnerable
    assert {f["classification"] for f in rep["findings"]} == {"confirmed", "passed"}
    assert {e["session_id"] for e in rep["evidence_index"]} == {vulnerable, corrected}
    assert first.finding_id in render_markdown(rep)


def test_live_report_has_no_simulated_banner(store):
    store.create_device("cardputer-01", "c", False)
    sid = _session(store, device="cardputer-01", mode="live")
    rep = build_report(store, sid)
    assert rep["source_modes"] == ["live"] and not any("SIMULATED" in x for x in rep["limitations"])
    assert "SIMULATED" not in render_markdown(rep)


def test_report_endpoint_formats_and_ownership(store, tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        bind_host="127.0.0.1",
        https_port=8443,
        http_port=8000,
        admin_token="report-admin-token-0123456789",
        tls_cert=tmp_path / "c",
        tls_key=tmp_path / "k",
        ai_provider="groq",
        ai_base_url="",
        ai_model="",
        ai_api_key="",
        ai_timeout_s=5,
        ai_max_input_tokens=1,
        ai_max_context_events=40,
        ai_max_output_tokens=1,
        ai_price_input_per_mtok=None,
        ai_price_output_per_mtok=None,
    )
    c = TestClient(create_app(settings, store=store))
    tok_a = store.create_device("sim-01", "sim", True)
    tok_b = store.create_device("sim-02", "sim", True)
    sid = _session(store)
    a = {"Authorization": f"Bearer {tok_a}"}
    md = c.get(f"/v1/sessions/{sid}/report", headers=a)
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert md.text.startswith("# Oyster Pocket session report")
    js = c.get(f"/v1/sessions/{sid}/report", params={"format": "json"}, headers=a)
    assert schema_errors("report.schema.json", js.json()) == []
    assert c.get(f"/v1/sessions/{sid}/report", params={"format": "pdf"}, headers=a).status_code == 422
    assert (
        c.get(f"/v1/sessions/{sid}/report", headers={"Authorization": f"Bearer {tok_b}"}).status_code == 404
    )
    assert c.get(f"/v1/sessions/{sid}/report").status_code == 401
