"""The approval state machine at the service level: create, list, decide, expiry, nonce binding, replay,
cancellation, capacity, restart and the database-level guards. A fake clock drives every deadline."""

from __future__ import annotations

import itertools
import sqlite3

import pytest
from _claude_support import Clock
from oyster.claude.mount import ClaudeRuntime
from oyster.claude.service import ClaudeConfig, ClaudeError, device_text


@pytest.fixture
def make(tmp_path):
    made: list[ClaudeRuntime] = []

    def build(
        config: ClaudeConfig | None = None, clock: Clock | None = None, sub: str = "svc"
    ) -> ClaudeRuntime:
        clk = clock or Clock()
        ids, nonces = itertools.count(1), itertools.count(1)
        rt = ClaudeRuntime(
            tmp_path / sub,
            clock=clk,
            config=config or ClaudeConfig(),
            new_id=lambda: f"appr-{next(ids):016x}",
            new_nonce=lambda: f"nonce-{next(nonces):08d}-abcdef",
            synchronous="OFF",
        )
        rt.clk = clk  # type: ignore[attr-defined]
        made.append(rt)
        return rt

    yield build
    for rt in made:
        rt.close()


@pytest.fixture
def rt(make):
    return make()


def states(rt: ClaudeRuntime) -> dict[str, str]:
    with rt.store.tx() as conn:
        return dict(conn.execute("SELECT approval_id, state FROM approvals").fetchall())


def listed(rt, device="dev-a"):
    return rt.service.list_for_device(device)


def test_create_is_pending_with_a_window_and_an_audit_row(rt):
    a = rt.service.create("s1", "Bash", "npm test", 90)
    assert a.state == "pending" and a.approval_id.startswith("appr-")
    assert a.expires_at == a.created_at + 90 and a.decided_by is None
    assert states(rt) == {a.approval_id: "pending"}
    assert [e.event for e in rt.service.history()[0]] == ["approval.created"]


def test_list_is_newest_first_capped_at_five_and_carries_a_nonce(make):
    rt = make(ClaudeConfig(max_pending=20))
    ids = [rt.service.create("s", "Bash", f"cmd {i}", 90).approval_id for i in range(7)]
    items = listed(rt)
    assert [i["approval_id"] for i in items] == ids[::-1][:5]
    assert all(i["nonce"] for i in items) and len({i["nonce"] for i in items}) == 5
    assert items[0]["summary"] == "cmd 6" and items[0]["tool"] == "Bash"
    assert set(items[0]) == {"approval_id", "tool", "summary", "age_s", "expires_in_s", "nonce"}


def test_the_nonce_is_per_device_and_stable_across_lists(rt):
    rt.service.create("s", "Bash", "ls", 90)
    a1, a2, b1 = listed(rt, "dev-a")[0], listed(rt, "dev-a")[0], listed(rt, "dev-b")[0]
    assert a1["nonce"] == a2["nonce"]
    assert a1["nonce"] != b1["nonce"]


def test_age_and_remaining_time_follow_the_clock(rt):
    rt.service.create("s", "Bash", "ls", 90)
    rt.clk.advance(25.5)
    item = listed(rt)[0]
    assert item["age_s"] == 25 and item["expires_in_s"] == 65


def test_a_device_allow_is_recorded_with_the_device(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    item = listed(rt)[0]
    done = rt.service.decide("dev-a", a.approval_id, "allow", item["nonce"])
    assert done.state == "allowed" and done.decided_by == "dev-a"
    assert states(rt)[a.approval_id] == "allowed"
    assert listed(rt) == []
    ev = rt.service.history()[0]
    assert [e.event for e in ev] == ["approval.allowed", "approval.created"]
    assert ev[0].actor_kind == "device" and ev[0].actor_id == "dev-a" and ev[0].device_id == "dev-a"


def test_a_device_deny_is_recorded(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    done = rt.service.decide("dev-a", a.approval_id, "deny", listed(rt)[0]["nonce"])
    assert done.state == "denied" and done.decided_by == "dev-a"


def test_only_one_decision_is_ever_accepted(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    nonce = listed(rt)[0]["nonce"]
    rt.service.decide("dev-a", a.approval_id, "deny", nonce)
    for decision in ("deny", "allow"):  # the same answer again, and the opposite one
        with pytest.raises(ClaudeError) as e:
            rt.service.decide("dev-a", a.approval_id, decision, nonce)
        assert (e.value.status, e.value.code) == (409, "already_decided")
    assert states(rt)[a.approval_id] == "denied"


def test_a_second_device_cannot_overturn_the_first_answer(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    nonce_a, nonce_b = listed(rt, "dev-a")[0]["nonce"], listed(rt, "dev-b")[0]["nonce"]
    rt.service.decide("dev-a", a.approval_id, "deny", nonce_a)
    with pytest.raises(ClaudeError) as e:
        rt.service.decide("dev-b", a.approval_id, "allow", nonce_b)
    assert e.value.status == 409
    assert states(rt)[a.approval_id] == "denied"


def test_the_nonce_is_cleared_once_decided(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    rt.service.decide("dev-a", a.approval_id, "allow", listed(rt)[0]["nonce"])
    with rt.store.tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == 0


def test_a_wrong_nonce_is_refused_and_the_approval_stays_decidable(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    good = listed(rt)[0]["nonce"]
    for bad in ("nonce-00000000-wrong", "x" * 8, good[:-1] + ("a" if good[-1] != "a" else "b")):
        with pytest.raises(ClaudeError) as e:
            rt.service.decide("dev-a", a.approval_id, "allow", bad)
        assert (e.value.status, e.value.code) == (403, "forbidden")
        assert bad not in e.value.detail
    assert states(rt)[a.approval_id] == "pending"
    assert rt.service.decide("dev-a", a.approval_id, "allow", good).state == "allowed"


def test_a_device_that_never_listed_the_approval_cannot_decide_it(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    for nonce in ("nonce-00000001-abcdef", "anything-at-all"):
        with pytest.raises(ClaudeError) as e:
            rt.service.decide("dev-b", a.approval_id, "allow", nonce)
        assert e.value.status == 403
    assert states(rt)[a.approval_id] == "pending"


def test_another_devices_nonce_does_not_work(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    nonce_a = listed(rt, "dev-a")[0]["nonce"]
    listed(rt, "dev-b")  # dev-b has its own nonce now
    with pytest.raises(ClaudeError) as e:
        rt.service.decide("dev-b", a.approval_id, "allow", nonce_a)
    assert e.value.status == 403
    assert states(rt)[a.approval_id] == "pending"


def test_a_nonce_is_bound_to_its_own_approval(rt):
    a1 = rt.service.create("s", "Bash", "one", 90)
    a2 = rt.service.create("s", "Bash", "two", 90)
    nonces = {i["approval_id"]: i["nonce"] for i in listed(rt)}
    with pytest.raises(ClaudeError) as e:
        rt.service.decide("dev-a", a1.approval_id, "allow", nonces[a2.approval_id])
    assert e.value.status == 403
    assert states(rt) == {a1.approval_id: "pending", a2.approval_id: "pending"}


def test_unknown_and_malformed_ids_are_404(rt):
    for approval_id in ("appr-ffffffffffffffff", "appr-short", "../etc/passwd", "x" * 300):
        with pytest.raises(ClaudeError) as e:
            rt.service.decide("dev-a", approval_id, "allow", "nonce-00000001-abcdef")
        assert (e.value.status, e.value.code, e.value.detail) == (404, "not_found", "unknown id")


def test_the_window_closes_exactly_at_the_deadline(rt):
    a = rt.service.create("s", "Bash", "ls", 30)
    nonce = listed(rt)[0]["nonce"]
    rt.clk.advance(29.9)
    assert [i["approval_id"] for i in listed(rt)] == [a.approval_id]
    rt.clk.advance(0.1)
    assert listed(rt) == []
    with pytest.raises(ClaudeError) as e:
        rt.service.decide("dev-a", a.approval_id, "allow", nonce)
    assert (e.value.status, e.value.code) == (409, "expired")
    assert states(rt)[a.approval_id] == "expired"


def test_a_late_decision_cannot_resurrect_an_expired_approval(rt):
    a = rt.service.create("s", "Bash", "ls", 30)
    nonce = listed(rt)[0]["nonce"]
    rt.clk.advance(31)
    for _ in range(2):
        with pytest.raises(ClaudeError):
            rt.service.decide("dev-a", a.approval_id, "allow", nonce)
    assert rt.service.poll(a.approval_id) == "expired"


def test_expiry_and_refusals_are_audited(rt):
    a = rt.service.create("s", "Bash", "ls", 30)
    nonce = listed(rt)[0]["nonce"]
    rt.clk.advance(31)
    with pytest.raises(ClaudeError):
        rt.service.decide("dev-a", a.approval_id, "allow", nonce)
    events = [(e.event, e.outcome, e.reason) for e in reversed(rt.service.history()[0])]
    assert events == [
        ("approval.created", "pending", None),
        ("approval.expired", "expired", "timeout"),
        ("request.denied", "refused", "expired"),
    ]


def test_cancel_is_a_one_way_move_and_blocks_a_later_decision(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    nonce = listed(rt)[0]["nonce"]
    assert rt.service.cancel(a.approval_id) is True
    assert rt.service.cancel(a.approval_id) is False
    assert rt.service.cancel("appr-ffffffffffffffff") is False
    assert listed(rt) == []
    with pytest.raises(ClaudeError) as e:
        rt.service.decide("dev-a", a.approval_id, "allow", nonce)
    assert e.value.status == 409
    assert states(rt)[a.approval_id] == "cancelled"


def test_poll_reports_each_outcome(rt):
    a1, a2, a3 = (rt.service.create("s", "Bash", c, 90) for c in "abc")
    items = {i["approval_id"]: i["nonce"] for i in listed(rt)}
    rt.service.decide("dev-a", a1.approval_id, "allow", items[a1.approval_id])
    rt.service.decide("dev-a", a2.approval_id, "deny", items[a2.approval_id])
    assert [rt.service.poll(a.approval_id) for a in (a1, a2, a3)] == ["allowed", "denied", "pending"]
    assert rt.service.poll("appr-ffffffffffffffff") is None


def test_capacity_is_bounded_and_freed_by_expiry(make):
    rt = make(ClaudeConfig(max_pending=3))
    for i in range(3):
        rt.service.create("s", "Bash", f"c{i}", 30)
    with pytest.raises(ClaudeError) as e:
        rt.service.create("s", "Bash", "one too many", 30)
    assert (e.value.status, e.value.code) == (503, "busy") and e.value.headers == {"Retry-After": "5"}
    rt.clk.advance(31)
    assert rt.service.create("s", "Bash", "fits again", 30).state == "pending"


def test_finished_approvals_are_pruned_but_the_audit_trail_stays(make):
    rt = make(ClaudeConfig(retention_s=1000))
    a = rt.service.create("s", "Bash", "old", 5)
    rt.clk.advance(2000)
    rt.service.create("s", "Bash", "new", 5)  # sweeps the old one, then prunes it
    assert a.approval_id not in states(rt)
    assert any(e.approval_id == a.approval_id for e in rt.service.history(limit=50)[0])


def test_restart_ends_pending_approvals_and_keeps_decided_ones(make):
    clock = Clock()
    rt1 = make(clock=clock, sub="same")
    keep = rt1.service.create("s", "Bash", "decided", 90)
    waiting = rt1.service.create("s", "Bash", "waiting", 90)
    rt1.service.decide(
        "dev-a",
        keep.approval_id,
        "allow",
        {i["approval_id"]: i["nonce"] for i in listed(rt1)}[keep.approval_id],
    )
    nonce = {i["approval_id"]: i["nonce"] for i in listed(rt1)}[waiting.approval_id]
    rt1.close()

    rt2 = make(clock=clock, sub="same")
    assert rt2.recovered == 1
    assert states(rt2) == {keep.approval_id: "allowed", waiting.approval_id: "expired"}
    assert listed(rt2) == []
    with pytest.raises(ClaudeError) as e:  # an answer to a request nobody waits for is refused
        rt2.service.decide("dev-a", waiting.approval_id, "allow", nonce)
    assert e.value.status == 409
    reasons = {e.reason for e in rt2.service.history(limit=50)[0] if e.event == "approval.expired"}
    assert reasons == {"backend_restarted"}


def test_stop_ends_pending_approvals(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    rt.start()
    rt.stop()
    assert states(rt)[a.approval_id] == "expired"
    assert rt.service.poll(a.approval_id) == "expired"


def test_the_schema_refuses_an_allow_without_a_deciding_device(rt):
    a = rt.service.create("s", "Bash", "ls", 90)
    with rt.store.tx() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE approvals SET state='allowed' WHERE approval_id=?", (a.approval_id,))
    with rt.store.tx() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE approvals SET decided_by='dev-a' WHERE approval_id=?", (a.approval_id,))
    with rt.store.tx() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE approvals SET state='maybe' WHERE approval_id=?", (a.approval_id,))
    assert states(rt)[a.approval_id] == "pending"


def test_the_database_refuses_to_modify_or_remove_audit_rows(rt):
    rt.service.create("s", "Bash", "ls", 90)
    with rt.store.tx() as conn, pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit SET outcome='allowed'")
    with rt.store.tx() as conn, pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit")
    assert len(rt.service.history()[0]) == 1


def test_store_pragmas_and_idempotent_creation(tmp_path):
    rt = ClaudeRuntime(tmp_path / "p")  # defaults: synchronous FULL, WAL
    try:
        assert rt.store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert rt.store._db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        again = ClaudeRuntime(tmp_path / "p")  # opening the same file twice creates nothing twice
        again.close()
        assert (tmp_path / "p" / "claude.db").is_file()
    finally:
        rt.close()


def test_a_full_device_list_stays_under_4_kib(make):
    rt = make(ClaudeConfig(max_pending=20))
    for i in range(6):
        rt.service.create("s" * 64, "mcp__" + "t" * 35, ("x" * 150) + str(i), 90)
    import json

    body = json.dumps({"approvals": listed(rt)})
    assert len(listed(rt)) == 5 and len(body) < 4096
    assert all(len(i["summary"]) <= 80 for i in listed(rt))


def test_device_text_is_printable_ascii_and_clipped_with_a_marker():
    assert device_text("café 中") == "caf? ?"
    long = device_text("a" * 200)
    assert len(long) == 80 and long.endswith("...")
    assert device_text("short") == "short"
