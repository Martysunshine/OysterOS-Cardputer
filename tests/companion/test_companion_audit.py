"""The audit log: append-only, complete for the required events, free of secrets, bounded."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import oyster.companion.audit as audit_module
import oyster.companion.store as store_module
import pytest
from _companion_support import ADMIN, OWNER, Clock, Rig
from oyster.companion.audit import AUDIT_ACTIONS, AuditLog
from oyster.companion.store import CompanionStore

SQL_CHANGES = re.compile(r"\b(UPDATE|DELETE)\b", re.IGNORECASE)


def test_events_are_recorded_without_secrets(rig: Rig):
    dev = rig.device("dev-1")
    rig.host("host-1")
    rig.c.get("/v1/hosts/host-1/health", headers=dev)
    rig.c.get("/v1/hosts/missing/health", headers=dev)
    created = rig.new_code()
    rig.redeem("ABCDEFGH")
    token = rig.redeem(created["code"]).json()["token"]
    rig.c.delete("/v1/admin/hosts/host-1", headers=OWNER)

    entries = rig.audit()
    assert {e["action"] for e in entries} == set(AUDIT_ACTIONS)
    by_action = {e["action"]: e for e in reversed(entries)}
    assert by_action["host_enrolled"]["actor"] == "owner"
    assert by_action["health_read"]["actor"] == "device:dev-1"
    assert by_action["pairing_redeemed"]["actor"] == "device:dev-9"
    assert by_action["pairing_failed"]["actor"] == "anonymous"
    assert [e["outcome"] for e in entries if e["action"] == "health_read"] == ["denied", "ok"]

    everything = str(entries)
    for secret in (ADMIN, token, created["code"], rig.store.create_device("probe", "p", True)):
        assert secret not in everything
    assert [e["id"] for e in entries] == sorted((e["id"] for e in entries), reverse=True)
    assert all(e["ts"] == 1_800_000_000.0 for e in entries)


def test_the_audit_log_has_no_way_to_change_or_remove_rows(tmp_path: Path):
    assert not SQL_CHANGES.search(Path(audit_module.__file__).read_text(encoding="utf-8"))
    source = Path(store_module.__file__).read_text(encoding="utf-8")
    forbidden = r"UPDATE\s+audit\b|DELETE\s+FROM\s+audit\b|INSERT\s+OR\s+REPLACE\s+INTO\s+audit"
    assert not re.search(forbidden, source, re.IGNORECASE)
    public_log = {n for n in dir(AuditLog) if not n.startswith("_")}
    assert public_log == {"record", "entries"}
    public_store = {n for n in dir(CompanionStore) if "audit" in n and not n.startswith("_")}
    assert public_store == {"audit_append", "audit_list"}


def test_the_database_itself_refuses_updates_and_deletes_of_audit_rows(rig: Rig):
    rig.host("host-1")
    db = rig.raw_companion_db()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit SET actor='someone-else'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit")
    assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1


def test_deleting_a_host_does_not_remove_its_audit_trail(rig: Rig):
    rig.host("host-1")
    rig.c.delete("/v1/admin/hosts/host-1", headers=OWNER)
    assert [e["action"] for e in rig.audit()] == ["host_deleted", "host_enrolled"]


def test_pages_do_not_overlap_and_end(rig: Rig):
    for i in range(5):
        rig.host(f"h{i}")
    seen: list[int] = []
    url = "/v1/admin/audit?limit=2"
    while url:
        page = rig.c.get(url, headers=OWNER).json()
        seen += [e["id"] for e in page["entries"]]
        nxt = page["next_before_id"]
        url = f"/v1/admin/audit?limit=2&before_id={nxt}" if nxt else ""
    assert seen == [5, 4, 3, 2, 1]


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=abc", "before_id=0", "before_id=x"])
def test_audit_query_is_bounded(rig: Rig, query):
    r = rig.c.get(f"/v1/admin/audit?{query}", headers=OWNER)
    assert r.status_code == 422 and r.json()["error"] == "validation_failed"


def test_fields_are_constrained_before_they_are_stored(rig: Rig):
    dev = rig.device("dev-1")
    rig.c.get("/v1/hosts/a%3Bb%0Ac/health", headers=dev)
    entry = rig.audit()[0]
    assert entry["target"] == "a?b?c"
    log = AuditLog(rig.companion.store, Clock())
    log.record("a" * 500, "host_deleted", "t" * 500, "o" * 500, "d" * 500)
    last = rig.audit()[0]
    assert [len(last[k]) for k in ("actor", "target", "outcome", "detail")] == [64, 64, 64, 160]
    with pytest.raises(ValueError, match="unknown audit action"):
        log.record("owner", "run_command")
