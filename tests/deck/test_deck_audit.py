"""Audit trail and history: append-only, complete, free of secrets, correctly scoped and paginated."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from oyster.deck import audit

AUDIT_SOURCE = Path(audit.__file__)


def test_audit_module_has_no_modifying_statements():
    text = AUDIT_SOURCE.read_text(encoding="utf-8")
    statements = (
        r"\bupdate\s+\w+\s+set\b|\bdelete\s+from\b|\bdrop\s+\w+|\breplace\s+into\b|\bor\s+replace\b|"
        r"\balter\s+table\b|\btruncate\b"
    )
    assert not re.search(statements, text, re.I)
    assert "INSERT INTO audit" in text and "SELECT" in text


def test_database_refuses_to_modify_or_remove_audit_rows(kit):
    kit.run(kit.dev_a, 1, "key-audit-0001")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), kit.runtime.store.tx() as conn:
        conn.execute("UPDATE audit SET outcome='ok'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), kit.runtime.store.tx() as conn:
        conn.execute("DELETE FROM audit")
    assert len(kit.audit_rows()) >= 2


def test_every_state_change_writes_one_audit_entry(kit):
    ok = kit.run_confirmed(kit.dev_a, 3, "key-audit-0002")
    assert kit.events(ok["job_id"]) == [
        "job.queued",
        "job.awaiting_confirmation",
        "job.running",
        "job.succeeded",
    ]
    read_only = kit.run_and_finish(kit.dev_a, 2, "key-audit-0003")
    assert kit.events(read_only["job_id"]) == ["job.queued", "job.running", "job.succeeded"]
    declined = kit.run_confirmed(kit.dev_a, 3, "key-audit-0004", approved=False)
    assert kit.events(declined["job_id"]) == ["job.queued", "job.awaiting_confirmation", "job.cancelled"]
    lapsed = kit.run(kit.dev_a, 4, "key-audit-0005").json()
    kit.clock.advance(121)
    kit.job(kit.dev_a, lapsed["job_id"])
    kit.job(kit.dev_a, lapsed["job_id"])  # reading again must not add rows
    assert kit.events(lapsed["job_id"]) == ["job.queued", "job.awaiting_confirmation", "job.expired"]
    kit.executor.inject("service_restart", "lost")
    unknown = kit.run_confirmed(kit.dev_a, 3, "key-audit-0006")
    assert kit.events(unknown["job_id"])[-1] == "job.unknown_outcome"
    kit.executor.inject("noop", "fail")
    failed = kit.run_and_finish(kit.dev_a, 1, "key-audit-0007")
    assert kit.events(failed["job_id"]) == ["job.queued", "job.running", "job.failed"]


def test_entries_say_who_what_when_outcome_job_and_key_hash(kit):
    j = kit.run_confirmed(kit.dev_a, 5, "key-audit-0008")
    entries = kit.client.get("/v1/deck/audit", headers=kit.owner, params={"job_id": j["job_id"]}).json()[
        "entries"
    ]
    last = entries[0]  # newest first
    assert last["event"] == "job.succeeded" and last["outcome"] == "succeeded"
    assert last["actor"] == "system:deck" and last["device_id"] == "dev-a"
    assert last["profile_id"] == "dev" and last["slot"] == 5
    assert last["actions"] == ["service_status", "service_restart"]
    assert last["job_id"] == j["job_id"] and last["ts"] == int(kit.clock())
    assert last["idempotency_key_hash"] == audit.key_hash("dev-a", "key-audit-0008")
    assert re.fullmatch(r"[0-9a-f]{64}", last["idempotency_key_hash"])
    running = next(e for e in entries if e["event"] == "job.running")
    assert running["actor"] == "device:dev-a"  # the device's confirmation started it
    assert [e["outcome"] for e in reversed(entries)] == ["pending", "pending", "pending", "succeeded"]


def test_denied_requests_are_audited(kit, make_profile):
    kit.run(kit.dev_b, 1, "key-audit-0009")  # not bound
    kit.run(kit.dev_a, 7, "key-audit-0010")  # empty slot
    j = kit.run(kit.dev_a, 3, "key-audit-0011").json()
    kit.confirm(kit.dev_a, j["job_id"], "wrong-nonce-value-0")
    kit.run(kit.dev_a, 1, "key-audit-0011")  # same key, different request
    kit.cancel(kit.dev_a, "dj-nothing")
    reasons = [r[7] for r in kit.audit_rows() if r[5] == "request.denied"]
    assert reasons == ["not_bound", "empty_slot", "bad_nonce", "idempotency_conflict", "unknown_job"]
    assert all(r[6] == "denied" for r in kit.audit_rows() if r[5] == "request.denied")


def test_profile_changes_are_audited_for_the_owner(kit, make_profile):
    p = make_profile()
    p["name"] = "Renamed"
    kit.put_profile(p)
    kit.client.delete("/v1/deck/profiles/dev", headers=kit.owner)
    rows = [(r[2], r[3], r[5], r[7], r[8]) for r in kit.audit_rows() if r[5].startswith("profile.")]
    assert rows == [
        ("owner", "owner", "profile.put", "created", "dev"),
        ("owner", "owner", "profile.put", "updated", "dev"),
        ("owner", "owner", "profile.deleted", None, "dev"),
    ]


def test_audit_never_contains_secrets(kit):
    key = "key-secret-plain-0001"
    j = kit.run(kit.dev_a, 3, key).json()
    nonce = j["nonce"]
    kit.confirm(kit.dev_a, j["job_id"], "wrong-nonce-value-0")
    kit.confirm(kit.dev_a, j["job_id"], nonce)
    kit.drain()
    kit.run(kit.dev_a, 1, key)  # conflict
    blob = repr(kit.audit_rows())
    for secret in (
        nonce,
        key,
        kit.tokens["dev-a"],
        kit.tokens["dev-b"],
        "test-admin-token",
        "wrong-nonce-value",
    ):
        assert secret not in blob
    api = kit.client.get("/v1/deck/audit", headers=kit.owner, params={"limit": 50}).text
    api += kit.history(kit.dev_a, limit=50).text
    for secret in (nonce, key, kit.tokens["dev-a"]):
        assert secret not in api
    # only the hash of the key and only action type names are stored: no arguments such as host ids
    assert audit.key_hash("dev-a", key) in blob and "nginx" not in blob and "h1" not in blob


def test_audit_entry_codes_are_validated():
    with pytest.raises(ValueError):
        audit.AuditEntry(ts=0, actor_kind="device", actor_id="d", event="job.running; drop", outcome="ok")
    with pytest.raises(ValueError):
        audit.AuditEntry(ts=0, actor_kind="device", actor_id="d", event="x", outcome="ok", reason="Free Text")


def test_a_device_sees_only_its_own_history_and_the_owner_sees_all(kit, make_profile):
    kit.put_profile(make_profile(profile_id="other", device_ids=["dev-b"]))
    a = kit.run_and_finish(kit.dev_a, 1, "key-audit-0020")
    rb = kit.run(kit.dev_b, 1, "key-audit-0021", profile="other").json()
    kit.drain()
    mine = kit.history(kit.dev_a).json()["entries"]
    theirs = kit.history(kit.dev_b).json()["entries"]
    assert {e["job_id"] for e in mine} == {a["job_id"]}
    assert {e["job_id"] for e in theirs} == {rb["job_id"]}
    assert all("device_id" not in e and "idempotency_key_hash" not in e and "actor" not in e for e in mine)
    owner = kit.client.get("/v1/deck/history", headers=kit.owner, params={"limit": 50}).json()["entries"]
    assert {e["job_id"] for e in owner if "job_id" in e} == {a["job_id"], rb["job_id"]}
    assert any(e["event"] == "profile.put" for e in owner)  # owner-only entries
    only_b = kit.client.get("/v1/deck/history", headers=kit.owner, params={"device_id": "dev-b"}).json()[
        "entries"
    ]
    assert {e["job_id"] for e in only_b} == {rb["job_id"]}
    by_job = kit.history(kit.dev_a, job_id=rb["job_id"]).json()["entries"]
    assert by_job == []  # another device's job id yields nothing


def test_history_is_newest_first_paginated_and_capped_at_50(kit):
    for i in range(30):  # 3 entries each = 90 entries
        kit.run_and_finish(kit.dev_a, 1, f"key-page-{i:05d}")
        kit.clock.advance(4)  # stay under the run rate limit
    first = kit.history(kit.dev_a).json()
    assert len(first["entries"]) == 10 and first["next_before"] == first["entries"][-1]["seq"]
    assert len(kit.history(kit.dev_a).content) < 4096  # a default page fits a device response
    seqs = [e["seq"] for e in first["entries"]]
    assert seqs == sorted(seqs, reverse=True)
    pages, before, seen = 0, None, []
    while True:
        params = {"limit": 50, **({"before": before} if before else {})}
        page = kit.history(kit.dev_a, **params).json()
        assert len(page["entries"]) <= 50
        seen += [e["seq"] for e in page["entries"]]
        pages += 1
        before = page["next_before"]
        if before is None:
            break
    assert pages == 2 and len(seen) == 90 and seen == sorted(set(seen), reverse=True)
    assert kit.history(kit.dev_a, limit=51).status_code == 422
    assert kit.history(kit.dev_a, limit=0).status_code == 422
    assert kit.history(kit.dev_a, before=0).status_code == 422
    assert kit.history(kit.dev_a, limit="x").status_code == 422
    assert len(kit.history(kit.dev_a, limit=50).json()["entries"]) == 50


def test_owner_job_list_is_paginated_and_filterable(kit):
    for i in range(7):
        kit.run_and_finish(kit.dev_a, 1, f"key-jobs-{i:05d}")
    kit.run(kit.dev_a, 3, "key-jobs-wait1")
    page = kit.client.get("/v1/deck/jobs", headers=kit.owner, params={"limit": 5}).json()
    assert len(page["jobs"]) == 5 and page["next_before"] is not None
    rest = kit.client.get(
        "/v1/deck/jobs", headers=kit.owner, params={"limit": 50, "before": page["next_before"]}
    ).json()
    assert len(rest["jobs"]) == 3 and rest["next_before"] is None
    waiting = kit.client.get(
        "/v1/deck/jobs", headers=kit.owner, params={"state": "awaiting_confirmation"}
    ).json()
    assert [j["state"] for j in waiting["jobs"]] == ["awaiting_confirmation"]
    assert kit.client.get("/v1/deck/jobs", headers=kit.owner, params={"state": "bogus"}).status_code == 422
    assert kit.client.get("/v1/deck/jobs", headers=kit.owner, params={"limit": 51}).status_code == 422
    assert all("nonce" not in j and j["device_id"] == "dev-a" for j in page["jobs"] + rest["jobs"])
