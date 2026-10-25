"""Crash and restart behaviour, and the deck's own SQLite file."""

from __future__ import annotations

import sqlite3
import time

from oyster.deck.store import DB_NAME, DeckStore


def states(kit):
    jobs = kit.client.get("/v1/deck/jobs", headers=kit.owner, params={"limit": 50}).json()["jobs"]
    return {j["job_id"]: (j["state"], j.get("reason")) for j in jobs}


def test_jobs_found_running_or_queued_after_a_restart_are_settled_and_never_re_run(kit):
    done = kit.run_and_finish(kit.dev_a, 1, "key-boot-00004")["job_id"]
    calls_before = len(kit.executor.calls)
    queued = kit.run(kit.dev_a, 1, "key-boot-00001").json()["job_id"]  # never reached the runner
    claimed = kit.run(kit.dev_a, 2, "key-boot-00002").json()["job_id"]
    assert kit.service.begin(claimed) is not None  # the runner claimed it, then the process died
    risky = kit.run(kit.dev_a, 3, "key-boot-00003").json()
    kit.confirm(kit.dev_a, risky["job_id"], risky["nonce"])  # approved, running, result never recorded

    executor = kit.restart()

    assert kit.runtime.recovered == {"unknown_outcome": 1, "failed": 2}
    now = states(kit)
    assert now[queued] == ("failed", "backend_restarted")
    assert now[claimed] == ("failed", "backend_restarted")  # not impactful: a plain failure
    assert now[risky["job_id"]] == ("unknown_outcome", "backend_restarted")  # impactful: outcome unknown
    assert now[done] == ("succeeded", None)
    assert kit.drain() == 0
    kit.runtime.runner.sweep_interval_s = 0.05
    kit.runtime.start()  # even with the worker running, nothing is re-executed
    time.sleep(0.3)
    kit.runtime.stop()
    assert executor.calls == [] and calls_before == 1  # the first process ran only what it finished


def test_the_unknown_outcome_after_a_restart_is_visible_and_not_retriable(kit):
    risky = kit.run(kit.dev_a, 3, "key-boot-00005").json()
    kit.confirm(kit.dev_a, risky["job_id"], risky["nonce"])
    executor = kit.restart()
    j = kit.job(kit.dev_a, risky["job_id"]).json()
    assert j["state"] == "unknown_outcome" and j["outcome_known"] is False
    replay = kit.run(kit.dev_a, 3, "key-boot-00005").json()  # a retry with the same key is the stored job
    assert replay["job_id"] == risky["job_id"] and replay["state"] == "unknown_outcome"
    assert kit.confirm(kit.dev_a, risky["job_id"], risky["nonce"]).status_code == 409
    assert kit.drain() == 0 and executor.calls == []


def test_a_job_awaiting_confirmation_survives_a_restart_with_its_deadline_and_nonce(kit):
    j = kit.run(kit.dev_a, 3, "key-boot-00006").json()
    executor = kit.restart()
    assert kit.runtime.recovered == {"unknown_outcome": 0, "failed": 0}
    again = kit.job(kit.dev_a, j["job_id"]).json()
    assert again["state"] == "awaiting_confirmation" and again["nonce"] == j["nonce"]
    assert again["confirm_deadline"] == j["confirm_deadline"]
    assert kit.confirm(kit.dev_a, j["job_id"], j["nonce"]).status_code == 200
    kit.drain()
    assert len(executor.calls) == 1  # executed exactly once, by the new process
    assert kit.job(kit.dev_a, j["job_id"]).json()["state"] == "succeeded"


def test_an_awaiting_job_whose_window_passed_during_the_outage_expires(kit):
    j = kit.run(kit.dev_a, 3, "key-boot-00007").json()
    executor = kit.restart()
    kit.clock.advance(300)
    late = kit.confirm(kit.dev_a, j["job_id"], j["nonce"])
    assert late.status_code == 409 and late.json()["error"] == "expired"
    assert kit.drain() == 0 and executor.calls == []


def test_recovery_is_idempotent_and_audited(kit):
    risky = kit.run(kit.dev_a, 3, "key-boot-00008").json()
    kit.confirm(kit.dev_a, risky["job_id"], risky["nonce"])
    kit.restart()
    rows = len(kit.audit_rows())
    assert kit.service.recover_after_restart() == {"unknown_outcome": 0, "failed": 0}
    assert len(kit.audit_rows()) == rows
    last = [r for r in kit.audit_rows() if r[11] == risky["job_id"]][-1]
    assert (last[2], last[3], last[5], last[6], last[7]) == (
        "system",
        "startup",
        "job.unknown_outcome",
        "unknown_outcome",
        "backend_restarted",
    )


def test_a_replay_after_a_restart_returns_the_stored_job_without_executing(kit):
    j = kit.run_and_finish(kit.dev_a, 2, "key-boot-00009")
    executor = kit.restart()
    again = kit.run(kit.dev_a, 2, "key-boot-00009").json()
    assert again["job_id"] == j["job_id"] and again["state"] == "succeeded" and again["duplicate"] is True
    assert kit.drain() == 0 and executor.calls == []


def test_the_deck_has_its_own_database_file(kit):
    deck_db = kit.tmp_path / DB_NAME
    assert deck_db.exists() and DB_NAME == "deck.db"
    deck_tables = {r[0] for r in sqlite3.connect(deck_db).execute("SELECT name FROM sqlite_master")}
    assert {"profiles", "jobs", "audit", "meta"} <= deck_tables
    with kit.core._tx() as db:  # the shared store has no deck tables
        core_tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not core_tables & {"profiles", "jobs_deck", "audit"}


def test_store_pragmas_and_idempotent_creation(tmp_path):
    path = tmp_path / "data" / DB_NAME  # the data directory is created on demand
    store = DeckStore(path)
    with store.tx() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL is the default
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        store.put_profile(conn, "p", "{}", 1.0)
    store.close()
    again = DeckStore(path)  # creating the tables a second time changes nothing
    with again.tx() as conn:
        assert again.get_profile_json(conn, "p") == "{}"
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert triggers == {"audit_no_update", "audit_no_delete"}
    again.close()


def test_store_rejects_unknown_synchronous_modes(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        DeckStore(tmp_path / DB_NAME, synchronous="1; DROP TABLE jobs")


def test_the_unique_key_constraint_backs_up_idempotency(kit):
    import dataclasses

    import pytest

    j = kit.run(kit.dev_a, 1, "key-boot-00010").json()
    store = kit.runtime.store
    with pytest.raises(sqlite3.IntegrityError), store.tx() as conn:
        original = store.get_job(conn, j["job_id"])
        store.insert_job(conn, dataclasses.replace(original, job_id="dj-duplicate-attempt"))
    with store.tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
