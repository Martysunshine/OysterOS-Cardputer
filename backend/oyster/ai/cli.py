"""AI subcommands for the `oyster` CLI: `ai-check` and `ask`.

oyster/cli.py calls register(subparsers); each subcommand sets `func(args) -> int`.
Neither command prints or writes the API key or the admin token.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from oyster.ai.provider import ProviderError
from oyster.ai.redact import clip, redact_text
from oyster.settings import Settings, load_settings

# Preference order when AI_MODEL is empty (ADR-002 lists these as tool-capable today). Only used to
# suggest a model; runs always use AI_MODEL.
TOOL_CAPABLE_CANDIDATES = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
)
TERMINAL_RUN = ("succeeded", "failed", "cancelled", "expired", "budget_exhausted", "ai_unavailable")
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "record_probe",
        "description": "Record the connectivity probe value.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string", "enum": ["oyster-check"]}},
            "required": ["value"],
            "additionalProperties": False,
        },
    },
}


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "ai-check",
        help="List provider models, verify a tool-call round trip with "
        "AI_MODEL, write data/provider_check.json (no key)",
    )
    p.add_argument("--model", help="Test this model id instead of AI_MODEL (runs still use AI_MODEL only)")
    p.set_defaults(func=cmd_ai_check)
    q = subparsers.add_parser("ask", help="Ask the AI about a session through the local backend API")
    q.add_argument("--session", required=True, help="Session id (sess-...)")
    q.add_argument("question", help="Question text (<= 1000 chars)")
    q.add_argument(
        "--backend", default=None, help="Backend base URL (default http://127.0.0.1:<OYSTER_HTTP_PORT>)"
    )
    q.add_argument(
        "--allow-remote-admin",
        action="store_true",
        help="Send the admin token to a non-loopback --backend (default: refused; plain http is then "
        "sent in clear text)",
    )
    q.add_argument("--timeout", type=float, default=600.0, help="Seconds to wait for the run")
    q.add_argument("--poll", type=float, default=1.0, help="Poll interval in seconds")
    q.set_defaults(func=cmd_ask)


# ---------------------------------------------------------------- ai-check
def _round_trip(provider, model: str) -> dict:
    """Tool call -> tool result -> second response, all against `model`."""
    t0 = time.monotonic()
    msgs: list[dict] = [
        {
            "role": "system",
            "content": "You are a connectivity check for a tool-calling API. Call the "
            "record_probe tool exactly once with value oyster-check.",
        },
        {"role": "user", "content": "Run the probe now."},
    ]
    usage = {"input_tokens": 0, "output_tokens": 0}

    def add_usage(data: dict) -> None:
        u = data.get("usage") or {}
        usage["input_tokens"] += int(u.get("prompt_tokens") or 0)
        usage["output_tokens"] += int(u.get("completion_tokens") or 0)

    data1 = provider.chat(msgs, [_PROBE_TOOL], model=model, max_tokens=512)
    add_usage(data1)
    message = ((data1.get("choices") or [{}])[0] or {}).get("message") or {}
    calls = message.get("tool_calls") or []
    tool_call_ok = False
    if calls:
        fn = calls[0].get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_call_ok = (
            fn.get("name") == "record_probe"
            and isinstance(args, dict)
            and args.get("value") == "oyster-check"
        )
    if not tool_call_ok:
        return {
            "ok": False,
            "tool_call_ok": False,
            "tool_result_ok": False,
            "usage": usage,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "detail": "model did not return the expected record_probe tool call",
        }
    call = calls[0]
    msgs += [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.get("id") or "call_probe",
                    "type": "function",
                    "function": {
                        "name": "record_probe",
                        "arguments": call["function"].get("arguments") or "{}",
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call.get("id") or "call_probe", "content": '{"recorded":true}'},
    ]
    data2 = provider.chat(msgs, [_PROBE_TOOL], model=model, max_tokens=512)
    add_usage(data2)
    tool_result_ok = bool(data2.get("choices"))
    return {
        "ok": tool_result_ok,
        "tool_call_ok": True,
        "tool_result_ok": tool_result_ok,
        "usage": usage,
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }


def run_ai_check(
    settings: Settings, *, model_override: str | None = None, transport: httpx.BaseTransport | None = None
) -> dict:
    """Check the configured provider. Returns the record written to data/provider_check.json."""
    from oyster.ai.openai_compat import OpenAICompatProvider

    secrets = tuple(s for s in (settings.ai_api_key, settings.admin_token) if s)
    result: dict = {
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": settings.ai_provider,
        "base_url": settings.ai_base_url,
        "model": None,
        "model_source": None,
        "configured_model": settings.ai_model or None,
        "models_listed": None,
        "tool_capable_candidates_listed": [],
        "tested": [],
        "ok": False,
        "error": None,
        "note": None,
    }
    if settings.provider_is_fake:
        result["error"] = "AI_PROVIDER=fake is the test/simulation provider; there is nothing to check"
        return result
    if not settings.ai_api_key:
        result["error"] = "AI_API_KEY is not set (backend/.env)"
        return result
    try:
        provider = OpenAICompatProvider(
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            name=settings.ai_provider,
            timeout_s=settings.ai_timeout_s,
            max_output_tokens=settings.ai_max_output_tokens,
            transport=transport,
        )
    except ValueError as e:
        result["error"] = str(e)
        return result
    try:
        models = provider.list_models()
        ids = sorted(m["id"] for m in models if m.get("active", True) is not False)
        result["models_listed"] = len(ids)
        result["tool_capable_candidates_listed"] = [c for c in TOOL_CAPABLE_CANDIDATES if c in ids]
        wanted = model_override or settings.ai_model
        if wanted:
            result["model_source"] = "--model" if model_override else "AI_MODEL"
            if ids and wanted not in ids:
                result["error"] = (
                    f"model {wanted!r} is not listed (or not active) at {settings.ai_base_url}/models"
                )
                return result
            to_test = [wanted]
        else:
            result["model_source"] = "auto-pick"
            to_test = result["tool_capable_candidates_listed"]
            if not to_test:
                result["error"] = "AI_MODEL is empty and no known tool-capable model is listed; set AI_MODEL"
                return result
        for model in to_test:
            try:
                rt = _round_trip(provider, model)
            except ProviderError as e:
                rt = {"ok": False, "detail": clip(redact_text(str(e), secrets), 300)}
            result["tested"].append(dict(model=model, **rt))
            if rt.get("ok"):
                result["model"] = model
                result["ok"] = True
                break
        if not result["ok"]:
            result["error"] = "no tested model completed a tool-call round trip"
        elif result["model_source"] == "auto-pick":
            result["note"] = (
                f"AI_MODEL is empty. Set AI_MODEL={result['model']} in backend/.env; runs use AI_MODEL only."
            )
        elif model_override and model_override != settings.ai_model:
            result["note"] = f"Tested --model only. Runs use AI_MODEL ({settings.ai_model or 'unset'})."
    except ProviderError as e:
        result["error"] = clip(redact_text(str(e), secrets), 300)
    finally:
        provider.close()
    return result


def write_provider_check(settings: Settings, result: dict) -> Path:
    path = Path(settings.data_dir) / "provider_check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=2, sort_keys=True)
    for s in (settings.ai_api_key, settings.admin_token):
        if s and s in text:  # defence in depth: the record never contains secrets
            raise RuntimeError("refusing to write a provider check that contains a secret")
    path.write_text(text + "\n", encoding="utf-8")
    return path


def cmd_ai_check(args: argparse.Namespace, settings: Settings | None = None) -> int:
    settings = settings or load_settings()
    result = run_ai_check(settings, model_override=getattr(args, "model", None))
    path = write_provider_check(settings, result)
    print(f"provider: {result['provider']}  base_url: {result['base_url']}")
    if result["models_listed"] is not None:
        print(
            f"models listed: {result['models_listed']}; tool-capable candidates: "
            f"{', '.join(result['tool_capable_candidates_listed']) or 'none'}"
        )
    for t in result["tested"]:
        print(f"  {t['model']}: {'round trip OK' if t.get('ok') else 'FAILED - ' + str(t.get('detail', ''))}")
    if result["ok"]:
        print(f"OK: {result['model']} supports a tool-call round trip.")
    else:
        print(f"NOT OK: {result['error']}")
    if result["note"]:
        print(result["note"])
    print(f"wrote {path}")
    return 0 if result["ok"] else 1


# ---------------------------------------------------------------- ask
def run_ask(
    base_url: str,
    token: str,
    session_id: str,
    question: str,
    *,
    poll_s: float = 1.0,
    timeout_s: float = 600.0,
    client: httpx.Client | None = None,
    out: Callable[[str], object] = print,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    own = client is None
    http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0, follow_redirects=False)
    headers = {"Authorization": f"Bearer {token}"}
    run_id = None
    try:
        r = http.post(
            f"/v1/sessions/{session_id}/assistant/messages", json={"question": question}, headers=headers
        )
        if r.status_code not in (200, 201, 202):
            out(f"error: backend returned {r.status_code}: {_err(r)}")
            return 2
        body = r.json()
        run_id = body["run_id"]
        out(f"run {run_id}: {body.get('status')}")
        printed = -1
        last_status = body.get("status")
        start = clock()
        run: dict = {}
        while True:
            g = http.get(f"/v1/runs/{run_id}", headers=headers)
            if g.status_code != 200:
                out(f"error: backend returned {g.status_code}: {_err(g)}")
                return 2
            run = g.json()
            for step in run.get("steps", []):
                if step.get("index", -1) > printed:
                    printed = step["index"]
                    tool = f" {step['tool']}" if step.get("tool") else ""
                    outcome = f" [{step['outcome']}]" if step.get("outcome") else ""
                    out(f"  #{step['index']} {step.get('kind')}{tool}{outcome}: {step.get('summary', '')}")
            status = run.get("status")
            if status != last_status:
                out(f"status: {status}")
                last_status = status
                pa = run.get("pending_action") or {}
                if status == "awaiting_local_confirmation" and pa:
                    out(
                        f"  >> confirm on the Cardputer: {pa.get('expected_effect')} (job {pa.get('job_id')})"
                    )
            if status in TERMINAL_RUN:
                break
            if clock() - start > timeout_s:
                out(f"timed out after {timeout_s:g} s waiting for run {run_id}; requesting cancellation")
                http.post(f"/v1/runs/{run_id}/cancel", headers=headers)
                return 3
            sleep(poll_s)
        _print_result(run, out)
        return 0 if run.get("status") == "succeeded" else 1
    except KeyboardInterrupt:
        if run_id:
            http.post(f"/v1/runs/{run_id}/cancel", headers=headers)
            out(f"cancellation requested for {run_id}")
        return 130
    except httpx.HTTPError as e:
        out(f"error: cannot reach the backend at {base_url} ({type(e).__name__})")
        return 2
    finally:
        if own:
            http.close()


def _err(r: httpx.Response) -> str:
    try:
        d = r.json()
        return clip(f"{d.get('error')}: {d.get('detail', '')}", 200)
    except ValueError:
        return clip(r.text, 200)


def _print_result(run: dict, out: Callable[[str], object]) -> None:
    prov = run.get("provider") or {}
    fake = " (FAKE PROVIDER - no language model)" if prov.get("is_fake") else ""
    out(f"provider: {prov.get('name')} model: {prov.get('model')}{fake}")
    if run.get("error"):
        out(f"error: {run['error']}")
    ans = run.get("answer")
    if ans:
        out("")
        out(ans.get("answer_markdown", ""))
        out("")
        out(f"evidence: {', '.join(ans.get('cited_evidence_ids', [])) or 'none cited'}")
        if ans.get("source_modes"):
            out(f"source modes: {', '.join(ans['source_modes'])}")
        for x in ans.get("limitations", []):
            out(f"limitation: {x}")
        for x in ans.get("suggestions", []):
            out(f"suggestion: {x}")
        for f in ans.get("findings", []):  # evaluator values, authoritative next to the model's wording
            out(
                f"finding: {f.get('test_id')} -> {f.get('classification')} ({f.get('severity')}) "
                f"[{f.get('finding_id')}]"
            )
    usage = run.get("usage") or {}
    budget = run.get("budget") or {}
    out(
        f"usage: {usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out tokens, "
        f"cost {usage.get('cost', 'unavailable')}; "
        f"actions {budget.get('actions_used')}/{budget.get('actions_max')}, "
        f"model requests {budget.get('provider_requests_used')}/{budget.get('provider_requests_max')}"
    )


def is_loopback_url(url: str) -> bool:
    """True for http(s)://localhost, 127.0.0.0/8 and [::1] only (names are not resolved)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    if u.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(u.hostname).is_loopback
    except ValueError:
        return False


def admin_target_problem(url: str, *, allow_remote_admin: bool = False) -> str | None:
    """Why the admin token must not be sent to `url`, or None. The token goes only to a loopback backend
    unless the owner passes --allow-remote-admin; any other scheme than http(s) is always refused."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "the backend URL must be http(s)://host[:port]"
    if is_loopback_url(url):
        return None
    if not allow_remote_admin:
        return (
            f"refusing to send the admin token to non-loopback host {u.hostname!r}; "
            "pass --allow-remote-admin only if you really mean it"
        )
    return None


def cmd_ask(args: argparse.Namespace, settings: Settings | None = None) -> int:
    settings = settings or load_settings()
    if not settings.admin_token:
        print("error: OYSTER_ADMIN_TOKEN is not set (backend/.env)", file=sys.stderr)
        return 2
    base = args.backend or f"http://127.0.0.1:{settings.http_port}"
    problem = admin_target_problem(base, allow_remote_admin=bool(getattr(args, "allow_remote_admin", False)))
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    if not is_loopback_url(base) and urlparse(base).scheme == "http":
        print("warning: the admin token is sent over plain http to a remote host", file=sys.stderr)
    return run_ask(
        base, settings.admin_token, args.session, args.question, poll_s=args.poll, timeout_s=args.timeout
    )
