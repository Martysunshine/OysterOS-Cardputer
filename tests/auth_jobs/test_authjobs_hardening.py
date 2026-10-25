"""Hardening of the backend core: gated-job lifecycle, listener audiences and auth throttling,
run admission limits, expiry/startup reconciliation, baseline ownership, delivery-time
re-checks, SQLite durability, secret handling and mutation guards."""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient
from oyster import auth, cli, fileperms
from oyster.auth import AuthThrottle
from oyster.contracts import TargetProfile
from oyster.db.sqlite_store import SqliteStore
from oyster.main import Runtime, create_app
from oyster.settings import REPO_DIR, RunLimits
from oyster.store import Conflict

WRITE = {"characteristic_id": "virtual_led", "value": "green", "restore": True}
TARGET_FILE = REPO_DIR / "schemas" / "examples" / "target.lab-01.json"


@pytest.fixture
def dev(lab):
    lab.enroll_target()
    headers = lab.enroll("sim-01")
    return headers, lab.session(headers)


def _gated(lab, sid):
    return lab.job(sid, operation="ble_write_lab_value", arguments=WRITE)


def _confirmation(rec, approved=True):
    return {"job_id": rec.job.job_id, "nonce": rec.job.nonce, "approved": approved}


# --- a confirmation-gated job cannot skip awaiting_local_confirmation ---
def test_gated_job_cannot_run_without_awaiting_confirmation(lab, dev):
    headers, sid = dev
    rec = _gated(lab, sid)
    job_id = lab.poll(headers)[0]["job_id"]
    r = lab.status(headers, job_id, "running")
    assert r.status_code == 409 and "awaiting_local_confirmation" in r.json()["detail"]
    assert lab.store.get_job(job_id).status == "delivered"
    assert lab.status(headers, job_id, "awaiting_local_confirmation").status_code == 200
    assert lab.status(headers, job_id, "running").status_code == 200
    assert lab.status(headers, job_id, "running").status_code == 200  # repeating the status is a no-op
    assert rec.job.requires_local_confirmation


@pytest.mark.parametrize("outcome", ["succeeded", "unknown_outcome"])
@pytest.mark.parametrize("path", ["delivered", "awaiting_local_confirmation"])
def test_gated_result_needs_the_awaiting_path_even_with_a_matching_confirmation(lab, dev, ev, outcome, path):
    headers, sid = dev
    rec = _gated(lab, sid)
    job_id = lab.poll(headers)[0]["job_id"]
    if path != "delivered":
        assert lab.status(headers, job_id, path).status_code == 200
    lab.upload(headers, sid, [ev("evt-000070", sid, "sim-01", 70, operation="ble_write_lab_value")])
    conf = _confirmation(rec)
    forged_path = lab.result(headers, job_id, ["evt-000070"], outcome=outcome, confirmation=conf)
    assert forged_path.status_code == 409 and forged_path.json()["error"] == "confirmation_required"
    assert lab.store.get_job(job_id).status == path
    # after the real path the identical submission is accepted
    if path == "delivered":
        assert lab.status(headers, job_id, "awaiting_local_confirmation").status_code == 200
    assert lab.status(headers, job_id, "running").status_code == 200
    ok = lab.result(headers, job_id, ["evt-000070"], outcome=outcome, confirmation=_confirmation(rec))
    assert ok.status_code == 200 and ok.json()["status"] == outcome


def test_non_gated_jobs_are_unaffected(lab, dev, ev):
    headers, sid = dev
    lab.job(sid)
    job_id = lab.poll(headers)[0]["job_id"]
    assert lab.status(headers, job_id, "running").status_code == 200
    lab.upload(headers, sid, [ev("evt-000071", sid, "sim-01", 71)])
    assert lab.result(headers, job_id, ["evt-000071"]).json() == {"status": "succeeded", "duplicate": False}
    lab.job(sid)
    other = lab.poll(headers)[0]["job_id"]  # a non-gated result straight from delivered is fine too
    assert lab.result(headers, other, []).status_code == 200


# --- never-started gated jobs expire, they are not unknown_outcome ---
@pytest.mark.parametrize("state", ["delivered", "awaiting_local_confirmation"])
def test_deadline_sweep_expires_gated_jobs_that_never_started(lab, dev, state):
    headers, sid = dev
    rec = lab.job(sid, operation="ble_write_lab_value", arguments=WRITE, deadline_s=30)
    job_id = lab.poll(headers)[0]["job_id"]
    if state != "delivered":
        lab.status(headers, job_id, state)
    lab.clock.advance(31)
    assert lab.store.expire_jobs(lab.clock()) == 1
    assert lab.store.get_job(job_id).status == "expired", rec.job.operation
    assert lab.store.get_job_audit(job_id)["status_reason"] == "deadline"


# --- listener audiences, token rules, auth throttling ---
def test_device_listener_app_serves_only_device_routes(settings, store):
    app = create_app(settings, store=store, audience="device")
    c = TestClient(app)
    owner = {"Authorization": f"Bearer {settings.admin_token}"}
    dev_token = store.create_device("sim-01", "sim", True)
    device = {"Authorization": f"Bearer {dev_token}"}
    # admin routes are not mounted at all
    assert c.post("/v1/admin/devices", headers=owner, json={"device_id": "x"}).status_code == 404
    assert c.get("/v1/admin/targets", headers=owner).status_code == 404
    # the owner token is refused on every authenticated device route, never treated as an owner
    for method, path in (
        ("get", "/v1/device/jobs"),
        ("post", "/v1/sessions"),
        ("get", "/v1/runs/run-x"),
        ("post", "/v1/runs/run-x/cancel"),
        ("get", "/v1/sessions/sess-x/report"),
        ("post", "/v1/sessions/sess-x/assistant/messages"),
        ("post", "/v1/sessions/sess-x/events"),
        ("post", "/v1/jobs/job-x/status"),
    ):
        r = getattr(c, method)(path, headers=owner, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 403 and r.json()["error"] == "forbidden", path
    # health, time and the device token keep working
    assert c.get("/v1/health").status_code == 200 and c.get("/v1/time").status_code == 200
    assert c.get("/v1/device/jobs", headers=device).status_code == 200
    # the loopback app still serves the owner routes
    local = TestClient(create_app(settings, store=store))
    assert local.get("/v1/admin/targets", headers=owner).status_code == 200


def test_serve_binds_the_lan_listener_to_the_device_audience(settings):
    settings.tls_cert.parent.mkdir(parents=True, exist_ok=True)
    settings.tls_cert.write_text("placeholder", encoding="utf-8")  # existence decides the HTTPS listener
    settings.tls_key.write_text("placeholder", encoding="utf-8")
    app = create_app(settings)
    local, lan = cli.server_configs(app, settings)
    assert local.host == "127.0.0.1" and local.app.state.audience == "loopback"
    assert lan.host == settings.bind_host and lan.app.state.audience == "device"
    assert lan.app is not app
    owner = {"Authorization": f"Bearer {settings.admin_token}"}
    probe = TestClient(lan.app)
    assert probe.get("/v1/admin/targets", headers=owner).status_code == 404  # no admin routes on the LAN
    assert probe.get("/v1/device/jobs", headers=owner).status_code == 403  # nor the owner token
    assert TestClient(local.app).get("/v1/admin/targets", headers=owner).status_code == 200
    assert lan.app.state.runtime is app.state.runtime  # one worker and sweeper for both listeners
    with pytest.raises(ValueError):
        cli.server_configs(lan.app, settings)  # the device app is never the owner app


@pytest.mark.parametrize(
    "token, fragment",
    [
        ("short-token-0123456789", "shorter than 24"),
        ("", "empty"),
        ("change-me-to-a-long-random-string", "placeholder"),
        ("please-change-me-0123456789abcdef", "placeholder"),
        ("a" * 40, "variety"),
        ("x1" * 300, "longer than 512"),
    ],
)
def test_serve_refuses_weak_admin_tokens_and_says_how_to_generate_one(
    env_dir, monkeypatch, capsys, token, fragment
):
    monkeypatch.setenv("OYSTER_ADMIN_TOKEN", token)
    monkeypatch.setattr(cli, "run_servers", lambda *a, **k: pytest.fail("served with a weak admin token"))
    assert cli.main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "OYSTER_ADMIN_TOKEN" in err and fragment in err and "secrets.token_urlsafe(32)" in err


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OYSTER_DATA_DIR", str(tmp_path))
    return tmp_path


def test_bearer_limit_is_one_consistent_512_for_owner_and_devices(settings_factory, tmp_path):
    long_admin = "Ab1-" * 100  # 400 characters, above the old 256 limit
    c = TestClient(create_app(settings_factory(tmp_path, admin_token=long_admin)))
    assert c.get("/v1/admin/targets", headers={"Authorization": f"Bearer {long_admin}"}).status_code == 200
    too_long = "Ab1-" * 140  # 560 characters
    c2 = TestClient(create_app(settings_factory(tmp_path / "b", admin_token=too_long)))
    assert c2.get("/v1/admin/targets", headers={"Authorization": f"Bearer {too_long}"}).status_code == 401
    assert auth.MAX_TOKEN_LEN == 512 and auth.admin_token_problem(too_long)


def test_failed_authentication_is_throttled_per_client_with_a_clock(lab):
    bad = {"Authorization": "Bearer wrong-token"}
    for _ in range(11):  # more than 10 failures within a minute
        assert lab.c.get("/v1/device/jobs", headers=bad).status_code == 401
    r = lab.c.get("/v1/device/jobs", headers=bad)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    assert lab.c.get("/v1/admin/targets", headers=lab.owner).status_code == 429  # blocked whatever the token
    lab.clock.advance(59)
    assert lab.c.get("/v1/admin/targets", headers=lab.owner).status_code == 429
    lab.clock.advance(2)
    assert lab.c.get("/v1/admin/targets", headers=lab.owner).status_code == 200
    assert lab.c.get("/v1/device/jobs", headers=bad).status_code == 401  # a fresh window


def test_failures_outside_the_window_do_not_add_up():
    now = [1000.0]
    th = AuthThrottle(lambda: now[0])
    for _ in range(10):
        th.record_failure("1.2.3.4")
    now[0] += 61
    for _ in range(10):
        th.record_failure("1.2.3.4")
    assert th.retry_after("1.2.3.4") is None
    th.record_failure("1.2.3.4")
    assert th.retry_after("1.2.3.4") == 60 and th.retry_after("5.6.7.8") is None
    now[0] += 60
    assert th.retry_after("1.2.3.4") is None


def test_owner_token_on_the_device_listener_counts_as_a_failed_authentication(settings, store, clock):
    c = TestClient(create_app(settings, store=store, audience="device"))
    owner = {"Authorization": f"Bearer {settings.admin_token}"}
    for _ in range(11):
        assert c.get("/v1/device/jobs", headers=owner).status_code == 403
    assert c.get("/v1/device/jobs", headers=owner).status_code == 429


# --- run admission limits ---
class NullWorker:
    """Accepts runs and never finishes them, so they stay queued."""

    provider = None

    def start(self):
        pass

    def stop(self):
        pass

    def submit(self, run_id):
        pass


def _ask(c, headers, sid, q="hello"):
    return c.post(f"/v1/sessions/{sid}/assistant/messages", headers=headers, json={"question": q})


def test_one_active_run_per_session(settings, store):
    c = TestClient(create_app(settings, store=store, worker=NullWorker()))
    tok = {"Authorization": f"Bearer {store.create_device('d1', 'd1', True)}"}
    sid = c.post("/v1/sessions", headers=tok, json={"source_mode": "simulated"}).json()["session_id"]
    first = _ask(c, tok, sid)
    assert first.status_code == 200 and first.json()["status"] == "queued"
    again = _ask(c, tok, sid)
    assert again.status_code == 429 and again.json()["error"] == "rate_limited"
    assert "active run" in again.json()["detail"] and again.headers["retry-after"]
    assert len(store.list_runs(sid)) == 1
    # a second session of the same device is a different budget; cancelling frees the first one
    assert c.post(f"/v1/runs/{first.json()['run_id']}/cancel", headers=tok).json() == {"status": "cancelled"}
    assert _ask(c, tok, sid).status_code == 200


def test_at_most_four_queued_runs_overall(settings, store):
    c = TestClient(create_app(settings, store=store, worker=NullWorker()))
    codes = []
    for i in range(6):
        tok = {"Authorization": f"Bearer {store.create_device(f'dev-{i}', 'd', True)}"}
        sid = c.post("/v1/sessions", headers=tok, json={"source_mode": "simulated"}).json()["session_id"]
        codes.append(_ask(c, tok, sid).status_code)
    assert codes == [200, 200, 200, 200, 429, 429]
    assert "queued" in _ask(c, tok, sid).json()["detail"]


def test_thirty_runs_per_device_per_hour_with_a_clock(lab):
    a = lab.enroll("dev-a")
    sid = lab.session(a)
    for i in range(30):  # no worker: each run ends ai_unavailable at once, so only the hourly cap applies
        assert _ask(lab.c, a, sid, f"q{i}").status_code == 200
    r = _ask(lab.c, a, sid)
    assert r.status_code == 429 and "last hour" in r.json()["detail"]
    other = lab.enroll("dev-b")
    assert _ask(lab.c, other, lab.session(other)).status_code == 200  # per device, not global
    lab.clock.advance(3601)
    assert _ask(lab.c, a, sid).status_code == 200


def test_run_limits_are_configurable_and_default_to_the_documented_values(settings):
    assert settings.run_limits == RunLimits(1, 4, 30)


# --- startup reconciliation ---
def _seed_interrupted(store):
    store.create_device("sim-01", "sim", True)
    sess, _ = store.register_session(
        "sim-01", None, "simulated", ["ble_read_profile_value", "ble_write_lab_value"], None
    )
    target = TargetProfile.model_validate(json.loads(TARGET_FILE.read_text(encoding="utf-8")))
    store.upsert_target(target)
    prov = {"name": "fake", "model": None, "is_fake": True}
    runs = {}
    for status in ("queued", "running", "awaiting_device", "awaiting_local_confirmation", "succeeded"):
        run = store.create_run(sess.session_id, f"q-{status}", prov)
        run.status = status
        run.pending_action = {"job_id": "job-x"} if status.startswith("awaiting") else None
        store.save_run(run)
        runs[status] = run.run_id

    def job(run_id, gated=False):
        return store.create_job(
            run_id=run_id,
            session_id=sess.session_id,
            operation="ble_write_lab_value" if gated else "ble_read_profile_value",
            arguments=WRITE if gated else {"characteristic_id": "public_sensor"},
            target_id="target-lab-01",
            scope_version=target.scope_version,
            deadline_s=300,
            limits={},
            requires_local_confirmation=False,
        ).job.job_id

    jobs = {"queued": job(runs["running"])}
    jobs["delivered_gated"] = job(runs["running"], gated=True)
    assert store.next_job_for_device("sim-01").job_id == jobs["queued"]  # oldest first
    store.set_job_status("sim-01", jobs["queued"], "running")  # non-gated, in flight, may be executing
    jobs["inflight"] = jobs.pop("queued")
    assert store.next_job_for_device("sim-01").job_id == jobs["delivered_gated"]
    jobs["awaiting_gated"] = job(runs["awaiting_local_confirmation"], gated=True)
    store.next_job_for_device("sim-01")
    store.set_job_status("sim-01", jobs["awaiting_gated"], "awaiting_local_confirmation")
    jobs["running_gated"] = job(runs["running"], gated=True)
    store.next_job_for_device("sim-01")
    store.set_job_status("sim-01", jobs["running_gated"], "awaiting_local_confirmation")
    store.set_job_status("sim-01", jobs["running_gated"], "running")
    jobs["queued"] = job(runs["queued"])
    jobs["unrelated_queued"] = job(None)  # not tied to any run
    return runs, jobs


def test_startup_reconciliation_fails_interrupted_runs_and_ends_unstarted_jobs(settings, store):
    runs, jobs = _seed_interrupted(store)
    app = create_app(settings, store=store)  # a new process: reconcile once
    status = {k: store.get_run(v).status for k, v in runs.items()}
    assert status == {
        "queued": "failed",
        "running": "failed",
        "awaiting_device": "failed",
        "awaiting_local_confirmation": "failed",
        "succeeded": "succeeded",
    }
    failed = store.get_run(runs["running"])
    assert "restarted" in failed.error and failed.pending_action is None
    assert store.get_run(runs["awaiting_local_confirmation"]).pending_action is None
    got = {k: store.get_job(v).status for k, v in jobs.items()}
    assert got["delivered_gated"] == got["awaiting_gated"] == got["queued"] == "cancelled"
    assert store.get_job_audit(jobs["awaiting_gated"])["status_reason"] == "backend_restarted"
    # jobs that may already be executing keep their state (result and deadline still apply)
    assert got["inflight"] == "running" and got["running_gated"] == "running"
    assert got["unrelated_queued"] == "queued"  # not tied to a run: left alone
    # idempotent: a second Runtime.start / app creation does not redo or undo anything
    assert app.state.runtime.reconcile() is None
    assert store.reconcile_startup() == {"runs_failed": 0, "jobs_cancelled": 0}


def test_runtime_start_reconciles_before_background_services(store):
    runs, _ = _seed_interrupted(store)
    order = []

    class Spy:
        def start(self):
            order.append(("worker", store.get_run(runs["running"]).status))

        def stop(self):
            pass

    rt = Runtime(store, Spy(), sweep_interval_s=3600)
    rt.start()
    try:
        assert order == [("worker", "failed")]
    finally:
        rt.stop()


# --- baseline sessions belong to the same device ---
def test_link_baseline_rejects_another_devices_session(store):
    store.create_device("sim-01", "a", True)
    store.create_device("sim-02", "b", True)
    mine, _ = store.register_session("sim-01", None, "simulated", [], None)
    mine2, _ = store.register_session("sim-01", None, "simulated", [], None)
    theirs, _ = store.register_session("sim-02", None, "simulated", [], None)
    with pytest.raises(Conflict) as e:
        store.link_baseline(mine.session_id, theirs.session_id)
    assert e.value.code == "validation_failed" and "same device" in e.value.detail
    assert store.get_session(mine.session_id).linked_baseline_session_id is None
    assert store.link_baseline(mine.session_id, mine2.session_id).linked_baseline_session_id == (
        mine2.session_id
    )
    assert store.link_baseline(mine.session_id, None).linked_baseline_session_id is None


def test_link_baseline_cli_reports_the_rejection(env_dir, capsys):
    store = SqliteStore(env_dir / "oyster.db")
    store.create_device("d1", "d1", True)
    store.create_device("d2", "d2", True)
    a, _ = store.register_session("d1", None, "simulated", [], None)
    b, _ = store.register_session("d2", None, "simulated", [], None)
    store.close()
    assert cli.main(["link-baseline", "--session", a.session_id, "--baseline", b.session_id]) == 2
    assert "same device" in capsys.readouterr().err


# --- delivery re-checks the target's allowed operations and profile ---
def test_narrowed_allowed_operations_stop_a_queued_job_at_delivery(lab, dev):
    headers, sid = dev
    kept = lab.job(sid)  # ble_read_profile_value, scope_version 1
    narrowed = lab.job(sid, operation="ble_inspect_gatt", arguments={})
    t = json.loads(TARGET_FILE.read_text(encoding="utf-8"))
    assert "ble_inspect_gatt" in t["allowed_operations"]
    t["allowed_operations"] = [o for o in t["allowed_operations"] if o != "ble_inspect_gatt"]
    lab.enroll_target(**{"allowed_operations": t["allowed_operations"]})  # scope_version stays 1
    assert [j["job_id"] for j in lab.poll(headers)] == [kept.job.job_id]
    assert lab.poll(headers) == []
    after = lab.store.get_job(narrowed.job.job_id)
    assert after.status == "failed"
    assert lab.store.get_job_audit(narrowed.job.job_id)["status_reason"] == "target_policy_changed"


def test_changed_profile_stops_a_queued_job_at_delivery(lab, dev):
    headers, sid = dev
    rec = lab.job(sid)
    changed = TargetProfile.model_validate(
        {**json.loads(TARGET_FILE.read_text(encoding="utf-8")), "profile_id": "no-such-profile"}
    )
    lab.store.upsert_target(changed)  # same scope_version, a profile the backend does not know
    assert lab.poll(headers) == []
    assert lab.store.get_job(rec.job.job_id).status == "failed"
    assert lab.store.get_job_audit(rec.job.job_id)["status_reason"] == "target_policy_changed"


# --- durability and rollback ---
def test_sqlite_uses_full_synchronous_for_acknowledged_data(store):
    assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_failed_commit_is_rolled_back_and_the_connection_stays_usable(store):
    class FailingCommit:
        def __init__(self, db):
            self._db = db
            self.fail = True

        def execute(self, sql, *a):
            if sql == "COMMIT" and self.fail:
                self.fail = False
                raise sqlite3.OperationalError("disk I/O error")
            return self._db.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._db, name)

    real = store._db
    store._db = FailingCommit(real)
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.create_device("sim-01", "sim", True)
        assert not real.in_transaction
    finally:
        store._db = real
    assert store.list_devices() == []  # nothing from the failed transaction survived
    store.create_device("sim-01", "sim", True)  # the next transaction can begin
    assert [d.device_id for d in store.list_devices()] == ["sim-01"]


# --- owner-only secret files ---
class _Run:
    def __init__(self, listing="C:\\k.key WIN\\alice:(R,W)\n", fail_grant=False):
        self.calls = []
        self.listing = listing
        self.fail_grant = fail_grant

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        rc = 5 if (self.fail_grant and "/grant:r" in args) else 0
        out = self.listing if len(args) == 2 else ""
        return type("P", (), {"returncode": rc, "stdout": out, "stderr": ""})()


def test_windows_acl_command_is_icacls_current_user_only(tmp_path):
    run = _Run()
    path = tmp_path / "ca.key"
    assert fileperms.restrict_to_current_user(path, platform="win32", run=run, user="alice") is None
    assert run.calls[0] == ["icacls", str(path), "/inheritance:r", "/grant:r", "alice:(R,W)"]
    assert run.calls[1] == ["icacls", str(path)]  # read back and verified


@pytest.mark.parametrize(
    "run, fragment",
    [
        (_Run(fail_grant=True), "icacls exit 5"),
        (_Run(listing="C:\\k.key Everyone:(R)\n WIN\\alice:(R,W)\n"), "broad group"),
        (_Run(listing="C:\\k.key WIN\\bob:(R,W)\n"), "could not verify"),
    ],
)
def test_unconfirmed_windows_protection_is_reported(tmp_path, run, fragment):
    warning = fileperms.restrict_to_current_user(tmp_path / "k", platform="win32", run=run, user="alice")
    assert warning and fragment in warning


def test_icacls_missing_is_a_warning_not_a_crash(tmp_path):
    def boom(*a, **k):
        raise FileNotFoundError("icacls")

    warning = fileperms.restrict_to_current_user(tmp_path / "k", platform="win32", run=boom, user="alice")
    assert warning and "failed to run" in warning


def test_secret_files_are_created_with_mode_600_and_acl_before_content(tmp_path, monkeypatch):
    seen = {}
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **k):
        seen["mode"] = mode
        return real_open(path, flags, mode, *a, **k)

    order = []
    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(fileperms, "restrict_to_current_user", lambda p, **k: order.append(p.read_bytes()))
    assert fileperms.write_private_file(tmp_path / "t.token", b"SECRET") is None
    assert seen["mode"] == 0o600
    assert order == [b""]  # the ACL step ran while the file was still empty
    assert (tmp_path / "t.token").read_bytes() == b"SECRET"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_secret_files_are_not_group_or_world_accessible(tmp_path):
    from oyster.tls import make_dev_ca

    res = make_dev_ca("192.168.1.50", tmp_path)
    for key in (res.ca_key, res.server_key):
        assert (key.stat().st_mode & 0o077) == 0
    assert res.warnings == []


def test_cli_does_not_claim_owner_only_when_protection_failed(env_dir, monkeypatch, capsys):
    monkeypatch.setattr("oyster.tls.write_private_file", lambda path, data, **k: "could not restrict (test)")
    assert cli.main(["make-dev-ca", "--ip", "192.168.1.50"]) == 0
    out = capsys.readouterr()
    assert "owner-only access" not in out.out
    assert "NOT confirmed" in out.err and "could not restrict (test)" in out.err


def test_cli_token_file_warns_when_protection_failed(env_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli, "write_private_file", lambda path, data, **k: "no acl (test)")
    tok = env_dir / "t.token"
    assert cli.main(["enroll-device", "--id", "d1", "--direct", "--write-token", str(tok)]) == 0
    assert "NOT confirmed" in capsys.readouterr().err


# --- secrets in repr, admin token destinations ---
def test_settings_repr_hides_the_admin_token_and_api_key(settings_factory, tmp_path):
    s = settings_factory(
        tmp_path, admin_token="ADMIN-SECRET-VALUE-0123456789", ai_api_key="gsk" "_API_SECRET"
    )
    shown = repr(s) + str(s)
    assert "ADMIN-SECRET-VALUE" not in shown and "gsk" "_API_SECRET" not in shown
    assert s.admin_token == "ADMIN-SECRET-VALUE-0123456789"  # still readable by code


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "https://127.0.0.2",
        "http://[::1]:8000",
    ],
)
def test_loopback_destinations_need_no_flag(url):
    assert auth.admin_destination_problem(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.50:8000",
        "https://backend.example.com",
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example",
        "ftp://127.0.0.1",
        "not a url",
    ],
)
def test_non_loopback_destinations_are_refused_without_the_flag(url, env_dir, capsys):
    assert auth.admin_destination_problem(url)
    assert cli.main(["list-targets", "--backend", url]) == 2
    err = capsys.readouterr().err
    assert "error:" in err and ("allow-remote-admin" in err or "not an http" in err)


def test_allow_remote_admin_overrides_with_a_clear_text_warning(capsys):
    assert auth.admin_destination_problem("https://backend.example.com", True) is None
    cli.check_admin_destination("https://backend.example.com", True)
    assert "clear text" not in capsys.readouterr().err
    cli.check_admin_destination("http://192.168.1.50:8000", True)
    assert "clear text" in capsys.readouterr().err


def test_owner_client_refuses_remote_hosts_itself(settings):
    from oyster.cli import CliError

    with pytest.raises(CliError):
        cli.owner_client(settings, "http://192.168.1.50:8000")
    with cli.owner_client(settings, "http://192.168.1.50:8000", allow_remote=True) as c:
        assert str(c.base_url).startswith("http://192.168.1.50")


# --- guards for existing protections ---
def test_chunked_bodies_without_content_length_are_capped(lab, dev):
    headers, sid = dev

    def chunks(n_chunks, size):
        for _ in range(n_chunks):
            yield b" " * size  # no Content-Length: httpx sends Transfer-Encoding: chunked

    r = lab.c.post(f"/v1/sessions/{sid}/events", headers=headers, content=chunks(4, 2000))
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"
    r = lab.c.post("/v1/admin/devices", headers=lab.owner, content=chunks(5, 4000))
    assert r.status_code == 413
    ok = lab.c.post(f"/v1/sessions/{sid}/events", headers=headers, content=iter([b'{"events":', b"[]}"]))
    assert ok.status_code == 200 and ok.json()["accepted"] == 0


def test_results_for_jobs_that_were_never_in_flight_are_rejected(lab, dev):
    headers, sid = dev
    queued = lab.job(sid)
    r = lab.result(headers, queued.job.job_id, [])
    assert r.status_code == 409 and "no result is expected" in r.json()["detail"]
    assert lab.store.get_job(queued.job.job_id).status == "queued"
    run = lab.store.create_run(sid, "q", {"name": "fake", "model": None, "is_fake": True})
    run.status = "running"
    lab.store.save_run(run)
    cancelled = lab.job(sid, run_id=run.run_id)
    assert lab.c.post(f"/v1/runs/{run.run_id}/cancel", headers=headers).status_code == 200
    assert lab.store.get_job(cancelled.job.job_id).status == "cancelled"
    r = lab.result(headers, cancelled.job.job_id, [], outcome="succeeded")
    assert r.status_code == 409 and lab.store.get_job(cancelled.job.job_id).status == "cancelled"


# --- direct reads are limited to public, non-challenge characteristics ---
def _readable_characteristics():
    from oyster.policy import catalogue

    chars = catalogue.load_profile("oyster-lab-v1")["characteristics"]
    return [
        (cid, c["policy"].get("read") == "public" and cid != "app_auth")
        for cid, c in chars.items()
        if "read" in c["properties"]
    ]


@pytest.mark.parametrize("cid, allowed", _readable_characteristics())
def test_ble_read_profile_value_is_limited_to_public_non_challenge_characteristics(lab, dev, cid, allowed):
    _, sid = dev
    args = {"characteristic_id": cid}
    if allowed:
        assert lab.job(sid, arguments=args).job.arguments == args
        return
    with pytest.raises(Conflict) as e:
        lab.job(sid, arguments=args)
    assert e.value.code == "validation_failed"


def test_the_readable_set_is_what_the_firmware_and_ai_tools_offer():
    allowed = {cid for cid, ok in _readable_characteristics() if ok}
    assert {"public_sensor", "device_info", "diag_status"} <= allowed
    assert not allowed & {"restricted_enc", "restricted_mitm", "diag_manifest", "chunk_data", "app_auth"}


# --- run answers carry the evaluator findings, bounded for the device ---
def _finding_rows(n, **over):
    rows = []
    for i in range(n):
        rows.append(
            {
                "finding_id": f"finding-{i:024d}",
                "test_id": f"test_number_{i:03d}_" + "x" * 50,
                "classification": "passed",
                "severity": "informational",
                **over,
            }
        )
    return rows


def _answer_run(lab, headers, answer):
    sid = lab.session(headers)
    run = lab.store.create_run(sid, "q", {"name": "fake", "model": None, "is_fake": True})
    run.status, run.answer = "succeeded", answer
    lab.store.save_run(run)
    return run.run_id


def test_run_answer_exposes_findings_and_stays_schema_valid(lab):
    from oyster.evidence.validate import schema_errors

    a = lab.enroll("dev-a")
    rows = _finding_rows(3)
    rows[1]["classification"], rows[1]["severity"] = "confirmed", "medium"
    run_id = _answer_run(
        lab, a, {"answer_markdown": "ok", "cited_evidence_ids": [], "findings": rows, "ignored": "x"}
    )
    for headers in (a, lab.owner):
        view = lab.c.get(f"/v1/runs/{run_id}", headers=headers).json()
        assert view["answer"]["findings"] == rows and "ignored" not in view["answer"]
        assert not schema_errors("run.schema.json", view)


def test_device_run_response_is_bounded_and_keeps_the_important_findings(lab):
    from oyster.evidence.validate import schema_errors

    a = lab.enroll("dev-a")
    rows = _finding_rows(60)
    rows[55].update(classification="confirmed", severity="high")  # last in stored order, first by importance
    long_text = "Explanation. " * 400
    run_id = _answer_run(lab, a, {"answer_markdown": long_text, "cited_evidence_ids": [], "findings": rows})
    raw = lab.c.get(f"/v1/runs/{run_id}", headers=a)
    assert len(raw.content) <= 4096
    ans = raw.json()["answer"]
    assert not schema_errors("run.schema.json", raw.json())
    assert len(ans["findings"]) + ans["findings_truncated"] == 60 and ans["findings_truncated"] > 0
    assert ans["findings"][0]["finding_id"] == rows[55]["finding_id"]  # confirmed survives the cut
    assert raw.json() == lab.c.get(f"/v1/runs/{run_id}", headers=a).json()  # deterministic
    owner = lab.c.get(f"/v1/runs/{run_id}", headers=lab.owner).json()["answer"]
    assert len(owner["findings"]) == 60 and "findings_truncated" not in owner  # owner view is complete


def test_device_run_response_drops_findings_last_when_the_cap_alone_is_not_enough(lab):
    a = lab.enroll("dev-a")
    rows = _finding_rows(16)
    for r in rows:
        r["test_id"] = "t" * 80
        r["finding_id"] = "finding-" + "f" * 200  # oversized ids: 16 of them exceed 4 KiB on their own
    rows[9].update(classification="confirmed", severity="high", finding_id="finding-" + "c" * 200)
    run_id = _answer_run(lab, a, {"answer_markdown": "Explanation. " * 400, "findings": rows})
    raw = lab.c.get(f"/v1/runs/{run_id}", headers=a)
    ans = raw.json()["answer"]
    assert len(raw.content) <= 4096
    assert ans["findings"] and ans["findings"][0]["classification"] == "confirmed"
    assert len(ans["findings"]) + ans["findings_truncated"] == 16
