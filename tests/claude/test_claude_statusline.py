"""integrations/claude-code/oyster_statusline.py: the printed line, the best-effort report, and the rule that
nothing Claude Code did not send is invented."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from _claude_support import SCRIPT_DIR, FakeBackend, closed_port, load_script

load_script("oyster_hook")  # the status script imports its sibling
sl = load_script("oyster_statusline")

TOKEN = "fake-owner-token-0123456789abcdef"

FULL = {
    "session_id": "sess-1",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
    "cost": {"total_cost_usd": 0.01234},
    "context_window": {"used_percentage": 8},
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": 1},
        "seven_day": {"used_percentage": 41.2},
    },
}


@pytest.fixture
def backend():
    made: list[FakeBackend] = []

    def make(reply=None) -> FakeBackend:
        b = FakeBackend(reply or (lambda path, body: (200, {"stored": True})))
        made.append(b)
        return b

    yield make
    for b in made:
        b.close()


@pytest.fixture
def env(tmp_path):
    return {"OYSTER_ADMIN_TOKEN": TOKEN, "OYSTER_ENV_FILE": str(tmp_path / "no-such.env")}


def feed(data, env, b=None) -> str:
    """The printed line. Without a fake backend nothing is posted: tests never touch a real port."""
    if b is None:
        return sl.handle(json.dumps(data).encode(), dict(env), lambda *args: (200, b""))
    return sl.handle(json.dumps(data).encode(), {**env, "OYSTER_BACKEND": b.url})


def test_the_documented_example_prints_one_short_line(backend, env):
    assert feed(FULL, env, backend()) == "Opus | ctx 8% | $0.01 | 5h 24% 7d 41%"


def test_the_report_carries_exactly_what_claude_code_sent(backend, env):
    b = backend()
    feed(FULL, env, b)
    (req,) = b.requests
    assert req["path"] == "/v1/claude/status" and req["auth"] == f"Bearer {TOKEN}"
    assert req["body"] == {
        "session": "sess-1",
        "model": "Opus",
        "cost_usd": 0.01234,
        "context_pct": 8.0,
        "five_hour_pct": 23.5,
        "seven_day_pct": 41.2,
    }
    assert TOKEN not in req["raw"].decode()


def test_absent_values_are_left_out_not_invented(backend, env):
    b = backend()
    data = {"session_id": "s", "model": {"display_name": "Sonnet"}, "cost": {"total_cost_usd": 2}}
    assert feed(data, env, b) == "Sonnet | $2.00"
    assert b.requests[0]["body"] == {"session": "s", "model": "Sonnet", "cost_usd": 2.0}


def test_null_percentages_early_in_a_session_are_left_out(backend, env):
    b = backend()
    data = {**FULL, "context_window": {"used_percentage": None}, "rate_limits": {"five_hour": {}}}
    assert feed(data, env, b) == "Opus | $0.01"
    assert set(b.requests[0]["body"]) == {"session", "model", "cost_usd"}


def test_without_a_cost_nothing_is_posted(backend, env):
    b = backend()
    data = {"session_id": "s", "model": {"display_name": "Opus"}, "context_window": {"used_percentage": 3}}
    assert feed(data, env, b) == "Opus | ctx 3%"
    assert b.requests == []


def test_without_a_session_nothing_is_posted(backend, env):
    b = backend()
    data = {k: v for k, v in FULL.items() if k != "session_id"}
    assert feed(data, env, b).startswith("Opus")
    assert b.requests == []


def test_the_model_id_is_the_fallback_name(backend, env):
    data = {**FULL, "model": {"id": "claude-x-1"}}
    assert feed(data, env).startswith("claude-x-1 |")
    assert feed({k: v for k, v in FULL.items() if k != "model"}, env).startswith("claude |")


@pytest.mark.parametrize("bad", [-1, 101, float("inf"), float("nan"), True, "50", None, [], {}])
def test_out_of_range_or_non_numeric_values_are_ignored(env, bad):
    data = {**FULL, "context_window": {"used_percentage": bad}}
    assert "ctx" not in feed(data, env)


def test_cost_must_be_a_sane_number(env):
    for bad in (-0.5, 1e12, "3", True, None):
        assert "$" not in feed({**FULL, "cost": {"total_cost_usd": bad}}, env)


def test_the_model_name_is_ascii_and_short(backend, env):
    b = backend()
    out = feed({**FULL, "model": {"display_name": "Opusé " + "x" * 80}}, env, b)
    assert out.isascii() and len(b.requests[0]["body"]["model"]) <= 40
    assert b.requests[0]["body"]["model"].startswith("Opus? xxx")


def test_the_session_id_is_made_safe(backend, env):
    b = backend()
    feed({**FULL, "session_id": "a b/c;" + "z" * 100}, env, b)
    assert b.requests[0]["body"]["session"] == ("a_b_c_" + "z" * 100)[:64]


def test_the_line_is_printed_even_when_the_backend_is_down(env):
    cfg = {**env, "OYSTER_BACKEND": f"http://127.0.0.1:{closed_port()}"}
    assert sl.handle(json.dumps(FULL).encode(), cfg).startswith("Opus | ctx 8%")


def test_the_line_is_printed_when_the_backend_errors_or_is_not_configured(backend, tmp_path):
    b = backend(lambda path, body: (500, b"boom"))
    cfg = {"OYSTER_BACKEND": b.url, "OYSTER_ADMIN_TOKEN": TOKEN}
    assert sl.handle(json.dumps(FULL).encode(), cfg).startswith("Opus")
    none = {"OYSTER_BACKEND": b.url, "OYSTER_ENV_FILE": str(tmp_path / "missing.env")}
    n = len(b.requests)
    assert sl.handle(json.dumps(FULL).encode(), none).startswith("Opus")
    assert len(b.requests) == n  # no token, no request


def test_a_non_loopback_backend_is_never_contacted(env):
    calls = []
    cfg = {**env, "OYSTER_BACKEND": "http://192.168.1.50:8000"}
    out = sl.handle(json.dumps(FULL).encode(), cfg, lambda *a: calls.append(a))
    assert out.startswith("Opus") and calls == []


def test_a_slow_backend_is_cut_off_after_the_post_timeout(env):
    seen = []

    def poster(base, path, token, payload, timeout):
        seen.append(timeout)
        raise TimeoutError

    assert sl.handle(json.dumps(FULL).encode(), {**env, "OYSTER_BACKEND": "http://127.0.0.1:1"}, poster)
    assert seen == [1.0]


@pytest.mark.parametrize("raw", [b"", b"nope", b"[]", b"null", b"42", b"\xff"])
def test_garbage_input_prints_nothing(env, raw):
    assert sl.handle(raw, env) == ""


def test_the_cli_prints_the_line_and_exits_zero(backend, tmp_path):
    b = backend()
    base = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE") if k in os.environ}
    done = subprocess.run(  # noqa: S603 - our own script with fixed arguments
        [sys.executable, str(SCRIPT_DIR / "oyster_statusline.py")],
        input=json.dumps(FULL).encode(),
        capture_output=True,
        env={**base, "OYSTER_BACKEND": b.url, "OYSTER_ADMIN_TOKEN": TOKEN},
        timeout=30,
        check=False,
    )
    assert done.returncode == 0 and done.stderr == b""
    assert done.stdout.decode().strip() == "Opus | ctx 8% | $0.01 | 5h 24% 7d 41%"
    assert done.stdout.isascii() and len(b.requests) == 1


def test_the_cli_survives_garbage(tmp_path):
    base = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE") if k in os.environ}
    done = subprocess.run(  # noqa: S603 - our own script with fixed arguments
        [sys.executable, str(SCRIPT_DIR / "oyster_statusline.py")],
        input=b"\x00\xff garbage",
        capture_output=True,
        env={**base, "OYSTER_ENV_FILE": str(tmp_path / "none.env")},
        timeout=30,
        check=False,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
