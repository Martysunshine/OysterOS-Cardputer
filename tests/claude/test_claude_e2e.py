"""The real hook script against the real app served by uvicorn on an ephemeral loopback port (never the
LAN): a device answer ends the long poll, silence means the normal prompt, and a killed hook cancels its
approval. The device side is driven through the same app object."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from _claude_support import ADMIN, SCRIPT_DIR
from oyster.claude.service import ClaudeConfig

ALLOW = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}


@pytest.fixture
def live(kit_factory):
    kit = kit_factory(ClaudeConfig(poll_interval_s=0.02), real_time=True)
    kit.dev_a = kit.enroll("dev-a")
    server = uvicorn.Server(
        uvicorn.Config(kit.app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(1500):
        if server.started and server.servers:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("uvicorn did not start")
    kit.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    yield kit
    kit.runtime.stop()
    server.should_exit = True
    thread.join(15)


def start_hook(kit, command="npm test", wait_s=30, **env_extra) -> subprocess.Popen:
    event = {
        "session_id": "sess-live",
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    base = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE") if k in os.environ}
    proc = subprocess.Popen(  # noqa: S603 - our own script with fixed arguments
        [sys.executable, str(SCRIPT_DIR / "oyster_hook.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **base,
            "OYSTER_BACKEND": kit.url,
            "OYSTER_ADMIN_TOKEN": ADMIN,
            "OYSTER_WAIT_S": str(wait_s),
            **env_extra,
        },
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(event).encode())
    proc.stdin.close()
    return proc


def wait_for_listing(kit, tries=3000):
    for _ in range(tries):
        items = kit.listing(kit.dev_a)
        if items:
            return items[0]
        time.sleep(0.01)
    raise AssertionError("the approval never appeared")


def finish(proc: subprocess.Popen, timeout=20):
    out, err = proc.communicate(timeout=timeout)
    return proc.returncode, out, err


def test_device_allow_reaches_the_hook_process(live):
    proc = start_hook(live, "git status")
    item = wait_for_listing(live)
    assert item["summary"] == "git status" and item["tool"] == "Bash"
    assert live.decide(live.dev_a, item["approval_id"], item["nonce"], "allow").status_code == 200
    code, out, err = finish(proc)
    assert (code, err) == (0, b"")
    assert json.loads(out) == ALLOW


def test_device_deny_reaches_the_hook_process(live):
    proc = start_hook(live)
    item = wait_for_listing(live)
    live.decide(live.dev_a, item["approval_id"], item["nonce"], "deny")
    code, out, _ = finish(proc)
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_no_answer_means_no_output_and_the_normal_prompt(live):
    proc = start_hook(live, wait_s=1)
    code, out, err = finish(proc)
    assert (code, out, err) == (0, b"", b"")
    assert live.listing(live.dev_a) == []
    assert live.service.history(limit=10)[0][0].event == "approval.expired"


def test_a_secret_in_the_command_never_reaches_the_backend(live):
    proc = start_hook(live, "curl -H 'Authorization: Bearer abcdefghijklmnop1234' https://x.test", wait_s=1)
    item = wait_for_listing(live)
    assert "abcdefghijklmnop1234" not in json.dumps(item)
    finish(proc)
    with live.runtime.store.tx() as conn:
        stored = str(conn.execute("SELECT summary FROM approvals").fetchall())
    assert "abcdefghijklmnop1234" not in stored


def test_a_killed_hook_cancels_its_approval(live):
    proc = start_hook(live, wait_s=60)
    item = wait_for_listing(live)
    proc.kill()
    finish(proc)
    for _ in range(1500):
        if live.service.poll(item["approval_id"]) == "cancelled":
            break
        time.sleep(0.02)
    assert live.service.poll(item["approval_id"]) == "cancelled"
    assert live.listing(live.dev_a) == []
    late = live.decide(live.dev_a, item["approval_id"], item["nonce"], "allow")
    assert late.status_code == 409
    assert live.events(item["approval_id"]) == ["approval.created", "approval.cancelled", "request.denied"]


def test_two_hooks_wait_independently(live):
    first = start_hook(live, "echo first")
    wait_for_listing(live)
    second = start_hook(live, "echo second")
    for _ in range(3000):
        items = live.listing(live.dev_a)
        if len(items) == 2:
            break
        time.sleep(0.01)
    by_summary = {i["summary"]: i for i in items}
    live.decide(
        live.dev_a, by_summary["echo second"]["approval_id"], by_summary["echo second"]["nonce"], "allow"
    )
    code2, out2, _ = finish(second)
    assert code2 == 0 and json.loads(out2) == ALLOW
    assert first.poll() is None  # still waiting
    live.decide(
        live.dev_a, by_summary["echo first"]["approval_id"], by_summary["echo first"]["nonce"], "deny"
    )
    code1, out1, _ = finish(first)
    assert code1 == 0 and json.loads(out1)["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_stopping_the_backend_releases_a_waiting_hook(live):
    proc = start_hook(live, wait_s=60)
    wait_for_listing(live)
    live.runtime.stop()
    code, out, _ = finish(proc, timeout=10)
    assert (code, out) == (0, b"")
