"""Session reports: JSON per schemas/report.schema.json and a Markdown rendering of the same data.

A report cites only evidence that exists in the session itself or in its explicitly linked baseline
(events, or artifacts named by accepted job results). Findings or AI answers citing anything else are
excluded (or, with strict=True, rejected with CitationError) and the exclusion is listed as a limitation.
The simulated label stays visible: a banner, a tag on every simulated finding, and the source column.

Findings, evidence and limitations are generated purely from stored records. Text written by the model (run
answers, answer limitations) and other free text (questions, errors) is untrusted: the Markdown form shows it
as a plain-text block labelled "Model explanation (not verified)" with links, images and raw HTML removed
and URLs defanged, so opening the report never fetches or follows anything.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from oyster.contracts import Finding
from oyster.store import Store

REPORT_VERSION = "1.0"
MAX_INDEX_EVENTS = 2000
INDEX_TRUNCATED = "Evidence index truncated"
_MODE_ORDER = ("live", "offline", "simulated")
SIMULATED_BANNER = (
    "SIMULATED DATA: some or all evidence in this report comes from the software simulator. "
    "It validates orchestration, policy evaluation and reporting only; it does not validate "
    "radio drivers, pairing interoperability or actual BLE permissions."
)
_STANDING_LIMITATIONS = (
    "Classifications come from deterministic evaluators against the documented lab profile; AI text explains "
    "results but never sets a classification.",
    "Severity is contextual to the documented profile; a readable attribute alone does not establish impact.",
)


class CitationError(ValueError):
    """A finding or answer cites evidence outside the session and its linked baseline."""

    def __init__(self, owner: str, invalid: list[str]):
        super().__init__(f"{owner} cites evidence outside the report scope: {', '.join(invalid[:10])}")
        self.owner = owner
        self.invalid = invalid


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scopes(store: Store, session_id: str) -> list[str]:
    sess = store.get_session(session_id)
    return [session_id] + ([sess.linked_baseline_session_id] if sess.linked_baseline_session_id else [])


def invalid_citations(store: Store, session_id: str, ids: list[str]) -> list[str]:
    """Ids that are not evidence of the session or its linked baseline (order preserved, de-duplicated)."""
    return _invalid(store, _scopes(store, session_id), ids)


def _invalid(store: Store, scopes: list[str], ids: list[str]) -> list[str]:
    remaining = list(dict.fromkeys(ids))
    artifacts = getattr(store, "artifact_ids", None)
    for sid in scopes:
        if not remaining:
            break
        arts = set(artifacts(sid)) if artifacts else set()
        remaining = [i for i in store.missing_event_ids(sid, remaining) if i not in arts]
    return remaining


def build_report(store: Store, session_id: str, *, strict: bool = False, now: float | None = None) -> dict:
    sess = store.get_session(session_id)
    scopes = _scopes(store, session_id)
    limitations: list[str] = []
    modes = {sess.source_mode}

    index: list[dict] = []
    for sid in scopes:
        # the newest events: a long session must not lose its latest evidence to the cap
        events = store.get_events(sid, limit=MAX_INDEX_EVENTS + 1, newest=True)
        if len(events) > MAX_INDEX_EVENTS:
            events = events[-MAX_INDEX_EVENTS:]
            count = getattr(store, "count_events", None)
            total = count(sid) if count else None
            omitted = f"{total - MAX_INDEX_EVENTS} oldest of {total}" if total else "the oldest"
            limitations.append(
                f"{INDEX_TRUNCATED} for {sid}: showing the newest {len(events)} events; {omitted} "
                "events are not listed (they remain in the session database)."
            )
        for ev in events:
            modes.add(ev.source_mode.value)
            index.append(
                {
                    "event_id": ev.event_id,
                    "operation": ev.operation,
                    "status": ev.status.value,
                    "source_mode": ev.source_mode.value,
                    "session_id": ev.session_id,
                    "target_id": ev.target_id,
                }
            )
        if sid != session_id:
            modes.add(store.get_session(sid).source_mode)

    findings: list[dict] = []
    for sid in scopes:
        for f in store.list_findings(sid):
            bad = [*f.evidence_ids] if f.session_id not in scopes else _invalid(store, scopes, f.evidence_ids)
            if bad:
                if strict:
                    raise CitationError(f.finding_id, bad)
                limitations.append(
                    f"Finding {f.finding_id} omitted: it cites evidence outside this session "
                    f"and its linked baseline ({', '.join(bad[:5])})."
                )
                continue
            modes.add(f.source_mode.value)
            findings.append(f.model_dump(mode="json"))

    runs: list[dict] = []
    list_runs = getattr(store, "list_runs", None)
    for run in list_runs(session_id) if list_runs else []:
        answer = dict(run.answer or {})
        cited = list(answer.get("cited_evidence_ids") or [])
        bad = _invalid(store, scopes, cited) if cited else []
        if bad:
            if strict:
                raise CitationError(run.run_id, bad)
            limitations.append(
                f"Run {run.run_id}: removed citations outside this session ({', '.join(bad[:5])})."
            )
            cited = [c for c in cited if c not in bad]
        runs.append(
            {
                "run_id": run.run_id,
                "status": run.status,
                "question": run.question,
                "provider": run.provider,
                "created_at": _iso(run.created_at),
                "answer_markdown": answer.get("answer_markdown"),
                "cited_evidence_ids": cited,
                "limitations": list(answer.get("limitations") or []),
                "error": run.error,
            }
        )

    if "simulated" in modes:
        limitations.insert(0, SIMULATED_BANNER)
    limitations.extend(_STANDING_LIMITATIONS)
    return {
        "report_version": REPORT_VERSION,
        "session_id": session_id,
        "generated_at": _iso(now if now is not None else datetime.now(UTC).timestamp()),
        "source_modes": [m for m in _MODE_ORDER if m in modes],
        "linked_baseline_session_id": sess.linked_baseline_session_id,
        "findings": findings,
        "evidence_index": index,
        "runs": runs,
        "limitations": limitations,
    }


# --- Markdown ---
def _cell(text: object) -> str:
    s = "" if text is None else str(text)
    s = s.replace("\r", " ").replace("\n", " ").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace("`", "'")


_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_REFDEF_RE = re.compile(r"^[ \t]{0,3}\[[^\]]*\]:[ \t]*\S+.*$", re.MULTILINE)
_SCHEME_RE = re.compile(
    r"(?i)\b(https?|ftps?|file|javascript|vbscript|wss?|data(?=:\s*[a-z]+/))(:(?://)?)"
)
_DEFANGED_HOST_RE = re.compile(r"(?i)\b(hxxps?|fxps?)(://)([^\s/?#)\]>\"']+)")
_WWW_RE = re.compile(r"(?i)\bwww\.")
_DEFANG_SCHEME = {"http": "hxxp", "https": "hxxps", "ftp": "fxp", "ftps": "fxps"}


def _defang_scheme(m: re.Match) -> str:
    name, sep = m.group(1), m.group(2)
    swapped = _DEFANG_SCHEME.get(name.lower())
    return f"{swapped}{sep}" if swapped else f"{name}[:]{sep[1:]}"


def defang(text: str) -> str:
    """Untrusted text made inert: image/link syntax reduced to words, URL schemes and hosts defanged
    (https://a.b -> hxxps://a[.]b, javascript:x -> javascript[:]x), angle brackets replaced."""
    t = _IMAGE_RE.sub(lambda m: f"(image omitted: {m.group(1).replace('[', '')}; {m.group(2)})", text)
    t = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", t)
    t = _REFDEF_RE.sub("(reference link removed)", t)
    t = _SCHEME_RE.sub(_defang_scheme, t)
    t = _DEFANGED_HOST_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3).replace('.', '[.]')}", t)
    t = _WWW_RE.sub("www[.]", t)
    return t.replace("<", "\u2039").replace(">", "\u203a")


def _plain_block(text: str) -> str:
    """Model-written prose as a fenced plain-text block (literal in every CommonMark renderer)."""
    safe = defang(text).replace("\r\n", "\n").replace("\r", "\n")
    longest = max((len(m) for m in re.findall(r"~+", safe)), default=0)
    fence = "~" * max(3, longest + 1)
    return f"{fence}text\n{safe.strip()}\n{fence}"


def _model_cell(text: object) -> str:
    return _cell(defang("" if text is None else str(text)))


def _tag(mode: str) -> str:
    return " **[SIMULATED]**" if mode == "simulated" else ""


def render_markdown(report: dict) -> str:
    simulated = "simulated" in report["source_modes"]
    out = ["# Oyster Pocket session report", ""]
    if simulated:
        out += [f"> **{SIMULATED_BANNER}**", ""]
    out += [
        "| Field | Value |",
        "| --- | --- |",
        f"| Session | `{_cell(report['session_id'])}` |",
        f"| Source modes | {_cell(', '.join(report['source_modes']))} |",
        f"| Linked baseline | {_cell(report['linked_baseline_session_id'] or 'none')} |",
        f"| Generated (UTC) | {_cell(report['generated_at'])} |",
        f"| Report version | {_cell(report['report_version'])} |",
        "",
    ]

    out += ["## Findings", ""]
    findings = [Finding.model_validate(f) for f in report["findings"]]
    if not findings:
        out += ["No findings recorded.", ""]
    else:
        out += [
            "| Classification | Severity | Confidence | Test | Source | Evidence |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for f in findings:
            out.append(
                f"| {f.classification.value}{_tag(f.source_mode.value)} | {f.severity} | {f.confidence} | "
                f"{_cell(f.test_id)} | {f.source_mode.value} | {_cell(', '.join(f.evidence_ids))} |"
            )
        out.append("")
        for f in findings:
            out += [
                f"### {_cell(f.title)}{_tag(f.source_mode.value)}",
                "",
                f"- Finding: `{_cell(f.finding_id)}` (session `{_cell(f.session_id)}`, target "
                f"`{_cell(f.target_id)}`, profile `{_cell(f.profile_id)}`)",
                f"- Classification: **{f.classification.value}**, severity {f.severity}, "
                f"confidence {f.confidence}, source {f.source_mode.value}",
                f"- Expected policy: {_cell(f.expected_policy)}",
                f"- Observed behaviour: {_cell(f.observed_behavior)}",
                f"- Evidence: {', '.join('`' + _cell(e) + '`' for e in f.evidence_ids)}",
            ]
            if f.retest_of:
                out.append(f"- Retest of: `{_cell(f.retest_of)}`")
            for lim in f.limitations:
                out.append(f"- Limitation: {_cell(lim)}")
            out.append("")

    out += ["## AI runs", ""]
    if not report["runs"]:
        out += ["No AI runs for this session.", ""]
    for r in report["runs"]:
        prov = r.get("provider") or {}
        fake = " (fake provider)" if prov.get("is_fake") else ""
        out += [
            f"### `{_cell(r['run_id'])}` - {_cell(r['status'])}",
            "",
            f"- Provider: {_cell(prov.get('name'))} {_cell(prov.get('model') or '')}{fake}",
            f"- Question: {_model_cell(r.get('question'))}",
        ]
        if r.get("error"):
            out.append(f"- Error: {_model_cell(r['error'])}")
        if r.get("answer_markdown"):
            out += [
                "- Model explanation (not verified; classifications above come from the evaluator, "
                "links, images and HTML removed, URLs defanged):",
                "",
                _plain_block(r["answer_markdown"]),
                "",
            ]
        if r.get("cited_evidence_ids"):
            out.append(
                f"- Cited evidence: {', '.join('`' + _cell(e) + '`' for e in r['cited_evidence_ids'])}"
            )
        for lim in r.get("limitations") or []:
            out.append(f"- Model-stated limitation (not verified): {_model_cell(lim)}")
        out.append("")

    out += ["## Evidence index", ""]
    out += [f"> {_cell(x)}" for x in report["limitations"] if x.startswith(INDEX_TRUNCATED)]
    if not report["evidence_index"]:
        out += ["No evidence uploaded.", ""]
    else:
        out += [
            "| Event | Session | Operation | Status | Source | Target |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for e in report["evidence_index"]:
            out.append(
                f"| `{_cell(e['event_id'])}` | `{_cell(e['session_id'])}` | {_cell(e['operation'])} | "
                f"{_cell(e['status'])} | {_cell(e['source_mode'])}{_tag(e['source_mode'])} | "
                f"{_cell(e.get('target_id') or '-')} |"
            )
        out.append("")

    out += ["## Limitations", ""]
    out += [f"- {_cell(x)}" for x in report["limitations"]]
    out.append("")
    return "\n".join(out)
