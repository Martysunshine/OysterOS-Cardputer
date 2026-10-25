"""Idempotency-Key semantics, the run-request body, and the per-device run rate limit."""

from __future__ import annotations

import threading

import pytest


def test_the_key_header_is_required(kit):
    r = kit.run(kit.dev_a, 1, key=None)
    assert r.status_code == 422 and r.json()["error"] == "validation_failed"
    assert kit.drain() == 0 and kit.executor.calls == []


@pytest.mark.parametrize(
    "key,ok",
    [
        ("1234567", False),  # 7 characters
        ("12345678", True),  # 8
        ("k" * 80, True),
        ("k" * 81, False),
        ("has space 1", False),
        ("semi;colon1", False),
        ("new\tline-1", False),
        ("", False),
    ],
)
def test_key_length_and_charset(kit, key, ok):
    r = kit.client.post("/v1/deck/profiles/dev/slots/1/run", headers={**kit.dev_a, "Idempotency-Key": key})
    assert (r.status_code == 200) is ok, r.text
    if not ok:
        assert r.status_code == 422


def test_replay_returns_the_stored_job_and_executes_once(kit):
    first = kit.run(kit.dev_a, 2, "key-replay-01")
    second = kit.run(kit.dev_a, 2, "key-replay-01")
    assert first.status_code == second.status_code == 200
    assert first.json()["duplicate"] is False and second.json()["duplicate"] is True
    assert first.json()["job_id"] == second.json()["job_id"]
    assert kit.drain() == 1  # one queue entry, not two
    assert len(kit.executor.calls) == 1
    third = kit.run(kit.dev_a, 2, "key-replay-01")
    assert third.json()["state"] == "succeeded" and third.json()["duplicate"] is True
    assert len(kit.executor.calls) == 1
    assert len(kit.client.get("/v1/deck/jobs", headers=kit.owner).json()["jobs"]) == 1


def test_same_key_with_a_different_request_is_a_conflict(kit):
    first = kit.run(kit.dev_a, 1, "key-conflict-1").json()
    other_slot = kit.run(kit.dev_a, 2, "key-conflict-1")
    assert other_slot.status_code == 409 and other_slot.json()["error"] == "conflict"
    kit.put_profile(kit_profile_copy(kit, "other"))
    other_profile = kit.run(kit.dev_a, 1, "key-conflict-1", profile="other")
    assert other_profile.status_code == 409
    kit.drain()
    assert len(kit.executor.calls) == 1
    assert kit.job(kit.dev_a, first["job_id"]).json()["state"] == "succeeded"
    assert len(kit.client.get("/v1/deck/jobs", headers=kit.owner).json()["jobs"]) == 1
    denied = [r for r in kit.audit_rows() if r[5] == "request.denied"]
    assert [r[7] for r in denied] == ["idempotency_conflict", "idempotency_conflict"]


def kit_profile_copy(kit, profile_id):
    import json

    body = json.loads(kit.client.get("/v1/deck/profiles/dev", headers=kit.owner).text)
    body["profile_id"] = profile_id
    return body


def test_a_key_is_never_reused_after_completion(kit):
    done = kit.run_and_finish(kit.dev_a, 1, "key-after-0001")
    assert done["state"] == "succeeded"
    again = kit.run(kit.dev_a, 1, "key-after-0001")
    assert again.json()["job_id"] == done["job_id"] and again.json()["duplicate"] is True
    assert kit.drain() == 0
    assert len(kit.executor.calls) == 1
    # a finished impactful job: the same key does not start another execution either
    j = kit.run_confirmed(kit.dev_a, 3, "key-after-0002")
    assert j["state"] == "succeeded"
    again = kit.run(kit.dev_a, 3, "key-after-0002").json()
    assert again["job_id"] == j["job_id"] and again["state"] == "succeeded" and "nonce" not in again
    assert kit.drain() == 0 and len(kit.executor.calls) == 2


def test_keys_are_scoped_to_the_device(kit, make_profile):
    kit.put_profile(make_profile(device_ids=["dev-a", "dev-b"]))
    a = kit.run(kit.dev_a, 1, "key-shared-001").json()
    b = kit.run(kit.dev_b, 1, "key-shared-001").json()
    assert a["job_id"] != b["job_id"] and a["duplicate"] is False and b["duplicate"] is False


def test_replay_of_an_awaiting_impactful_job_returns_the_same_nonce_to_its_device_only(kit):
    first = kit.run(kit.dev_a, 3, "key-nonce-0001").json()
    again = kit.run(kit.dev_a, 3, "key-nonce-0001").json()
    assert first["nonce"] == again["nonce"] and again["duplicate"] is True
    assert again["state"] == "awaiting_confirmation"
    assert kit.executor.calls == []


def test_concurrent_duplicate_runs_execute_once(kit):
    results, errors = [], []
    barrier = threading.Barrier(8)

    def go():
        try:
            barrier.wait(timeout=5)
            results.append(kit.service.run_slot("dev-a", "dev", 2, "key-race-00001"))
        except Exception as e:  # pragma: no cover - reported through the assertion below
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert len({job.job_id for job, _ in results}) == 1
    assert sorted(dup for _, dup in results) == [False] + [True] * 7
    assert kit.drain() == 1
    assert len(kit.executor.calls) == 1


def test_concurrent_duplicate_impactful_runs_make_one_job_and_one_nonce(kit):
    results = []
    barrier = threading.Barrier(6)

    def go():
        barrier.wait(timeout=5)
        results.append(kit.service.run_slot("dev-a", "dev", 3, "key-race-00002"))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len({(job.job_id, job.nonce) for job, _ in results}) == 1
    assert kit.executor.calls == []


def test_concurrent_confirmations_start_the_job_once(kit):
    j = kit.run(kit.dev_a, 3, "key-race-00003").json()
    codes = []
    barrier = threading.Barrier(6)

    def go():
        barrier.wait(timeout=5)
        try:
            kit.service.confirm("dev-a", j["job_id"], j["job_id"], j["nonce"], True)
            codes.append(200)
        except Exception as e:
            codes.append(e.status)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(codes) == [200] + [409] * 5
    assert kit.drain() == 1 and len(kit.executor.calls) == 1


def test_run_body_is_optional_bounded_and_never_read(kit):
    flags = {
        "confirmed": True,
        "approved": True,
        "nonce": "x",
        "impactful": False,
        "requires_confirmation": False,
    }
    r = kit.run(kit.dev_a, 3, "key-body-00001", json=flags)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "awaiting_confirmation" and body["requires_confirmation"] is True
    assert kit.drain() == 0 and kit.executor.calls == []
    assert kit.run(kit.dev_a, 1, "key-body-00002", json={}).status_code == 200
    assert kit.run(kit.dev_a, 1, "key-body-00003", content=b"").status_code == 200
    assert kit.run(kit.dev_a, 1, "key-body-00004", content=b"[1]").status_code == 422
    assert kit.run(kit.dev_a, 1, "key-body-00005", content=b"{nope").status_code == 422
    assert kit.run(kit.dev_a, 1, "key-body-00006", content=b'{"a":"' + b"x" * 2000 + b'"}').status_code == 413


def test_a_body_does_not_change_the_identity_of_a_request(kit):
    first = kit.run(kit.dev_a, 1, "key-body-00007", json={"anything": 1})
    second = kit.run(kit.dev_a, 1, "key-body-00007")
    assert second.status_code == 200 and second.json()["job_id"] == first.json()["job_id"]


def test_run_requests_are_rate_limited_per_device(kit, make_profile):
    kit.put_profile(make_profile(device_ids=["dev-a", "dev-b"]))
    for i in range(20):
        assert kit.run(kit.dev_a, 1, f"key-rate-{i:05d}").status_code == 200
        kit.drain()
    limited = kit.run(kit.dev_a, 1, "key-rate-99999")
    assert limited.status_code == 429 and limited.json()["error"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1
    assert kit.run(kit.dev_b, 1, "key-rate-00000").status_code == 200  # another device is unaffected
    kit.clock.advance(30)
    assert kit.run(kit.dev_a, 1, "key-rate-99998").status_code == 429
    kit.clock.advance(31)
    assert kit.run(kit.dev_a, 1, "key-rate-99997").status_code == 200
    jobs = kit.client.get("/v1/deck/jobs", headers=kit.owner, params={"limit": 50}).json()["jobs"]
    assert len(jobs) == 22  # 20 + dev-b + the final one; the limited requests created nothing


def test_the_runner_queue_is_bounded(kit_factory, make_profile):
    from oyster.deck.jobs import DeckConfig

    kit = kit_factory(DeckConfig(queue_size=2))
    dev = kit.enroll("dev-a")
    kit.put_profile()
    assert kit.run(dev, 1, "key-queue-0001").status_code == 200
    assert kit.run(dev, 1, "key-queue-0002").status_code == 200
    full = kit.run(dev, 1, "key-queue-0003")
    assert full.status_code == 503 and full.json()["error"] == "busy"
    assert full.headers["Retry-After"] == "5"
    assert len(kit.client.get("/v1/deck/jobs", headers=kit.owner).json()["jobs"]) == 2  # nothing was created
    assert kit.drain() == 2
    assert kit.run(dev, 1, "key-queue-0003").status_code == 200  # the same key is usable: no job was stored
