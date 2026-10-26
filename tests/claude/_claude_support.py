"""Support code for the Claude Code link tests: an in-process app over temporary SQLite files, a controllable
clock, deterministic ids and nonces, and helpers to run the hook's long poll on a thread (no network)."""

from __future__ import annotations

import importlib.util
import itertools
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient
from oyster.claude import mount
from oyster.claude.mount import ClaudeRuntime
from oyster.claude.service import ClaudeConfig
from oyster.db.sqlite_store import SqliteStore
from oyster.deck.mount import DeckRuntime
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
        ai_max_context_events=40,
        ai_max_output_tokens=1500,
        ai_price_input_per_mtok=None,
        ai_price_output_per_mtok=None,
    )


class Waiter:
    """A hook request running on a thread: the long poll blocks until the test answers it."""

    def __init__(self, client: TestClient, headers: dict, body: dict):
        self.response = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(client, headers, body), daemon=True)
        self._thread.start()

    def _run(self, client: TestClient, headers: dict, body: dict) -> None:
        try:
            self.response = client.post("/v1/claude/approvals", headers=headers, json=body)
        except BaseException as e:  # noqa: BLE001 - surfaced by result()
            self.error = e

    def result(self, timeout: float = 15.0):
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the long poll did not finish"
        if self.error is not None:
            raise self.error
        return self.response


class ClaudeKit:
    """Driver around the TestClient and the link's runtime for readable tests."""

    def __init__(self, tmp_path: Path, config: ClaudeConfig | None = None, real_time: bool = False):
        self.tmp_path = tmp_path
        self.clock = time.time if real_time else Clock()
        self.settings = make_settings(tmp_path)
        self.config = config or ClaudeConfig(poll_interval_s=0.01)
        self._ids = itertools.count(1)
        self._nonces = itertools.count(1)
        self.runtime = self._runtime()
        self.core = SqliteStore(":memory:", clock=self.clock)  # device tokens only
        # the deck is not under test: a cheap runtime keeps each app creation fast and closable
        self.deck = DeckRuntime(tmp_path, synchronous="OFF", autostart=False)
        self.app = create_app(self.settings, store=self.core, worker=None, deck_runtime=self.deck)
        self.client = TestClient(self.app)
        self.owner = {"Authorization": f"Bearer {ADMIN}"}
        self.tokens: dict[str, str] = {}

    def _runtime(self) -> ClaudeRuntime:
        rt = ClaudeRuntime(
            self.tmp_path,
            clock=self.clock,
            config=self.config,
            new_id=lambda: f"appr-{next(self._ids):016x}",
            new_nonce=lambda: f"nonce-{next(self._nonces):08d}-abcdef",
            synchronous="OFF",  # durability is not under test here
        )
        return mount.adopt(rt)

    @property
    def service(self):
        return self.runtime.service

    def device_app(self) -> TestClient:
        """The LAN-facing app (device audience) over the same runtime and store."""
        app = create_app(
            self.settings, store=self.core, worker=None, audience="device", deck_runtime=self.deck
        )
        return TestClient(app)

    def restart(self) -> None:
        """Simulate a crash: drop the runtime and open a fresh one on the same file."""
        self.runtime.close()
        mount.forget(self.tmp_path)
        self.runtime = self._runtime()
        self.app = create_app(self.settings, store=self.core, worker=None, deck_runtime=self.deck)
        self.client = TestClient(self.app)

    def close(self) -> None:
        mount.forget(self.tmp_path)
        self.deck.close()
        self.core.close()

    # --- setup helpers ---
    def enroll(self, device_id: str) -> dict:
        """Enrol (or re-enrol, rotating the token) a device straight in the core store."""
        self.tokens[device_id] = self.core.create_device(device_id, device_id, True)
        return {"Authorization": f"Bearer {self.tokens[device_id]}"}

    # --- hook side ---
    @staticmethod
    def body(
        summary: str = "npm test", tool: str = "Bash", session: str = "sess-1", wait_s: int = 90
    ) -> dict:
        return {"session": session, "tool": tool, "summary": summary, "wait_s": wait_s}

    def hook(self, **kw) -> Waiter:
        return Waiter(self.client, self.owner, self.body(**kw))

    def wait_for_pending(self, n: int = 1, timeout: float = 10.0) -> None:
        """Block until `n` approvals are waiting (the hook threads have created them)."""
        deadline = threading.Event()
        for _ in range(int(timeout / 0.01)):
            with self.runtime.store.tx() as conn:
                if self.runtime.store.count_pending(conn, self.clock()) >= n:
                    return
            deadline.wait(0.01)
        raise AssertionError(f"fewer than {n} pending approval(s) after {timeout} s")

    # --- device side ---
    def listing(self, headers: dict) -> list[dict]:
        r = self.client.get("/v1/claude/approvals", headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["approvals"]

    def decide(self, headers: dict, approval_id: str, nonce: str, decision: str = "allow", **extra):
        body = {"decision": decision, "nonce": nonce, **extra}
        return self.client.post(f"/v1/claude/approvals/{approval_id}/decide", headers=headers, json=body)

    def pending_item(self, headers: dict) -> dict:
        items = self.listing(headers)
        assert len(items) >= 1
        return items[0]

    def audit_rows(self) -> list[tuple]:
        with self.runtime.store.tx() as conn:
            return conn.execute("SELECT * FROM audit ORDER BY seq").fetchall()

    def events(self, approval_id: str | None = None) -> list[str]:
        with self.runtime.store.tx() as conn:
            if approval_id is None:
                rows = conn.execute("SELECT event FROM audit ORDER BY seq").fetchall()
            else:
                rows = conn.execute(
                    "SELECT event FROM audit WHERE approval_id=? ORDER BY seq", (approval_id,)
                ).fetchall()
        return [r[0] for r in rows]


# --- PC-side scripts -------------------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "integrations" / "claude-code"


def load_script(name: str):
    """Import integrations/claude-code/<name>.py (the directory name has a hyphen, so no normal import)."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeBackend:
    """A loopback HTTP server that records POSTs and answers from `reply(path, body) -> (status, payload)`."""

    def __init__(self, reply=None):
        self.requests: list[dict] = []
        self.reply = reply or (
            lambda path, body: (200, {"decision": "timeout", "approval_id": "appr-0123456789abcdef"})
        )
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = None
                outer.requests.append(
                    {"path": self.path, "auth": self.headers.get("Authorization"), "body": body, "raw": raw}
                )
                status, payload = outer.reply(self.path, body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def closed_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
