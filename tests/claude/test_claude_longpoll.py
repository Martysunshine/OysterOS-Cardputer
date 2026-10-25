"""The hook's long poll: several waiters at once, a short fake clock, cancellation when the hook goes away,
shutdown, capacity, and the rule that "allow" only ever follows a recorded device decision."""

from __future__ import annotations

import asyncio
import json

import pytest
from _claude_support import ADMIN
from oyster.claude.mount import ClaudeRuntime
from oyster.claude.service import ClaudeConfig


def test_concurrent_hooks_get_their_own_answers(kit):
    first = kit.hook(summary="first", session="sess-a")
    kit.wait_for_pending(1)
    second = kit.hook(summary="second", session="sess-b")
    kit.wait_for_pending(2)
    items = {i["summary"]: i for i in kit.listing(kit.dev_a)}
    assert (
        kit.decide(kit.dev_a, items["second"]["approval_id"], items["second"]["nonce"], "deny").status_code
        == 200
    )
    assert (
        kit.decide(kit.dev_a, items["first"]["approval_id"], items["first"]["nonce"], "allow").status_code
        == 200
    )
    assert first.result().json() == {"decision": "allow", "approval_id": items["first"]["approval_id"]}
    assert second.result().json() == {"decision": "deny", "approval_id": items["second"]["approval_id"]}


def test_a_waiter_ends_at_its_own_deadline_not_another_ones(kit):
    short = kit.hook(summary="short", wait_s=10)
    kit.wait_for_pending(1)
    long = kit.hook(summary="long", wait_s=100)
    kit.wait_for_pending(2)
    kit.clock.advance(11)
    assert short.result().json()["decision"] == "timeout"
    assert [i["summary"] for i in kit.listing(kit.dev_a)] == ["long"]
    kit.clock.advance(100)
    assert long.result().json()["decision"] == "timeout"


def test_timeout_is_the_answer_for_every_non_decision_ending(kit):
    w = kit.hook(wait_s=20)
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    kit.service.cancel(item["approval_id"], "test")  # e.g. the owner killed the hook elsewhere
    out = w.result().json()
    assert out["decision"] == "timeout" and out["approval_id"] == item["approval_id"]


def test_the_backend_stopping_ends_waiters_with_timeout(kit):
    w = kit.hook()
    kit.wait_for_pending()
    kit.runtime.stop()
    assert w.result().json()["decision"] == "timeout"
    assert kit.listing(kit.dev_a) == []
    assert "approval.expired" in kit.events()


def test_too_many_waiting_approvals_are_refused_not_queued(kit_factory):
    kit = kit_factory(ClaudeConfig(max_pending=2, poll_interval_s=0.01, creates_per_minute=100))
    dev = kit.enroll("dev-a")
    waiters = [kit.hook(summary=f"c{i}") for i in range(2)]
    kit.wait_for_pending(2)
    r = kit.client.post("/v1/claude/approvals", headers=kit.owner, json=kit.body(summary="third"))
    assert r.status_code == 503 and r.json()["error"] == "busy" and r.headers["Retry-After"] == "5"
    item = kit.pending_item(dev)
    kit.decide(dev, item["approval_id"], item["nonce"], "deny")  # frees a slot
    assert kit.client.get("/v1/claude/approvals", headers=dev).status_code == 200
    kit.clock.advance(200)
    for w in waiters:
        w.result()


def test_allow_is_only_returned_for_a_recorded_device_decision(kit):
    """Every long poll that did not receive a device 'allow' returns something other than allow."""
    outcomes = []
    for i in range(3):
        w = kit.hook(summary=f"c{i}", wait_s=10)
        kit.wait_for_pending()
        if i == 1:  # cancelled out from under the waiter
            kit.service.cancel(kit.pending_item(kit.dev_a)["approval_id"])
        elif i == 2:  # wrong nonce, then the window closes
            item = kit.pending_item(kit.dev_a)
            assert kit.decide(kit.dev_a, item["approval_id"], "not-the-nonce-0", "allow").status_code == 403
            kit.clock.advance(11)
        else:
            kit.clock.advance(11)
        outcomes.append(w.result().json()["decision"])
    assert outcomes == ["timeout", "timeout", "timeout"]
    assert "approval.allowed" not in kit.events()


def test_the_decided_state_in_the_database_is_the_only_source_of_allow(kit):
    w = kit.hook()
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    kit.decide(kit.dev_a, item["approval_id"], item["nonce"], "allow")
    assert w.result().json()["decision"] == "allow"
    with kit.runtime.store.tx() as conn:
        state, decided_by = conn.execute(
            "SELECT state, decided_by FROM approvals WHERE approval_id=?", (item["approval_id"],)
        ).fetchone()
    assert (state, decided_by) == ("allowed", "dev-a")


# --- the hook disconnecting ---------------------------------------------------------------------------


async def _asgi_post(app, path: str, body: dict, headers: dict, disconnect: asyncio.Event):
    """Drive the app by hand: send one request, keep the connection open, then 'close' it on demand."""
    raw = json.dumps(body).encode()
    sent = {"request": False}
    out: list[dict] = []

    async def receive():
        if not sent["request"]:
            sent["request"] = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        out.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in {**headers, "content-length": str(len(raw))}.items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
        "state": {},
    }
    await app(scope, receive, send)
    return out


async def _wait_for(predicate, tries: int = 500):
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


async def test_a_hook_that_disconnects_cancels_its_approval(kit):
    disconnect = asyncio.Event()
    task = asyncio.create_task(
        _asgi_post(
            kit.app, "/v1/claude/approvals", kit.body(), {"authorization": f"Bearer {ADMIN}"}, disconnect
        )
    )
    await _wait_for(lambda: kit.service.poll("appr-0000000000000001") == "pending")
    assert [i["approval_id"] for i in kit.service.list_for_device("dev-a")] == ["appr-0000000000000001"]
    disconnect.set()
    await asyncio.wait_for(task, 5)
    assert kit.service.poll("appr-0000000000000001") == "cancelled"
    assert kit.service.list_for_device("dev-a") == []
    assert kit.events("appr-0000000000000001") == ["approval.created", "approval.cancelled"]
    reasons = [
        r[0]
        for r in kit.runtime.store._db.execute("SELECT reason FROM audit WHERE event='approval.cancelled'")
    ]
    assert reasons == ["hook_disconnected"]


async def test_a_late_device_answer_after_the_hook_left_is_refused(kit):
    disconnect = asyncio.Event()
    task = asyncio.create_task(
        _asgi_post(
            kit.app, "/v1/claude/approvals", kit.body(), {"authorization": f"Bearer {ADMIN}"}, disconnect
        )
    )
    await _wait_for(lambda: kit.service.poll("appr-0000000000000001") == "pending")
    nonce = kit.service.list_for_device("dev-a")[0]["nonce"]
    disconnect.set()
    await asyncio.wait_for(task, 5)
    with pytest.raises(Exception) as e:
        kit.service.decide("dev-a", "appr-0000000000000001", "allow", nonce)
    assert getattr(e.value, "status", None) == 409


async def test_the_long_poll_answers_normally_when_the_hook_stays(kit):
    stay = asyncio.Event()  # never set: the hook does not disconnect
    task = asyncio.create_task(
        _asgi_post(kit.app, "/v1/claude/approvals", kit.body(), {"authorization": f"Bearer {ADMIN}"}, stay)
    )
    await _wait_for(lambda: kit.service.poll("appr-0000000000000001") == "pending")
    item = kit.service.list_for_device("dev-a")[0]
    kit.service.decide("dev-a", item["approval_id"], "allow", item["nonce"])
    messages = await asyncio.wait_for(task, 5)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert start["status"] == 200
    assert json.loads(body) == {"decision": "allow", "approval_id": "appr-0000000000000001"}


async def test_cancelling_the_serving_task_cancels_the_approval(kit):
    disconnect = asyncio.Event()
    task = asyncio.create_task(
        _asgi_post(
            kit.app, "/v1/claude/approvals", kit.body(), {"authorization": f"Bearer {ADMIN}"}, disconnect
        )
    )
    await _wait_for(lambda: kit.service.poll("appr-0000000000000001") == "pending")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for(lambda: kit.service.poll("appr-0000000000000001") == "cancelled")


async def test_wait_decision_returns_the_documented_words(tmp_path):
    clock = {"t": 1000.0}
    ids = iter(f"appr-{i:016x}" for i in range(1, 99))
    rt = ClaudeRuntime(
        tmp_path / "w",
        clock=lambda: clock["t"],
        config=ClaudeConfig(poll_interval_s=0.001),
        new_id=lambda: next(ids),
        new_nonce=lambda: "nonce-fixed-0001",
        synchronous="OFF",
    )
    try:
        svc = rt.service
        a = svc.create("s", "Bash", "x", 5)
        task = asyncio.create_task(svc.wait_decision(a.approval_id))
        await asyncio.sleep(0.02)
        clock["t"] += 6
        assert await asyncio.wait_for(task, 5) == "timeout"

        b = svc.create("s", "Bash", "y", 5)

        async def gone() -> bool:
            return True

        assert await svc.wait_decision(b.approval_id, gone) == "disconnected"
        assert svc.poll(b.approval_id) == "cancelled"
        assert await svc.wait_decision("appr-ffffffffffffffff") == "timeout"
    finally:
        rt.close()
