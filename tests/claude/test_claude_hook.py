"""integrations/claude-code/oyster_hook.py against a fake loopback backend: the exact JSON printed for allow
and deny, silence (= Claude Code's normal prompt) for everything else, redaction and the loopback rule."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time

import pytest
from _claude_support import SCRIPT_DIR, FakeBackend, closed_port, load_script

hook = load_script("oyster_hook")

TOKEN = "fake-owner-token-0123456789abcdef"
APPR = "appr-0123456789abcdef"
ALLOW = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
DENY = {
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "deny", "message": "Denied from the Oyster device."},
    }
}


def answer(decision: str):
    return lambda path, body: (200, {"decision": decision, "approval_id": APPR})


@pytest.fixture
def backend():
    made: list[FakeBackend] = []

    def make(reply=None) -> FakeBackend:
        b = FakeBackend(reply)
        made.append(b)
        return b

    yield make
    for b in made:
        b.close()


@pytest.fixture
def env(tmp_path):
    """Hook environment: the token comes from the variable, never from the real backend/.env."""
    return {"OYSTER_ADMIN_TOKEN": TOKEN, "OYSTER_ENV_FILE": str(tmp_path / "no-such.env")}


def event(tool="Bash", tool_input=None, **extra) -> bytes:
    data = {
        "session_id": "sess-1",
        "transcript_path": "/x/t.jsonl",
        "cwd": "/x",
        "permission_mode": "default",
        "hook_event_name": "PermissionRequest",
        "tool_name": tool,
        "tool_input": {"command": "npm test"} if tool_input is None else tool_input,
        "permission_suggestions": [{"type": "addRules"}],
    }
    return json.dumps({**data, **extra}).encode()


def run(b, raw: bytes, env: dict, **more) -> str:
    return hook.handle(raw, {**env, "OYSTER_BACKEND": b.url, **more})


# --- what is printed ----------------------------------------------------------------------------------


def test_allow_prints_exactly_the_documented_allow_json(backend, env):
    b = backend(answer("allow"))
    assert json.loads(run(b, event(), env)) == ALLOW


def test_deny_prints_exactly_the_documented_deny_json(backend, env):
    b = backend(answer("deny"))
    assert json.loads(run(b, event(), env)) == DENY


def test_the_output_is_one_json_object_and_nothing_else(backend, env):
    out = run(backend(answer("allow")), event(), env)
    assert out == out.strip() and "\n" not in out and out.startswith("{") and out.endswith("}")
    assert out.isascii()


def test_the_request_to_the_backend(backend, env):
    b = backend(answer("allow"))
    run(b, event(), env, OYSTER_WAIT_S="30")
    (req,) = b.requests
    assert req["path"] == "/v1/claude/approvals" and req["auth"] == f"Bearer {TOKEN}"
    assert req["body"] == {"session": "sess-1", "tool": "Bash", "summary": "npm test", "wait_s": 30}
    assert TOKEN not in req["raw"].decode()


@pytest.mark.parametrize("decision", ["timeout", "ask", "maybe", "", None, 5, ["allow"]])
def test_anything_but_allow_or_deny_is_silence(backend, env, decision):
    b = backend(lambda p, body: (200, {"decision": decision, "approval_id": APPR}))
    assert run(b, event(), env) == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "allow"},  # no approval id: not a recorded decision
        {"decision": "allow", "approval_id": 5},
        {"decision": "allow", "approval_id": None},
        ["allow"],
        "allow",
        None,
        {},
    ],
)
def test_an_allow_without_an_approval_id_is_never_honoured(backend, env, payload):
    b = backend(lambda p, body: (200, payload))
    assert run(b, event(), env) == ""


@pytest.mark.parametrize("status", [201, 204, 301, 400, 401, 403, 404, 409, 422, 429, 500, 503])
def test_any_non_200_is_silence_even_with_an_allow_body(backend, env, status):
    b = backend(lambda p, body: (status, {"decision": "allow", "approval_id": APPR}))
    assert run(b, event(), env) == ""


@pytest.mark.parametrize("raw", [b"not json", b"<html>", b"", b"\xff\xfe\x00", b'{"decision": "allow"'])
def test_unparseable_replies_are_silence(backend, env, raw):
    b = backend(lambda p, body: (200, raw))
    assert run(b, event(), env) == ""


def test_backend_down_is_silence(env):
    out = hook.handle(event(), {**env, "OYSTER_BACKEND": f"http://127.0.0.1:{closed_port()}"})
    assert out == ""


def test_timeout_answer_is_silence_by_default_and_deny_only_on_request(backend, env):
    b = backend(answer("timeout"))
    assert run(b, event(), env) == ""
    for value in ("deny", "DENY", " deny "):
        assert (
            json.loads(run(b, event(), env, OYSTER_ON_TIMEOUT=value))["hookSpecificOutput"]["decision"][
                "behavior"
            ]
            == "deny"
        )
    for value in ("ask", "", "allow", "anything"):
        assert run(b, event(), env, OYSTER_ON_TIMEOUT=value) == ""


def test_deny_on_timeout_does_not_apply_to_errors(backend, env):
    down = {**env, "OYSTER_BACKEND": f"http://127.0.0.1:{closed_port()}", "OYSTER_ON_TIMEOUT": "deny"}
    assert hook.handle(event(), down) == ""
    b = backend(lambda p, body: (500, {"decision": "timeout", "approval_id": APPR}))
    assert run(b, event(), env, OYSTER_ON_TIMEOUT="deny") == ""


def test_a_slow_backend_cannot_hang_the_hook():
    b = FakeBackend(lambda p, body: (time.sleep(2), (200, {}))[1])
    try:
        started = time.monotonic()
        with pytest.raises(OSError):
            hook.post_json(b.url, "/v1/claude/approvals", TOKEN, {}, 0.3)
        assert time.monotonic() - started < 1.5
    finally:
        b.close()


def test_a_poster_that_raises_anything_is_silence(env):
    for exc in (TimeoutError(), ConnectionResetError(), ValueError("x"), RuntimeError("boom"), KeyError("k")):

        def poster(*a, _e=exc):
            raise _e

        assert hook.handle(event(), {**env, "OYSTER_BACKEND": "http://127.0.0.1:1"}, poster) == ""


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b"null",
        b"42",
        b'"allow"',
        b"\xff\xfe",
        pytest.param(b"{" * 5000, id="open-braces"),
        pytest.param(b"[" * 100000, id="deep-nesting"),
    ],
)
def test_malformed_input_is_silence(backend, env, raw):
    b = backend(answer("allow"))
    assert run(b, raw, env) == ""
    assert b.requests == []


def test_a_non_string_tool_name_still_works(backend, env):
    b = backend(answer("allow"))
    assert json.loads(run(b, b'{"tool_name": 5, "tool_input": 7}', env)) == ALLOW
    assert b.requests[0]["body"]["tool"] == "tool" and b.requests[0]["body"]["summary"] == ""


def test_another_event_is_ignored_without_contacting_the_backend(backend, env):
    b = backend(answer("allow"))
    for name in ("PreToolUse", "Notification", "SessionStart", ""):
        assert run(b, event(hook_event_name=name), env) == ""
    assert b.requests == []


def test_a_missing_event_name_is_tolerated(backend, env):
    data = json.loads(event())
    del data["hook_event_name"]
    assert json.loads(run(backend(answer("allow")), json.dumps(data).encode(), env)) == ALLOW


def test_unknown_extra_fields_are_tolerated(backend, env):
    raw = event(brand_new_field={"nested": [1, 2, 3]}, prompt_id="x")
    assert json.loads(run(backend(answer("deny")), raw, env)) == DENY


def test_the_kill_switch(backend, env):
    b = backend(answer("allow"))
    assert run(b, event(), env, OYSTER_HOOK_DISABLE="1") == ""
    assert b.requests == []


# --- configuration ------------------------------------------------------------------------------------


def test_no_token_means_silence_and_no_request(backend, tmp_path):
    b = backend(answer("allow"))
    cfg = {"OYSTER_BACKEND": b.url, "OYSTER_ENV_FILE": str(tmp_path / "missing.env")}
    assert hook.handle(event(), cfg) == ""
    assert hook.handle(event(), {**cfg, "OYSTER_ADMIN_TOKEN": "   "}) == ""
    assert b.requests == []


@pytest.mark.parametrize("token", ["has space inside", "line\nbreak", "tab\there", "x" * 513, "ctl\x01char"])
def test_a_malformed_token_is_never_sent(backend, env, token):
    b = backend(answer("allow"))
    assert run(b, event(), {**env, "OYSTER_ADMIN_TOKEN": token}) == ""
    assert b.requests == []


def test_token_and_port_come_from_the_env_file(backend, tmp_path):
    b = backend(answer("allow"))
    f = tmp_path / "backend.env"
    f.write_text(
        "# comment\n\nexport OYSTER_ADMIN_TOKEN='quoted-token-0123456789abcdef'\nOTHER=1\n"
        f"OYSTER_HTTP_PORT={b.port}\n",
        encoding="utf-8",
    )
    assert json.loads(hook.handle(event(), {"OYSTER_ENV_FILE": str(f)})) == ALLOW
    assert b.requests[0]["auth"] == "Bearer quoted-token-0123456789abcdef"


def test_the_environment_variable_wins_over_the_env_file(backend, tmp_path):
    b = backend(answer("allow"))
    f = tmp_path / "backend.env"
    f.write_text("OYSTER_ADMIN_TOKEN=from-the-file-0123456789abcdef\n", encoding="utf-8")
    cfg = {"OYSTER_BACKEND": b.url, "OYSTER_ENV_FILE": str(f), "OYSTER_ADMIN_TOKEN": TOKEN}
    hook.handle(event(), cfg)
    assert b.requests[0]["auth"] == f"Bearer {TOKEN}"


def test_the_env_file_value_with_a_trailing_comment_and_double_quotes(tmp_path):
    f = tmp_path / "e.env"
    f.write_text('A="dq-value"\nB=plain # trailing comment\nexport C = spaced\n', encoding="utf-8")
    values = hook._read_env_file(f)
    assert values["A"] == "dq-value" and values["B"] == "plain" and values["C"] == "spaced"
    assert hook._read_env_file(tmp_path / "missing") == {}


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.50:8000",
        "http://192.168.1.51:8000",
        "http://example.test:8000",
        "https://127.0.0.1:8000",
        "ftp://127.0.0.1:8000",
        "http://127.0.0.1.evil.test:8000",
        "http://localhost.evil.test:8000",
        "http://user@192.168.1.50:8000",
        "http://0.0.0.0:8000",
        "file:///etc/passwd",
        "not a url",
        "http://127.0.0.1:99999",
    ],
)
def test_the_token_is_only_ever_sent_to_loopback(backend, env, url):
    cfg = {**env, "OYSTER_BACKEND": url}
    assert hook.load_settings(cfg) is None
    assert hook.handle(event(), cfg) == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
        "http://127.0.0.1:8000/",
        "http://127.1.2.3:80",
    ],
)
def test_loopback_urls_are_accepted(env, url):
    loaded = hook.load_settings({**env, "OYSTER_BACKEND": url})
    assert loaded is not None and loaded[1] == TOKEN and not loaded[0].endswith("/")


def test_proxy_variables_are_ignored(backend, env, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{closed_port()}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    b = backend(answer("allow"))
    assert json.loads(run(b, event(), env)) == ALLOW
    assert len(b.requests) == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5", 5),
        ("1", 1),
        ("0", 1),
        ("-3", 1),
        ("500", 120),
        ("120", 120),
        ("abc", 90),
        ("", 90),
        (" 45 ", 45),
    ],
)
def test_wait_seconds_is_clamped(backend, env, value, expected):
    b = backend(answer("timeout"))
    run(b, event(), env, OYSTER_WAIT_S=value)
    assert b.requests[0]["body"]["wait_s"] == expected


# --- the summary --------------------------------------------------------------------------------------


def test_a_short_command_is_sent_whole(backend, env):
    b = backend(answer("timeout"))
    run(b, event(tool_input={"command": "git status --short"}), env)
    assert b.requests[0]["body"]["summary"] == "git status --short"


def test_a_long_command_is_cut_at_80_characters_and_says_how_long_it_was():
    command = "echo " + "x" * 120 + " && rm -rf important"
    out = hook.summarize("Bash", {"command": command})
    assert len(out) == 80 and out.startswith("echo xxxx") and out.endswith("...[" + str(len(command)) + "]")
    assert "rm -rf" not in out


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(" " * 5_000_000, id="spaces"),
        pytest.param("-" * 2_000_000, id="dashes"),
        pytest.param("a" * 3_000_000, id="letters"),
        pytest.param("token=" * 500_000, id="keys"),
        pytest.param("\n" * 3_000_000, id="newlines"),
        pytest.param("x " * 2_000_000, id="words"),
        pytest.param(("-----" + "BEGIN KEY----- ") * 100_000, id="pem-starts"),
    ],
)
def test_huge_or_pathological_commands_are_summarised_quickly(command):
    started = time.monotonic()
    out = hook.summarize("Bash", {"command": command})
    assert time.monotonic() - started < 3.0
    assert len(out) <= 80


def test_the_boundary_at_exactly_80_characters():
    assert hook.summarize("Bash", {"command": "x" * 80}) == "x" * 80
    assert len(hook.summarize("Bash", {"command": "x" * 81})) == 80


def test_line_breaks_stay_visible_so_a_hidden_second_line_is_noticed():
    out = hook.summarize("Bash", {"command": "ls\nrm -rf /tmp/x\n"})
    assert out == "ls \\n rm -rf /tmp/x"
    assert "\n" not in out


def test_powershell_is_summarised_like_bash():
    assert hook.summarize("PowerShell", {"command": "Get-ChildItem"}) == "Get-ChildItem"


@pytest.mark.parametrize(
    "tool,path,expected",
    [
        ("Write", "C:\\project\\src\\index.ts", "index.ts"),
        ("Edit", "/home/user/proj/app.py", "app.py"),
        ("MultiEdit", "C:/mixed\\separators/file.txt", "file.txt"),
        ("NotebookEdit", "/nb/analysis.ipynb", "analysis.ipynb"),
    ],
)
def test_file_tools_send_only_the_file_name(tool, path, expected):
    out = hook.summarize(
        tool, {"file_path": path, "content": "SECRET CONTENT", "old_string": "x", "new_string": "y"}
    )
    assert out == expected


def test_other_tools_send_no_details():
    for tool in ("Read", "Grep", "WebFetch", "mcp__server__tool", "Task"):
        assert hook.summarize(tool, {"url": "https://x.test/?token=abc123", "pattern": "p"}) == ""


@pytest.mark.parametrize("tool_input", [None, "ls", ["ls"], 5, {}, {"command": 5}, {"command": None}])
def test_odd_tool_input_gives_an_empty_summary(tool_input):
    assert hook.summarize("Bash", tool_input) == ""


def test_odd_tool_and_session_names_are_made_safe(backend, env):
    b = backend(answer("timeout"))
    run(b, event(tool="we ird/tool;name" + "x" * 60, session_id="sess 1;rm -rf"), env)
    body = b.requests[0]["body"]
    assert body["tool"] == ("we_ird_tool_name" + "x" * 60)[:40]
    assert body["session"] == "sess_1_rm_-rf"
    run(b, event(tool=None, session_id=None), env)
    assert b.requests[1]["body"]["tool"] == "tool" and b.requests[1]["body"]["session"] == "unknown"


SECRET_COMMANDS = [
    ("curl -H 'Authorization: Bearer abcdefghijklmnop1234' https://x.test", "abcdefghijklmnop1234"),
    ("curl -u admin:pa55w0rd https://x.test --password hunter2hunter2", "hunter2hunter2"),
    ("export API_KEY=abc123secretvalue && run", "abc123secretvalue"),
    ("deploy --token=ghTokenValue99", "ghTokenValue99"),
    ("git clone https://user:pa55w0rd@example.com/r.git", "pa55w0rd"),
    ("echo sk-proj-abcdefghijklmnopqrstuv", "abcdefghijklmnopqrstuv"),
    ("echo 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
    ("echo dGhpcyBpcyBhIGZha2UgYmFzZTY0IHNlY3JldCB2YWx1ZTEyMw", "dGhpcyBpcyBhIGZha2UgYmFzZTY0"),
    ("echo ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"),
    ("run secret='quoted secret value'", "quoted secret value"),
]


@pytest.mark.parametrize("command,secret", SECRET_COMMANDS, ids=[c[1][:16] for c in SECRET_COMMANDS])
def test_secrets_are_redacted_before_anything_is_sent(backend, env, command, secret):
    b = backend(answer("timeout"))
    run(b, event(tool_input={"command": command}), env)
    assert secret not in b.requests[0]["raw"].decode()
    assert secret not in hook.summarize("Bash", {"command": command})


def test_a_secret_straddling_the_cut_is_redacted_before_clipping():
    command = "x" * 70 + " token=SUPERSECRETVALUE123456 tail"
    out = hook.summarize("Bash", {"command": command})
    assert "SUPERSECRET" not in out and "SUPER" not in out


def test_ordinary_commands_are_not_mangled():
    for command in ("npm test", "pytest tests/claude -q", "git log --oneline -5", "ls -la /usr/local/bin"):
        assert hook.redact(command) == command


def test_the_token_never_reaches_stdout_or_the_payload(backend, env):
    b = backend(answer("allow"))
    out = run(b, event(tool_input={"command": f"echo {TOKEN}"}), env)
    assert TOKEN not in out and TOKEN not in b.requests[0]["raw"].decode()


# --- the command line ---------------------------------------------------------------------------------


def cli(args=(), stdin=b"", extra_env=None, timeout=30):
    base = {
        k: os.environ[k]
        for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")
        if k in os.environ
    }
    return subprocess.run(  # noqa: S603 - our own script with fixed arguments
        [sys.executable, str(SCRIPT_DIR / "oyster_hook.py"), *args],
        input=stdin,
        capture_output=True,
        env={**base, **(extra_env or {})},
        timeout=timeout,
        check=False,
    )


def test_cli_allow_end_to_end(backend, tmp_path):
    b = backend(answer("allow"))
    done = cli(stdin=event(), extra_env={"OYSTER_BACKEND": b.url, "OYSTER_ADMIN_TOKEN": TOKEN})
    assert done.returncode == 0 and done.stderr == b""
    assert json.loads(done.stdout) == ALLOW


def test_cli_prints_nothing_and_exits_zero_on_every_failure(backend, tmp_path):
    missing = str(tmp_path / "none.env")
    for stdin, cfg in (
        (b"garbage", {"OYSTER_BACKEND": "http://127.0.0.1:1", "OYSTER_ADMIN_TOKEN": TOKEN}),
        (event(), {"OYSTER_BACKEND": f"http://127.0.0.1:{closed_port()}", "OYSTER_ADMIN_TOKEN": TOKEN}),
        (event(), {"OYSTER_ENV_FILE": missing}),
        (b"", {"OYSTER_ENV_FILE": missing}),
    ):
        done = cli(stdin=stdin, extra_env=cfg)
        assert (done.returncode, done.stdout, done.stderr) == (0, b"", b""), cfg


def test_cli_selftest_passes():
    done = cli(["--selftest"])
    assert done.returncode == 0, done.stdout.decode()
    assert b"13/13 checks passed" in done.stdout and b"FAIL" not in done.stdout


def test_the_watchdog_ends_a_stuck_process_silently(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "_exit", lambda code: calls.append(code))
    hook._arm_watchdog(0.05)
    time.sleep(0.4)
    assert calls == [0]


# --- hygiene ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["oyster_hook.py", "oyster_statusline.py"])
def test_the_scripts_import_only_the_standard_library(name):
    tree = ast.parse((SCRIPT_DIR / name).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"oyster_hook"}  # the status script imports its sibling
    assert imported <= allowed, imported - allowed


@pytest.mark.parametrize("name", ["oyster_hook.py", "oyster_statusline.py"])
def test_the_scripts_do_not_log_or_print_from_the_hook_path(name):
    source = (SCRIPT_DIR / name).read_text(encoding="utf-8")
    assert "logging" not in source and "traceback" not in source
    assert "print(" not in source.split("def selftest")[0]
