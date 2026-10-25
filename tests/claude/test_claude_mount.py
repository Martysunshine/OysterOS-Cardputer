"""Wiring: install(app) is idempotent, both listeners share one runtime, the lifespan starts and stops it,
and the link has its own database file."""

from __future__ import annotations

import pytest
from _claude_support import make_settings
from fastapi.testclient import TestClient
from oyster.claude import mount
from oyster.claude.mount import ClaudeRuntime, install
from oyster.db.sqlite_store import SqliteStore
from oyster.deck.mount import DeckRuntime
from oyster.main import create_app


def claude_paths(app) -> list[tuple[str, str]]:
    """(METHOD, path) of every Claude route, from the generated schema (route objects vary by version)."""
    schema = app.openapi()["paths"]
    return sorted((m.upper(), p) for p, ops in schema.items() if p.startswith("/v1/claude") for m in ops)


def test_install_twice_changes_nothing(kit):
    before = claude_paths(kit.app)
    again = install(kit.app)
    assert again is kit.runtime and claude_paths(kit.app) == before
    assert len(before) == 6


def test_create_app_installs_the_link_and_the_loopback_app_gets_the_owner_routes(kit):
    assert kit.app.state.claude is kit.runtime
    assert ("POST", "/v1/claude/approvals") in claude_paths(kit.app)


def test_the_device_app_gets_only_the_device_routes(kit):
    lan = create_app(kit.settings, store=kit.core, worker=None, audience="device", deck_runtime=kit.deck)
    assert lan.state.claude is kit.runtime  # one runtime for both listeners
    assert claude_paths(lan) == [
        ("GET", "/v1/claude/approvals"),
        ("GET", "/v1/claude/status"),
        ("POST", "/v1/claude/approvals/{approval_id}/decide"),
    ]


def test_the_runtime_is_shared_per_data_directory(tmp_path):
    settings = make_settings(tmp_path / "shared")
    core = SqliteStore(":memory:")
    deck = DeckRuntime(tmp_path, synchronous="OFF", autostart=False)
    try:
        a = create_app(settings, store=core, worker=None, deck_runtime=deck)
        b = create_app(settings, store=core, worker=None, audience="device", deck_runtime=deck)
        assert a.state.claude is b.state.claude
        other = create_app(make_settings(tmp_path / "other"), store=core, worker=None, deck_runtime=deck)
        assert other.state.claude is not a.state.claude
    finally:
        mount.forget(tmp_path / "shared")
        mount.forget(tmp_path / "other")
        deck.close()
        core.close()


def test_the_link_has_its_own_database_file(kit):
    assert (kit.tmp_path / "claude.db").is_file()
    assert (kit.tmp_path / "deck.db").is_file() and kit.runtime.store.path != str(kit.tmp_path / "deck.db")


def test_the_lifespan_stops_the_runtime_and_ends_pending_approvals(kit):
    a = kit.service.create("s", "Bash", "ls", 90)
    with TestClient(kit.app):  # lifespan start ... stop
        assert kit.service.poll(a.approval_id) == "pending"
    assert kit.service.poll(a.approval_id) == "expired"
    assert "approval.expired" in kit.events(a.approval_id)


def test_start_and_stop_are_reference_counted(tmp_path):
    rt = ClaudeRuntime(tmp_path / "rc", synchronous="OFF")
    try:
        a = rt.service.create("s", "Bash", "ls", 90)
        rt.start()
        rt.start()
        rt.stop()
        assert rt.service.poll(a.approval_id) == "pending"  # one user is left
        rt.stop()
        assert rt.service.poll(a.approval_id) == "expired"
        rt.stop()  # an extra stop is harmless
    finally:
        rt.close()


def test_adopt_replaces_and_closes_the_previous_runtime(tmp_path):
    first = ClaudeRuntime(tmp_path / "ad", synchronous="OFF")
    second = ClaudeRuntime(tmp_path / "ad", synchronous="OFF")
    try:
        mount.adopt(first)
        assert mount.shared_runtime(tmp_path / "ad") is first
        mount.adopt(second)
        assert mount.shared_runtime(tmp_path / "ad") is second
        with pytest.raises(Exception):  # noqa: B017 - sqlite3.ProgrammingError: the old connection is closed
            first.store.newest_status()
    finally:
        mount.forget(tmp_path / "ad")
