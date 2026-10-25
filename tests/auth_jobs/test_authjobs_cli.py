"""`oyster` CLI: direct and HTTP owner paths, token handling, the AI-command hook."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from oyster import cli
from oyster.db.sqlite_store import SqliteStore
from oyster.main import create_app
from oyster.settings import REPO_DIR, load_settings

ADMIN = "cli-admin-token-0123456789abcdef"
TARGET_FILE = REPO_DIR / "schemas" / "examples" / "target.lab-01.json"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OYSTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OYSTER_ADMIN_TOKEN", ADMIN)
    return tmp_path


def test_init_db_enroll_direct_and_token_file(env, capsys):
    assert cli.main(["init-db"]) == 0
    tok_file = env / "sim.token"
    assert (
        cli.main(
            ["enroll-device", "--id", "sim-01", "--simulated", "--direct", "--write-token", str(tok_file)]
        )
        == 0
    )
    out = capsys.readouterr()
    token = tok_file.read_text().strip()
    assert token and token not in out.out + out.err
    store = SqliteStore(env / "oyster.db")
    try:
        dev = store.device_for_token(token)
        assert dev is not None and dev.device_id == "sim-01" and dev.simulated
    finally:
        store.close()
    # without --write-token the token goes to stdout (once) and nothing else does
    assert cli.main(["enroll-device", "--id", "sim-02", "--direct"]) == 0
    out = capsys.readouterr()
    assert len(out.out.strip().splitlines()) == 1 and "enrolled" in out.err


def test_owner_commands_over_http(env, monkeypatch, capsys):
    settings = load_settings()
    app = create_app(settings)

    def fake_client(s, base_url=None):
        return TestClient(app, headers={"Authorization": f"Bearer {s.admin_token}"})

    monkeypatch.setattr(cli, "owner_client", fake_client)
    assert cli.main(["enroll-target", "--file", str(TARGET_FILE)]) == 0
    assert cli.main(["list-targets"]) == 0
    assert "target-lab-01" in capsys.readouterr().out
    assert cli.main(["enroll-device", "--id", "dev-http"]) == 0
    token = capsys.readouterr().out.strip()
    c = TestClient(app)
    assert c.get("/v1/device/jobs", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    sid = c.post(
        "/v1/sessions", headers={"Authorization": f"Bearer {token}"}, json={"source_mode": "live"}
    ).json()["session_id"]
    assert cli.main(["export-report", "--session", sid, "--format", "json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["session_id"] == sid and rep["source_modes"] == ["live"]
    assert cli.main(["sessions"]) == 0
    assert sid in capsys.readouterr().out
    # HTTP errors surface as a clean CLI error
    assert cli.main(["export-report", "--session", "sess-missing"]) == 2
    assert "HTTP 404" in capsys.readouterr().err


def test_unreachable_backend_and_missing_admin_token(env, monkeypatch, capsys):
    assert cli.main(["list-targets", "--backend", "http://127.0.0.1:9"]) == 2
    assert "not reachable" in capsys.readouterr().err
    monkeypatch.setenv("OYSTER_ADMIN_TOKEN", "")
    assert cli.main(["list-targets"]) == 2
    assert "OYSTER_ADMIN_TOKEN" in capsys.readouterr().err


def test_serve_refuses_placeholder_admin_token(env, monkeypatch, capsys):
    monkeypatch.setenv("OYSTER_ADMIN_TOKEN", "change-me-to-a-long-random-string")
    assert cli.main(["serve"]) == 2
    assert "OYSTER_ADMIN_TOKEN" in capsys.readouterr().err


def test_link_baseline_and_report_direct(env, capsys):
    store = SqliteStore(env / "oyster.db")
    store.create_device("d1", "d1", True)
    a, _ = store.register_session("d1", None, "simulated", [], None)
    b, _ = store.register_session("d1", None, "simulated", [], None)
    store.close()
    assert cli.main(["link-baseline", "--session", b.session_id, "--baseline", a.session_id]) == 0
    assert cli.main(["link-baseline", "--session", b.session_id, "--baseline", b.session_id]) == 2
    out_file = env / "r.md"
    assert cli.main(["export-report", "--session", b.session_id, "--direct", "--out", str(out_file)]) == 0
    text = out_file.read_text(encoding="utf-8")
    assert "SIMULATED DATA" in text and a.session_id in text


def test_validate_schemas_command(capsys):
    assert cli.main(["validate-schemas"]) == 0
    assert "0 failure(s)" in capsys.readouterr().out


def test_ai_commands_registered_or_reported_unavailable(capsys):
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert {"ai-check", "ask"} <= set(sub.choices)
    if cli._ai_register is None:
        assert cli.main(["ai-check"]) == 1
        assert "unsupported" in capsys.readouterr().err
    expected = {
        "init-db",
        "serve",
        "make-dev-ca",
        "enroll-device",
        "enroll-target",
        "list-targets",
        "sessions",
        "provision-device",
        "export-report",
        "validate-schemas",
    }
    assert expected <= set(sub.choices)


def test_serve_listeners_keep_device_connections_alive(env, capsys):
    """The device polls every 4 s over one TLS connection; uvicorn's 5 s default would drop it."""
    settings = load_settings()
    settings.tls_cert.parent.mkdir(parents=True, exist_ok=True)
    settings.tls_cert.write_text("placeholder", encoding="utf-8")  # existence decides the HTTPS listener
    settings.tls_key.write_text("placeholder", encoding="utf-8")
    app = create_app(settings)
    configs = cli.server_configs(app, settings)
    assert [c.host for c in configs] == ["127.0.0.1", settings.bind_host]
    assert configs[1].ssl_certfile == str(settings.tls_cert)
    assert all(c.timeout_keep_alive >= 30 for c in configs)
    only_http = cli.server_configs(app, settings, http_only=True)
    assert len(only_http) == 1 and only_http[0].timeout_keep_alive >= 30
