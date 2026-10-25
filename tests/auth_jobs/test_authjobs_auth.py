"""Authentication and ownership: 401/403/404/409 semantics from docs/API.md."""

from __future__ import annotations

import sqlite3


def test_health_and_time_need_no_auth(lab):
    assert lab.c.get("/v1/health").json() == {"status": "ok", "api": "v1"}
    t = lab.c.get("/v1/time").json()
    assert set(t) == {"unix", "iso"} and isinstance(t["unix"], int) and t["iso"].endswith("Z")


def test_admin_routes_401_without_or_with_bad_token_and_403_for_devices(lab):
    body = {"device_id": "dev-x", "display_name": "x", "simulated": True}
    r = lab.c.post("/v1/admin/devices", json=body)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert r.headers.get("www-authenticate") == "Bearer"
    assert (
        lab.c.post("/v1/admin/devices", json=body, headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )
    assert (
        lab.c.post("/v1/admin/devices", json=body, headers={"Authorization": "Basic abc"}).status_code == 401
    )
    dev = lab.enroll("dev-a")
    for method, path in (
        ("post", "/v1/admin/devices"),
        ("post", "/v1/admin/targets"),
        ("get", "/v1/admin/targets"),
    ):
        r = getattr(lab.c, method)(path, headers=dev, **({"json": body} if method == "post" else {}))
        assert r.status_code == 403 and r.json()["error"] == "forbidden", path


def test_owner_token_cannot_act_as_a_device(lab):
    r = lab.c.get("/v1/device/jobs", headers=lab.owner)
    assert r.status_code == 403
    r = lab.c.post("/v1/sessions", headers=lab.owner, json={"source_mode": "simulated"})
    assert r.status_code == 403


def test_empty_admin_token_disables_owner_access(tmp_path, settings_factory):
    from fastapi.testclient import TestClient
    from oyster.main import create_app

    c = TestClient(create_app(settings_factory(tmp_path, admin_token="")))
    assert c.get("/v1/admin/targets", headers={"Authorization": "Bearer "}).status_code == 401
    assert c.get("/v1/admin/targets").status_code == 401


def test_device_tokens_are_stored_salted_hashed_and_rotated(lab, settings):
    a = lab.enroll("dev-a")
    b = lab.enroll("dev-b")
    tok_a = a["Authorization"].split()[1]
    assert len(tok_a) >= 43  # 32 random bytes, urlsafe base64
    db = sqlite3.connect(settings.db_path)
    rows = db.execute("SELECT device_id, token_salt, token_hash FROM devices ORDER BY device_id").fetchall()
    dump = "\n".join(line for line in db.iterdump())
    db.close()
    assert tok_a not in dump and b["Authorization"].split()[1] not in dump
    assert rows[0][1] != rows[1][1] and len(rows[0][2]) == 64
    lab.session(a)
    # re-enrolling rotates: the old token is revoked, the new one works
    a2 = lab.enroll("dev-a")
    assert lab.c.get("/v1/device/jobs", headers=a).status_code == 401
    assert lab.c.get("/v1/device/jobs", headers=a2).status_code == 200


def test_simulated_device_cannot_open_live_session(lab):
    sim = lab.enroll("sim-1", simulated=True)
    r = lab.c.post("/v1/sessions", headers=sim, json={"source_mode": "live", "capabilities": []})
    assert r.status_code == 422 and r.json()["error"] == "validation_failed"


def test_session_registration_is_idempotent_and_ids_bind_to_one_device(lab):
    a, b = lab.enroll("dev-a"), lab.enroll("dev-b")
    sid = lab.session(a, "sess-0000abcd-001")
    r = lab.c.post("/v1/sessions", headers=a, json={"session_id": sid, "source_mode": "simulated"})
    assert r.json() == {"session_id": sid, "created": False}
    r = lab.c.post("/v1/sessions", headers=b, json={"session_id": sid, "source_mode": "simulated"})
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    r = lab.c.post("/v1/sessions", headers=a, json={"session_id": "bad id", "source_mode": "simulated"})
    assert r.status_code == 422
    r = lab.c.post("/v1/sessions", headers=a, json={"source_mode": "simulated", "capabilities": ["shell"]})
    assert r.status_code == 422


def test_foreign_and_unknown_ids_are_404_everywhere(lab, ev):
    lab.enroll_target()
    a, b = lab.enroll("dev-a"), lab.enroll("dev-b")
    sid = lab.session(a)
    lab.upload(a, sid, [ev("evt-000001", sid, "dev-a", 1)])
    job = lab.job(sid).job
    run = lab.c.post(f"/v1/sessions/{sid}/assistant/messages", headers=a, json={"question": "hi"}).json()[
        "run_id"
    ]

    probes = [
        ("post", f"/v1/sessions/{sid}/events", {"events": [ev("evt-000002", sid, "dev-b", 2)]}),
        ("post", f"/v1/sessions/{sid}/assistant/messages", {"question": "leak?"}),
        ("get", f"/v1/sessions/{sid}/report", None),
        ("get", f"/v1/runs/{run}", None),
        ("post", f"/v1/runs/{run}/cancel", None),
        ("post", f"/v1/jobs/{job.job_id}/status", {"status": "running"}),
        (
            "post",
            f"/v1/jobs/{job.job_id}/result",
            {
                "schema_version": "1.0",
                "job_id": job.job_id,
                "idempotency_key": "res-12345678",
                "outcome": "succeeded",
                "event_ids": [],
            },
        ),
    ]
    for method, path, body in probes:
        kw = {"json": body} if body is not None else {}
        foreign = getattr(lab.c, method)(path, headers=b, **kw)
        assert foreign.status_code == 404, (path, foreign.text)
        assert foreign.json() == {"error": "not_found", "detail": "unknown id"}
        unknown_path = (
            path.replace(sid, "sess-doesnotexist")
            .replace(run, "run-doesnotexist")
            .replace(job.job_id, "job-doesnotexist")
        )
        unknown = getattr(lab.c, method)(unknown_path, headers=b, **kw)
        assert unknown.status_code == 404 and unknown.json() == foreign.json()
    # device B never received A's job
    assert lab.poll(b) == []
    # the owner sees everything
    assert lab.c.get(f"/v1/runs/{run}", headers=lab.owner).status_code == 200
    assert lab.c.get(f"/v1/sessions/{sid}/report", headers=lab.owner).status_code == 200


def test_unknown_route_uses_error_shape(lab):
    r = lab.c.get("/v1/nothing-here")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
