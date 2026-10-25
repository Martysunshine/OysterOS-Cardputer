"""No command surface, exact route set, own database, idempotent start-up and install."""

from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path

import oyster.companion as companion_pkg
import oyster.companion.api as api
import oyster.companion.models as models
import pytest
from _companion_support import OWNER, Clock, Rig
from fastapi.testclient import TestClient
from oyster.companion.adapters import HostAdapter, MockHostAdapter
from oyster.companion.mount import install
from oyster.companion.store import CompanionStore
from pydantic import BaseModel

EXEC_LIKE = re.compile(r"cmd|command|shell|exec|script|argv|args|sudo|run|invoke|eval|popen|system", re.I)
PACKAGE_DIR = Path(companion_pkg.__file__).parent

INTENDED_ROUTES = {
    ("GET", "/v1/hosts"),
    ("GET", "/v1/hosts/{host_id}/health"),
    ("POST", "/v1/pair"),
    ("POST", "/v1/admin/hosts"),
    ("GET", "/v1/admin/hosts"),
    ("DELETE", "/v1/admin/hosts/{host_id}"),
    ("POST", "/v1/admin/pairing"),
    ("GET", "/v1/admin/audit"),
}
DEVICE_APP_ROUTES = {r for r in INTENDED_ROUTES if "/admin/" not in r[1]}


def companion_routes(app) -> set[tuple[str, str]]:
    schema = app.openapi()
    return {
        (method.upper(), path)
        for path, ops in schema["paths"].items()
        for method, op in ops.items()
        if "companion" in op.get("tags", [])
    }


# --- no command surface --------------------------------------------------------------------------------
def test_the_adapter_protocol_has_only_typed_reads():
    members = {n for n in dir(HostAdapter) if not n.startswith("_")}
    assert members == {"describe", "health"}
    assert list(inspect.signature(HostAdapter.health).parameters) == ["self", "metrics"]
    assert inspect.signature(HostAdapter.health).parameters["metrics"].annotation == "frozenset[str]"
    assert not [m for m in members if EXEC_LIKE.search(m)]


def test_the_mock_adapter_adds_no_callable_surface():
    public = [n for n in dir(MockHostAdapter) if not n.startswith("_")]
    callables = {n for n in public if callable(getattr(MockHostAdapter, n))}
    assert callables == {"describe", "health"}
    assert MockHostAdapter().describe().read_only is True


def _models() -> list[type[BaseModel]]:
    classes = (m for _, m in inspect.getmembers(models, inspect.isclass))
    return [m for m in classes if issubclass(m, BaseModel) and m is not BaseModel]


def test_every_model_forbids_extra_fields_and_has_no_command_like_field():
    found = _models()
    assert len(found) > 10
    for model in found:
        if model.__module__ != models.__name__:
            continue
        assert model.model_config.get("extra") == "forbid", model.__name__
        bad = [f for f in model.model_fields if EXEC_LIKE.search(f)]
        assert not bad, (model.__name__, bad)


@pytest.mark.parametrize(
    "extra", [{"command": "id"}, {"cmd": "id"}, {"exec": ["ls"]}, {"script": "x"}, {"args": []}]
)
def test_no_request_accepts_a_command_field(rig: Rig, extra):
    host = {"host_id": "h", "display_name": "H", "metrics": ["cpu_percent"], "device_ids": []}
    assert rig.c.post("/v1/admin/hosts", headers=OWNER, json={**host, **extra}).status_code == 422
    assert rig.c.post("/v1/admin/pairing", headers=OWNER, json=extra).status_code == 422
    assert rig.c.post("/v1/pair", json={"code": "ABCDEFGH", "device_id": "d", **extra}).status_code == 422


def test_the_package_never_runs_programs_or_evaluates_input():
    banned = re.compile(
        r"\bsubprocess\b|\bos\.(system|popen|exec\w*|spawn\w*)\b|\bPopen\b|\beval\(|\bexec\(|shell\s*=\s*True"
        r"|create_subprocess|\bpty\b|\bparamiko\b|\bpexpect\b|__import__|importlib"
    )
    for path in PACKAGE_DIR.glob("*.py"):
        assert not banned.search(path.read_text(encoding="utf-8")), path.name


# --- routes -----------------------------------------------------------------------------------------------
def test_openapi_lists_exactly_the_intended_routes(rig: Rig, device_rig: Rig):
    assert companion_routes(rig.app) == INTENDED_ROUTES
    assert companion_routes(device_rig.app) == DEVICE_APP_ROUTES


def test_the_existing_health_route_is_not_redefined(rig: Rig):
    paths = rig.app.openapi()["paths"]
    assert list(paths["/v1/health"]) == ["get"]
    assert "companion" not in paths["/v1/health"]["get"].get("tags", [])
    ours = [r.path for router in (api.device_router, api.admin_router) for r in router.routes]
    assert "/v1/health" not in ours


# --- storage and start-up ---------------------------------------------------------------------------------
def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_the_companion_has_its_own_database_file(rig: Rig):
    rig.host()
    main, own = rig.settings.db_path, rig.settings.data_dir / "companion.db"
    assert main != own and own.exists()
    assert {"hosts", "pairing", "audit", "meta"} <= tables(own)
    assert not {"hosts", "pairing", "audit"} & tables(main)
    assert "devices" not in tables(own)  # device credentials stay in the main store


def test_start_up_creates_tables_idempotently_and_keeps_data(tmp_path: Path, clock: Clock):
    rig = Rig(tmp_path, clock)
    try:
        with TestClient(rig.app):  # lifespan start-up opens the store
            rig.host("host-1")
        assert rig.companion._store is None  # shut-down closed it
        with TestClient(rig.app):
            hosts = rig.c.get("/v1/admin/hosts", headers=OWNER).json()["hosts"]
            assert [h["host_id"] for h in hosts] == ["host-1"]
        path = tmp_path / "companion.db"
        for _ in range(2):
            CompanionStore(path).close()
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchall() == [("1",)]
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
            assert len(names) == len(set(names)) == 2
    finally:
        rig.close()


def test_the_store_opens_lazily_without_a_lifespan(rig: Rig):
    assert rig.companion._store is None
    rig.host()
    assert rig.companion._store is not None


def test_the_original_lifespan_still_runs(tmp_path: Path, clock: Clock):
    rig = Rig(tmp_path, clock)
    try:
        with TestClient(rig.app) as client:
            assert rig.app.state.runtime._users == 1  # the main application's own start-up ran
            assert client.get("/v1/health").json() == {"status": "ok", "api": "v1"}
        assert rig.app.state.runtime._users == 0
    finally:
        rig.close()


def test_install_is_idempotent(rig: Rig):
    before = len(rig.app.routes)
    again = install(rig.app)
    assert again is rig.companion
    assert len(rig.app.routes) == before
    assert companion_routes(rig.app) == INTENDED_ROUTES
