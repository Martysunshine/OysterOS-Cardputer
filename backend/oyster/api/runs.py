"""AI run status (run.schema.json) and cooperative cancellation."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from oyster.api.common import AnyPrincipal
from oyster.settings import Settings
from oyster.store import RunRec

router = APIRouter()

MAX_STEPS = 32
DEVICE_RESPONSE_LIMIT = 4096
_STEP_KINDS = ("provider_request", "tool_call", "job", "backend_tool", "validation_error", "final_answer")
_ANSWER_KEYS = (
    "answer_markdown",
    "cited_evidence_ids",
    "limitations",
    "suggestions",
    "source_modes",
    "findings",
    "findings_truncated",
)
_FINDING_KEYS = ("finding_id", "test_id", "classification", "severity")
# most decision-relevant first, so a device-side truncation never drops a confirmed finding for a pass
_CLASS_RANK = {
    "confirmed": 0,
    "candidate": 1,
    "inconclusive": 2,
    "unsupported": 3,
    "passed": 4,
    "observation": 5,
}
MAX_DEVICE_FINDINGS = 16
_PENDING_KEYS = ("job_id", "operation", "target_id", "expected_effect", "requires_local_confirmation")
_TRUNC = " [truncated; full text in the session report]"


def _step(raw: dict) -> dict | None:
    if not isinstance(raw, dict) or raw.get("kind") not in _STEP_KINDS:
        return None
    out = {
        "index": int(raw.get("index", 0)),
        "kind": raw["kind"],
        "summary": str(raw.get("summary", ""))[:240],
    }
    for k in ("tool", "job_id", "outcome"):
        if k in raw:
            out[k] = None if raw[k] is None else str(raw[k])
    if "event_ids" in raw:
        out["event_ids"] = [str(x) for x in raw.get("event_ids") or []]
    return out


def _pick(raw: dict | None, keys: tuple[str, ...]) -> dict | None:
    if raw is None:
        return None
    return {k: raw[k] for k in keys if k in raw}


def _answer(raw: dict | None) -> dict | None:
    ans = _pick(raw, _ANSWER_KEYS)
    if ans is not None and "findings" in ans:
        ans["findings"] = [
            {k: f[k] for k in _FINDING_KEYS if k in f} for f in ans["findings"] or [] if isinstance(f, dict)
        ]
    return ans


def _trim_findings(ans: dict, keep: int) -> None:
    """Keep the `keep` most decision-relevant findings (stable order within a class) and count the rest."""
    found = ans.get("findings") or []
    if len(found) <= keep:
        return
    ranked = sorted(found, key=lambda f: _CLASS_RANK.get(f.get("classification"), 9))
    ans["findings"] = ranked[:keep]
    ans["findings_truncated"] = int(ans.get("findings_truncated") or 0) + len(found) - keep


def run_view(run: RunRec, settings: Settings) -> dict:
    b = settings.budgets
    if settings.pricing_configured():
        cost: float | str = round(
            (
                run.input_tokens * settings.ai_price_input_per_mtok
                + run.output_tokens * settings.ai_price_output_per_mtok
            )
            / 1e6,
            6,
        )
    else:
        cost = "unavailable"
    steps = [s for s in (_step(x) for x in run.steps) if s is not None][-MAX_STEPS:]
    prov = run.provider or {}
    return {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "status": run.status,
        "question": run.question[:1000],
        "provider": {
            "name": str(prov.get("name", "unknown")),
            "model": prov.get("model"),
            "is_fake": bool(prov.get("is_fake", False)),
        },
        "budget": {
            "actions_used": run.actions_used,
            "actions_max": b.max_actions,
            "provider_requests_used": run.provider_requests_used,
            "provider_requests_max": b.max_provider_requests,
            "elapsed_s": float(run.elapsed_s),
            "max_duration_s": float(b.max_duration_s),
        },
        "steps": steps,
        "answer": _answer(run.answer),
        "pending_action": _pick(run.pending_action, _PENDING_KEYS),
        "usage": {"input_tokens": run.input_tokens, "output_tokens": run.output_tokens, "cost": cost},
        "error": run.error[:500] if run.error else None,
    }


def _size(view: dict) -> int:
    return len(json.dumps(view, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def bound_for_device(view: dict, limit: int = DEVICE_RESPONSE_LIMIT) -> dict:
    """Keep a device response under 4 KiB: drop old steps, shorten lists, cap the findings list (most
    relevant first, the dropped count in findings_truncated), then truncate the answer text, and only as a
    last resort drop further findings (counted)."""
    if _size(view) <= limit:
        return view
    view = json.loads(json.dumps(view))
    view["steps"] = view["steps"][-4:]
    view["question"] = view["question"][:240]
    ans = view.get("answer")
    if ans:
        for k in ("limitations", "suggestions"):
            if k in ans:
                ans[k] = [str(x)[:120] for x in ans[k][:3]]
        if "cited_evidence_ids" in ans:
            ans["cited_evidence_ids"] = ans["cited_evidence_ids"][:16]
    if ans and _size(view) > limit:
        _trim_findings(ans, MAX_DEVICE_FINDINGS)
    for keep in (2, 0):
        if _size(view) <= limit:
            break
        view["steps"] = view["steps"][-keep:] if keep else []
        for s in view["steps"]:
            s["summary"] = s["summary"][:80]
    if ans and "answer_markdown" in ans and _size(view) > limit:
        text = ans["answer_markdown"]
        over = _size(view) - limit
        ans["answer_markdown"] = text[: max(0, len(text) - over - len(_TRUNC) - 16)] + _TRUNC
        while _size(view) > limit and len(ans["answer_markdown"]) > len(_TRUNC):
            ans["answer_markdown"] = ans["answer_markdown"][: -len(_TRUNC) - 64] + _TRUNC
    while ans and ans.get("findings") and _size(view) > limit:  # authoritative data goes last
        _trim_findings(ans, len(ans["findings"]) - 1)
    if view.get("error") and _size(view) > limit:
        view["error"] = view["error"][:120]
    return view


@router.get("/v1/runs/{run_id}")
def get_run(run_id: str, request: Request, p: AnyPrincipal) -> dict:
    run = request.app.state.store.get_run(run_id, p.scope)
    view = run_view(run, request.app.state.settings)
    return view if p.is_owner else bound_for_device(view)


@router.post("/v1/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request, p: AnyPrincipal) -> dict:
    """Cooperative: queued jobs of the run become cancelled; an active run is finalised by the worker."""
    run = request.app.state.store.request_run_cancel(run_id, p.scope)
    return {"status": run.status}
