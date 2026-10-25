"""Helpers for the companion tests: fake settings, a controllable clock, and a driver around the TestClient.

No network, no environment, no real secrets: every value here is made up for the tests.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from oyster.companion.adapters import AdapterReading, MockHostAdapter
from oyster.companion.mount import Companion, install
from oyster.db.sqlite_store import SqliteStore
from oyster.main import create_app
from oyster.settings import Settings

ADMIN = "test-admin-token-0123456789abcdef"
OWNER = {"Authorization": f"Bearer {ADMIN}"}
BAD = {"Authorization": "Bearer " + "invalid"}


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


class BlockingAdapter:
    """An adapter that never answers until released (exercises the API's own deadline)."""

    def __init__(self):
        self.release = threading.Event()

    def describe(self):
        return MockHostAdapter().describe()

    def health(self, metrics):
        self.release.wait(timeout=10)
        return AdapterReading()


class BrokenAdapter:
    """An adapter that fails with an error whose text must never reach a client."""

    SECRET = "ssh-password-hunter2"

    def describe(self):
        return MockHostAdapter().describe()

    def health(self, metrics):
        raise RuntimeError(f"connection failed with {self.SECRET}")


class Rig:
    """One in-process app (loopback or device audience) with the companion installed."""

    def __init__(self, tmp_path: Path, clock: Clock, audience: str = "loopback", **install_kw):
        self.clock = clock
        self.settings = make_settings(tmp_path)
        self.store = SqliteStore(self.settings.db_path, clock=clock)
        self.adapters: dict[str, object] = {}
        options = {
            "rng": random.Random(1234),
            "adapter_factory": lambda host: self.adapters.setdefault(host.host_id, MockHostAdapter()),
            **install_kw,
        }
        self.app = create_app(
            self.settings, store=self.store, worker=None, audience=audience, companion_options=options
        )
        self.companion: Companion = install(self.app)  # returns the installation create_app made
        self.c = TestClient(self.app)

    def close(self) -> None:
        self.companion.close()
        self.store.close()

    # --- driver helpers ---
    def device(self, device_id: str = "dev-1") -> dict:
        """Enrol a device straight in the main store (the device app has no admin routes)."""
        return {"Authorization": f"Bearer {self.store.create_device(device_id, device_id, True)}"}

    def host(self, host_id: str = "host-1", **over) -> dict:
        body = {
            "host_id": host_id,
            "display_name": "Test host",
            "adapter": "mock",
            "metrics": ["cpu_percent", "load_1m", "mem_used_percent", "disk_used_percent", "uptime_s"],
            "services": [],
            "device_ids": ["dev-1"],
        }
        body.update(over)
        r = self.c.post("/v1/admin/hosts", headers=OWNER, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def new_code(self, **body) -> dict:
        r = self.c.post("/v1/admin/pairing", headers=OWNER, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def redeem(self, code: str, device_id: str = "dev-9", **extra):
        return self.c.post("/v1/pair", json={"code": code, "device_id": device_id, **extra})

    def audit(self, limit: int = 200) -> list[dict]:
        r = self.c.get(f"/v1/admin/audit?limit={limit}", headers=OWNER)
        assert r.status_code == 200, r.text
        return r.json()["entries"]

    def raw_companion_db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.settings.data_dir / "companion.db")
