#!/usr/bin/env python3
"""Claude Code PermissionRequest hook: let the Oyster device answer "may this tool run?".

Claude Code runs this command when it is about to ask the user for permission and writes the event as JSON
to stdin (fields used: hook_event_name, session_id, tool_name, tool_input). The hook posts a short, redacted
summary to the Oyster backend on this PC (loopback only), waits for the owner to press allow or deny on the
Cardputer, and prints the decision JSON that Claude Code documents for the PermissionRequest event:

    allow: {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    deny:  {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": {"behavior": "deny", "message": "..."}}}

Anything else (no answer in time, backend down or not configured, bad input, any error) prints nothing and
exits 0, which Claude Code treats as "no decision": the normal permission prompt appears. "allow" is only
ever printed for an answer the backend recorded from a paired device. Standard library only.

Environment (all optional):
  OYSTER_BACKEND      base URL, default http://127.0.0.1:8000 (loopback only: the owner token goes there)
  OYSTER_ADMIN_TOKEN  owner token; otherwise read from the .env file below
  OYSTER_ENV_FILE     path of a .env file holding OYSTER_ADMIN_TOKEN (default: backend/.env of this checkout)
  OYSTER_WAIT_S       seconds to wait for the device, 1..120, default 90
  OYSTER_ON_TIMEOUT   "ask" (default: normal prompt) or "deny" (deny when the device did not answer in time)
  OYSTER_HOOK_DISABLE set to 1 to switch the hook off without editing the Claude Code settings

Run `python oyster_hook.py --selftest` to check the script against a fake local server.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import os
import re
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

EVENT = "PermissionRequest"
DEFAULT_BACKEND = "http://127.0.0.1:8000"
WAIT_DEFAULT, WAIT_MIN, WAIT_MAX = 90, 1, 120
SESSION_MAX, TOOL_MAX, SUMMARY_MAX, COMMAND_MAX = 64, 40, 160, 80
STDIN_MAX = 8 * 1024 * 1024  # a Write call carries the whole file; larger input is not worth reading
REPLY_MAX = 4096
WORK_WINDOW = 2000  # characters of a command or path that are examined; the rest is only counted
CONNECT_TIMEOUT_S = 1.0
SHELL_TOOLS = ("Bash", "PowerShell")
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
DENY_MESSAGE = "Denied from the Oyster device."
TIMEOUT_DENY_MESSAGE = "No answer from the Oyster device in time; denied because OYSTER_ON_TIMEOUT=deny."

REDACTED = "[redacted]"

# --- redaction ----------------------------------------------------------------------------------------
# Best effort, and deliberately greedy: a summary that hides too much only costs readability. The backend
# redacts again; this pass keeps secrets from ever leaving the Claude Code process tree.
_CRED_NAME = (
    r"(?:[\w.-]{0,80}(?:pass(?:word|wd)?|pwd|token|secret|api[_-]?key|auth(?:orization)?|credential|cookie"
    r"|private[_-]?key|access[_-]?key|session[_-]?key|signature)[\w.-]{0,80}|key)"
)
_VALUE = r"""(?:(?:bearer|basic|token|digest)\s+)?(?:"[^"]*"|'[^']*'|\S+)"""
_KV = re.compile(rf"(?i)(?<![\w-])({_CRED_NAME})(\s*[=:]\s*)({_VALUE})")
_FLAG = re.compile(rf"(?i)(--?{_CRED_NAME})(\s+|=)({_VALUE})")
_PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_CREDENTIALS = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_VENDOR = re.compile(
    r"\b(?:(?:gsk|sk|pk|rk|xox[abp])[-_][A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*)"
)
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_RUN = re.compile(r"(?<![A-Za-z0-9+_=-])[A-Za-z0-9+_=-]{32,}(?![A-Za-z0-9+_=-])")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_NEWLINE = re.compile(r"[ \t]*\r?\n[ \t\r\n]*")


def _mask_run(m: re.Match) -> str:
    s = m.group(0)
    return REDACTED if any(c.isalpha() for c in s) and any(c.isdigit() for c in s) else s


def _keep_name(m: re.Match) -> str:
    return f"{m.group(1)}{m.group(2)}{REDACTED}"


def redact(text: str) -> str:
    """`text` with credential-looking substrings removed and control characters turned into spaces."""
    t = _PEM.sub(REDACTED, text)
    t = _BEARER.sub("Bearer " + REDACTED, t)
    t = _CTRL.sub(" ", t)
    t = _URL_CREDENTIALS.sub(rf"\1{REDACTED}@", t)
    t = _FLAG.sub(_keep_name, t)
    t = _KV.sub(_keep_name, t)
    t = _VENDOR.sub(REDACTED, t)
    t = _LONG_HEX.sub(REDACTED, t)
    return _RUN.sub(_mask_run, t)


def clip(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max(0, max_chars - 3)] + "..."


# --- summary ------------------------------------------------------------------------------------------


def _line_break(_: re.Match) -> str:
    return " \\n "  # a function, not a template: re.sub would turn a template's backslash-n into a newline


def _clean_command(command: str) -> str:
    """One line; line breaks stay visible as a literal backslash-n so a hidden second line is noticed."""
    return _SPACE.sub(" ", _NEWLINE.sub(_line_break, command.strip())).strip()


def summarize(tool_name: str, tool_input: object) -> str:
    """Shell tools: the start of the command (a longer one ends in "...[total length]"). File tools: the file
    name only, never the path or the content. Other tools: empty (the tool name is sent separately)."""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return ""
        command = command.strip()
        # Only the start is ever shown, so only the start is processed: this bounds the regex work however
        # large a command (or a pathological one) Claude Code sends. Secrets are removed before the cut.
        shown = redact(_clean_command(command[:WORK_WINDOW])).strip()
        if len(command) <= COMMAND_MAX and len(shown) <= COMMAND_MAX:
            return shown
        marker = f"...[{len(command)}]"
        return shown[: COMMAND_MAX - len(marker)] + marker
    if tool_name in FILE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(path, str):
            return ""
        base = re.split(r"[\\/]", path.strip()[-WORK_WINDOW:])[-1]
        return clip(redact(base).strip(), COMMAND_MAX)
    return ""


def _ident(value: object, max_chars: int, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:max_chars]
    return cleaned or fallback


# --- configuration ------------------------------------------------------------------------------------


def _read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file ("export " and quotes tolerated). Only used for two keys."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        out[key] = value
    return out


def default_env_file() -> Path:
    return Path(__file__).resolve().parents[2] / "backend" / ".env"


def load_settings(env: Mapping[str, str]) -> tuple[str, str] | None:
    """(base_url, token) or None when the hook is not configured or the URL is not loopback."""
    file_values: dict[str, str] = {}
    token = env.get("OYSTER_ADMIN_TOKEN", "").strip()
    port_hint = ""
    env_file = env.get("OYSTER_ENV_FILE", "").strip()
    path = Path(env_file) if env_file else default_env_file()
    if not token or not env.get("OYSTER_BACKEND", "").strip():
        file_values = _read_env_file(path)
        token = token or file_values.get("OYSTER_ADMIN_TOKEN", "").strip()
        port_hint = file_values.get("OYSTER_HTTP_PORT", "").strip()
    base = env.get("OYSTER_BACKEND", "").strip()
    if not base:
        base = f"http://127.0.0.1:{port_hint}" if port_hint.isdigit() else DEFAULT_BACKEND
    if not token or len(token) > 512 or re.search(r"[\s\x00-\x1f\x7f]", token):
        return None
    if _loopback_url(base) is None:
        return None
    return base.rstrip("/"), token


def _loopback_url(base: str) -> tuple[str, int] | None:
    """(host, port) when `base` is an http URL on a loopback address, else None: the owner token must not
    be sent anywhere else."""
    try:
        parts = urlsplit(base)
        host = (parts.hostname or "").lower()
        port = parts.port or 80
    except ValueError:
        return None
    if parts.scheme != "http" or not host:
        return None
    if host == "localhost":
        return host, port
    try:
        return (host, port) if ipaddress.ip_address(host).is_loopback else None
    except ValueError:
        return None


def wait_seconds(env: Mapping[str, str]) -> int:
    raw = env.get("OYSTER_WAIT_S", "").strip()
    try:
        value = int(raw) if raw else WAIT_DEFAULT
    except ValueError:
        value = WAIT_DEFAULT
    return max(WAIT_MIN, min(WAIT_MAX, value))


# --- backend call -------------------------------------------------------------------------------------


def post_json(base: str, path: str, token: str, payload: dict, timeout: float) -> tuple[int, bytes]:
    """POST without proxies (http.client never reads proxy variables) and return (status, first bytes)."""
    target = _loopback_url(base)
    if target is None:
        raise ValueError("not a loopback URL")
    # Connect with a short timeout of its own: on Windows a closed loopback port is only reported after
    # about two seconds, and that delay would be added to every prompt while the backend is not running.
    sock = socket.create_connection(target, timeout=min(CONNECT_TIMEOUT_S, timeout))
    sock.settimeout(timeout)
    conn = http.client.HTTPConnection(target[0], target[1], timeout=timeout)
    conn.sock = sock
    try:
        body = json.dumps(payload).encode("utf-8")
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Connection": "close",
            },
        )
        resp = conn.getresponse()
        return resp.status, resp.read(REPLY_MAX)
    finally:
        conn.close()


Poster = Callable[[str, str, str, dict, float], tuple[int, bytes]]


def render(decision: str, message: str = DENY_MESSAGE) -> str:
    """The stdout text for a decision; "" means no decision (Claude Code shows its normal prompt)."""
    if decision == "allow":
        behavior: dict = {"behavior": "allow"}
    elif decision == "deny":
        behavior = {"behavior": "deny", "message": message}
    else:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": EVENT, "decision": behavior}})


def handle(raw: bytes, env: Mapping[str, str], poster: Poster = post_json) -> str:
    """The whole hook: stdin bytes in, stdout text out. Never raises; every failure is ""."""
    try:
        return _handle(raw, env, poster)
    except Exception:  # noqa: BLE001 - a hook must never break the prompt it is meant to help with
        return ""


def _handle(raw: bytes, env: Mapping[str, str], poster: Poster) -> str:
    if env.get("OYSTER_HOOK_DISABLE", "").strip() == "1":
        return ""
    event = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(event, dict):
        return ""
    if event.get("hook_event_name", EVENT) != EVENT:
        return ""  # wired to another event by mistake: the output shape would not apply
    settings = load_settings(env)
    if settings is None:
        return ""
    base, token = settings
    tool_name = event.get("tool_name")
    payload = {
        "session": _ident(event.get("session_id"), SESSION_MAX, "unknown"),
        "tool": _ident(tool_name, TOOL_MAX, "tool"),
        "summary": clip(
            summarize(tool_name if isinstance(tool_name, str) else "", event.get("tool_input")), SUMMARY_MAX
        ),
        "wait_s": wait_seconds(env),
    }
    status, reply = poster(base, "/v1/claude/approvals", token, payload, payload["wait_s"] + 10)
    if status != 200:
        return ""
    answer = json.loads(reply.decode("utf-8"))
    if not isinstance(answer, dict) or not isinstance(answer.get("approval_id"), str):
        return ""
    decision = answer.get("decision")
    if decision in ("allow", "deny"):
        return render(decision)
    if decision == "timeout" and env.get("OYSTER_ON_TIMEOUT", "").strip().lower() == "deny":
        return render("deny", TIMEOUT_DENY_MESSAGE)
    return ""


def _arm_watchdog(seconds: float) -> None:
    """Last resort so the hook can never hang Claude Code: end the process silently (= no decision)."""
    timer = threading.Timer(seconds, lambda: os._exit(0))
    timer.daemon = True
    timer.start()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--selftest" in args:
        return selftest()
    with contextlib.suppress(Exception):  # see handle(): nothing may escape and no noise is printed
        _arm_watchdog(wait_seconds(os.environ) + 15)
        raw = sys.stdin.buffer.read(STDIN_MAX + 1)
        text = handle(raw if len(raw) <= STDIN_MAX else b"", os.environ)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
    return 0


# --- self test ----------------------------------------------------------------------------------------


def selftest() -> int:
    """Run the hook against a fake local server and print one line per check. Exit 1 if any check fails."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    fake_token = "selftest-token-0123456789abcdef-xyz"  # noqa: S105 - not a credential
    seen: list[dict] = []

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            seen.append({"auth": self.headers.get("Authorization"), "body": body})
            word = body.get("summary", "")
            if word == "http-500":
                self.send_response(500)
                self.end_headers()
                return
            if word == "garbage":
                data = b"not json"
            else:
                decision = {"allow-me": "allow", "deny-me": "deny"}.get(word, "timeout")
                data = json.dumps({"decision": decision, "approval_id": "appr-0123456789abcdef"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    env = {"OYSTER_BACKEND": f"http://127.0.0.1:{server.server_address[1]}", "OYSTER_ADMIN_TOKEN": fake_token}

    def event(command: str, **extra) -> bytes:
        data = {
            "hook_event_name": EVENT,
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        return json.dumps({**data, **extra}).encode()

    def decision_of(out: str) -> str:
        if not out:
            return "none"
        return json.loads(out)["hookSpecificOutput"]["decision"]["behavior"]

    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, ok))
        print(("ok   " if ok else "FAIL ") + name)

    check("allow", decision_of(handle(event("allow-me"), env)) == "allow")
    check("deny", decision_of(handle(event("deny-me"), env)) == "deny")
    check("timeout falls back to the normal prompt", handle(event("whatever"), env) == "")
    check(
        "timeout with OYSTER_ON_TIMEOUT=deny",
        decision_of(handle(event("x"), {**env, "OYSTER_ON_TIMEOUT": "deny"})) == "deny",
    )
    check("http 500 falls back", handle(event("http-500"), env) == "")
    check("non-json reply falls back", handle(event("garbage"), env) == "")
    check("malformed stdin falls back", handle(b"{not json", env) == "")
    check("other event is ignored", handle(event("allow-me", hook_event_name="PreToolUse"), env) == "")
    check(
        "no token falls back",
        handle(event("allow-me"), {"OYSTER_BACKEND": env["OYSTER_BACKEND"], "OYSTER_ENV_FILE": os.devnull})
        == "",
    )
    closed = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    port = closed.server_address[1]
    closed.server_close()
    check(
        "backend down falls back",
        handle(event("allow-me"), {**env, "OYSTER_BACKEND": f"http://127.0.0.1:{port}"}) == "",
    )
    check(
        "non-loopback URL is refused",
        handle(event("allow-me"), {**env, "OYSTER_BACKEND": "http://192.168.1.50:8000"}) == "",
    )
    seen.clear()
    handle(
        event("curl -H 'Authorization: Bearer abcdefghijklmnop' --password hunter2 api_key=abc123 allow-me"),
        env,
    )
    sent = json.dumps(seen[0]["body"]) if seen else ""
    check(
        "secrets are redacted before sending",
        bool(seen) and not any(s in sent for s in ("abcdefghijklmnop", "hunter2", "abc123")),
    )
    check(
        "token is sent as a bearer header only",
        bool(seen) and seen[0]["auth"] == f"Bearer {fake_token}" and fake_token not in sent,
    )
    server.shutdown()
    bad = [n for n, ok in results if not ok]
    print(f"{len(results) - len(bad)}/{len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
