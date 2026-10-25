"""Fixtures for Control Deck tests: an in-process app over temporary SQLite files, a controllable clock,
deterministic job ids and nonces, and the MockExecutor (no network, no background threads unless a test
starts the runner)."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from oyster.db.sqlite_store import SqliteStore
from oyster.deck.actions import MockExecutor
from oyster.deck.jobs import DeckConfig
from oyster.deck.mount import DeckRuntime
from oyster.deck.profiles import Profile
from oyster.main import create_app
from oyster.settings import Settings

ADMIN = "test-admin-token-0123456789abcdef"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make_settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        bind_host="127.0.0.1",
        https_port=8443,
        http_port=8000,
        admin_token=ADMIN,
        tls_cert=data_dir / "dev-ca" / "server.crt",
        tls_key=data_dir / "dev-ca" / "server.key",
        ai_provider="groq",
        ai_base_url="https://ai.invalid/v1",
        ai_model="",
        ai_api_key="",
        ai_timeout_s=5,
        ai_max_input_tokens=12000,
        ai_max_output_tokens=1500,
        ai_price_input_per_mtok=None,
        ai_price_output_per_mtok=None,
    )


def default_profile(**overrides) -> dict:
    """Slots: 1 noop, 2 health check, 3 restart (impactful), 4 wake (impactful), 5 status then restart
    (impactful), 6 two read-only steps, 7 health check then status."""
    p = {
        "profile_id": "dev",
        "name": "Dev stack",
        "device_ids": ["dev-a"],
        "hosts": {"h1": {"services": ["nginx", "db"]}, "h2": {"services": []}},
        "slots": {
            "1": {"label": "Ping", "action": {"type": "noop"}},
            "2": {"label": "Health", "action": {"type": "host_health_check", "host_id": "h1"}},
            "3": {
                "label": "Restart nginx",
                "action": {"type": "service_restart", "host_id": "h1", "service": "nginx"},
            },
            "4": {"label": "Wake h2", "action": {"type": "wake_on_lan", "host_id": "h2"}},
            "5": {
                "label": "Status+restart",
                "actions": [
                    {"type": "service_status", "host_id": "h1", "service": "nginx"},
                    {"type": "service_restart", "host_id": "h1", "service": "nginx"},
                ],
            },
            "6": {
                "label": "Two checks",
                "actions": [
                    {"type": "host_health_check", "host_id": "h1"},
                    {"type": "service_status", "host_id": "h1", "service": "db"},
                ],
            },
        },
    }
    p.update(overrides)
    return p


class DeckKit:
    """Driver around the TestClient and the deck runtime for readable tests."""

    def __init__(self, tmp_path: Path, config: DeckConfig | None = None, autostart: bool = False):
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.settings = make_settings(tmp_path)
        self.config = config
        self.autostart = autostart
        self.executor = MockExecutor()
        self._ids = itertools.count(1)
        self._nonces = itertools.count(1)
        self.runtime = self._runtime(self.executor)
        self.core = SqliteStore(":memory:", clock=self.clock)  # device tokens only
        self.app = create_app(self.settings, store=self.core, worker=None, deck_runtime=self.runtime)
        self.client = TestClient(self.app)
        self.owner = {"Authorization": f"Bearer {ADMIN}"}
        self.tokens: dict[str, str] = {}

    def _runtime(self, executor: MockExecutor) -> DeckRuntime:
        return DeckRuntime(
            self.tmp_path,
            executor=executor,
            clock=self.clock,
            config=self.config,
            new_id=lambda: f"dj-{next(self._ids):016x}",
            new_nonce=lambda: f"nonce-{next(self._nonces):08d}-abcdef",
            autostart=self.autostart,
            synchronous="OFF",  # durability is not under test here; test_deck_restart checks the default
        )

    @property
    def service(self):
        return self.runtime.service

    def restart(self) -> MockExecutor:
        """Simulate a crash: drop the runtime without finishing anything and open a fresh one on the same
        files. Returns the new runtime's (empty) executor."""
        self.runtime.store.close()
        self.executor = MockExecutor()
        self.runtime = self._runtime(self.executor)
        self.app = create_app(self.settings, store=self.core, worker=None, deck_runtime=self.runtime)
        self.client = TestClient(self.app)
        return self.executor

    def close(self) -> None:
        self.runtime.close()
        self.core.close()

    # --- setup helpers ---
    def enroll(self, device_id: str) -> dict:
        """Enrol (or re-enrol, rotating the token) a device straight in the core store; the admin
        endpoint itself is covered by the existing API tests."""
        self.tokens[device_id] = self.core.create_device(device_id, device_id, True)
        return {"Authorization": f"Bearer {self.tokens[device_id]}"}

    def put_profile(self, profile: dict | None = None, expect: int = 200):
        profile = profile or default_profile()
        r = self.client.put(f"/v1/deck/profiles/{profile['profile_id']}", headers=self.owner, json=profile)
        assert r.status_code == expect, r.text
        return r

    # --- device calls ---
    def run(self, headers: dict, slot: int = 1, key: str = "key-00000001", profile: str = "dev", **kw):
        h = {**headers, "Idempotency-Key": key} if key is not None else dict(headers)
        return self.client.post(f"/v1/deck/profiles/{profile}/slots/{slot}/run", headers=h, **kw)

    def job(self, headers: dict, job_id: str):
        return self.client.get(f"/v1/deck/jobs/{job_id}", headers=headers)

    def confirm(
        self, headers: dict, job_id: str, nonce: str, approved: bool = True, body_job_id=None, **extra
    ):
        body = {"job_id": body_job_id or job_id, "nonce": nonce, "approved": approved, **extra}
        return self.client.post(f"/v1/deck/jobs/{job_id}/confirm", headers=headers, json=body)

    def cancel(self, headers: dict, job_id: str):
        return self.client.post(f"/v1/deck/jobs/{job_id}/cancel", headers=headers)

    def history(self, headers: dict, **params):
        return self.client.get("/v1/deck/history", headers=headers, params=params)

    def drain(self) -> int:
        return self.runtime.runner.run_pending()

    def run_and_finish(self, headers: dict, slot: int, key: str) -> dict:
        """Run a non-impactful slot to completion and return the final job."""
        r = self.run(headers, slot, key)
        assert r.status_code == 200, r.text
        self.drain()
        return self.job(headers, r.json()["job_id"]).json()

    def run_confirmed(self, headers: dict, slot: int, key: str, approved: bool = True) -> dict:
        """Run an impactful slot, confirm it, drain the runner and return the final job."""
        r = self.run(headers, slot, key)
        assert r.status_code == 200, r.text
        j = r.json()
        c = self.confirm(headers, j["job_id"], j["nonce"], approved)
        assert c.status_code == 200, c.text
        self.drain()
        return self.job(headers, j["job_id"]).json()

    def events(self, job_id: str) -> list[str]:
        with self.runtime.store.tx() as conn:
            rows = conn.execute("SELECT event FROM audit WHERE job_id=? ORDER BY seq", (job_id,)).fetchall()
        return [r[0] for r in rows]

    def audit_rows(self) -> list[tuple]:
        with self.runtime.store.tx() as conn:
            return conn.execute("SELECT * FROM audit ORDER BY seq").fetchall()


@pytest.fixture
def kit_factory(tmp_path: Path):
    made: list[DeckKit] = []

    def make(config: DeckConfig | None = None, autostart: bool = False) -> DeckKit:
        k = DeckKit(tmp_path / f"k{len(made)}", config, autostart)
        made.append(k)
        return k

    yield make
    for k in made:
        k.close()


@pytest.fixture
def kit(kit_factory) -> DeckKit:
    k = kit_factory()
    k.dev_a = k.enroll("dev-a")
    k.dev_b = k.enroll("dev-b")
    k.service.put_profile(Profile.model_validate(default_profile()))
    return k


@pytest.fixture
def make_profile():
    return default_profile
