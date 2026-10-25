"""Support for the Smart Home tests: a fake Home Assistant (httpx.MockTransport handler), a controllable clock
and a kit that builds an in-process app over a temporary data directory. Nothing here talks to a network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from oyster.db.sqlite_store import SqliteStore
from oyster.home.config import HomeConfig
from oyster.home.mount import HomeRuntime
from oyster.main import create_app
from oyster.settings import Settings

ADMIN = "test-admin-token-0123456789abcdef"
FAKE_TOKEN = (
    "fake-ha-token-0123456789abcdefghijklmnop"  # must never appear in a response, a log line or a file
)
HA_URL = "http://192.168.1.50:8123"
REPO = Path(__file__).resolve().parents[2]
EXAMPLE_ALLOWLIST = REPO / "examples" / "home_allowlist.example.json"

DEFAULT_STATES = {
    "light.living_ceiling": "on",
    "switch.living_fan": "off",
    "cover.living_blinds": "closed",
    "lock.front_door": "locked",
    "light.hall_lamp": "off",
    "switch.office_heater": "off",
    "scene.office_focus": "2026-10-25T12:00:00+00:00",
}
_SET = {
    "turn_on": "on",
    "turn_off": "off",
    "lock": "locked",
    "unlock": "unlocked",
    "open_cover": "open",
    "close_cover": "closed",
    "alarm_arm_away": "armed_away",
}


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeHA:
    """A stand-in for the Home Assistant REST API. `fail` makes every request (or those under `fail_on`)
    fail in one of the ways a real instance or network can."""

    def __init__(self, states: dict[str, str] | None = None):
        self.states = dict(DEFAULT_STATES if states is None else states)
        self.requests: list[httpx.Request] = []
        self.fail: str | None = None
        self.fail_on: str | None = None
        self.empty_service_answer = False
        self.token = FAKE_TOKEN
        self.on_request = None

    @property
    def service_calls(self) -> list[tuple[str, str, str]]:
        out = []
        for r in self.requests:
            if r.method == "POST" and r.url.path.startswith("/api/services/"):
                _, _, _, domain, service = r.url.path.split("/")
                out.append((domain, service, json.loads(r.content)["entity_id"]))
        return out

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        if self.fail and (self.fail_on is None or request.url.path.startswith(self.fail_on)):
            return self._failure(request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "401: Unauthorized"})
        path = request.url.path
        if request.method == "GET" and path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if request.method == "GET" and path.startswith("/api/states/"):
            eid = path.rsplit("/", 1)[1]
            if eid not in self.states:
                return httpx.Response(404, json={"message": "Entity not found."})
            return httpx.Response(200, json={"entity_id": eid, "state": self.states[eid], "attributes": {}})
        if request.method == "POST" and path.startswith("/api/services/"):
            _, _, _, domain, service = path.split("/")
            body = json.loads(request.content or b"{}")
            eid = body.get("entity_id")
            if not isinstance(eid, str):
                return httpx.Response(400, json={"message": "missing entity_id"})
            if eid not in self.states or self.empty_service_answer:
                return httpx.Response(200, json=[])
            if service == "toggle":
                now = self.states[eid]
                self.states[eid] = {"on": "off", "off": "on", "open": "closed", "closed": "open"}.get(
                    now, now
                )
            elif service in _SET and domain != "scene":
                self.states[eid] = _SET[service]
            return httpx.Response(200, json=[{"entity_id": eid, "state": self.states[eid], "attributes": {}}])
        return httpx.Response(404, json={"message": "404: Not Found"})

    def _failure(self, request: httpx.Request) -> httpx.Response:
        leak = f"{HA_URL} bearer {FAKE_TOKEN}"  # what a careless library message could contain
        match self.fail:
            case "down":
                raise httpx.ConnectError(f"[Errno 111] connection refused {leak}")
            case "timeout":
                raise httpx.ReadTimeout(f"timed out {leak}")
            case "tls":
                raise httpx.ConnectError(f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed {leak}")
            case "401":
                return httpx.Response(401, json={"message": f"401: Unauthorized {leak}"})
            case "403":
                return httpx.Response(403, json={"message": "403"})
            case "400":
                return httpx.Response(400, json={"message": f"bad request {leak}"})
            case "500":
                return httpx.Response(500, text=f"Internal Server Error {leak}")
            case "redirect":
                return httpx.Response(302, headers={"location": "http://203.0.113.9/steal"})
            case "huge":
                return httpx.Response(200, content=b'{"state": "on", "pad": "' + b"x" * 70_000 + b'"}')
            case "garbage":
                return httpx.Response(200, content=b"<html>not json</html>")
        raise AssertionError(f"unknown failure mode {self.fail}")


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


class Mono:
    """Monotonic clock for the state time budget: every read can advance it."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.step = 0.0

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class HomeKit:
    """App + fake Home Assistant + two enrolled devices. `allowlist` is a dict (written as the allowlist
    file), the text of a file, or None for the shipped example; `url`/`token` empty = not configured."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        allowlist: object = "example",
        url: str = HA_URL,
        token: str = FAKE_TOKEN,
        audience: str = "loopback",
        **limits,
    ):
        self.tmp_path = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.clock = Clock()
        self.mono = Mono()
        self.ha = FakeHA()
        self.path = tmp_path / "home_allowlist.json"
        self.write_allowlist(allowlist)
        self.config = HomeConfig(url=url, token=token, allowlist_path=self.path)
        self.limits = limits
        self.runtime = self._runtime()
        self.settings = make_settings(tmp_path)
        self.core = SqliteStore(":memory:", clock=self.clock)
        self.app = create_app(self.settings, store=self.core, worker=None, audience=audience)
        self.app.state.home = self.runtime  # the routes read it per request: this swaps in the fake transport
        self.client = TestClient(self.app)
        self.owner = {"Authorization": f"Bearer {ADMIN}"}
        self.dev_a = self.enroll("dev-a")
        self.dev_b = self.enroll("dev-b")
        self._keys = 0

    def _runtime(self) -> HomeRuntime:
        return HomeRuntime(
            self.tmp_path,
            config=self.config,
            transport=httpx.MockTransport(self.ha),
            clock=self.clock,
            monotonic=self.mono,
            **self.limits,
        )

    def restart(self) -> None:
        """A new process on the same files: fresh runtime, same data directory."""
        self.runtime.close()
        self.runtime = self._runtime()
        self.app.state.home = self.runtime

    def write_allowlist(self, allowlist: object) -> None:
        if allowlist == "example":
            self.path.write_bytes(EXAMPLE_ALLOWLIST.read_bytes())
        elif allowlist is None:
            self.path.unlink(missing_ok=True)
        elif isinstance(allowlist, (bytes, str)):
            self.path.write_bytes(allowlist if isinstance(allowlist, bytes) else allowlist.encode())
        else:
            self.path.write_text(json.dumps(allowlist), encoding="utf-8")

    def close(self) -> None:
        self.runtime.close()
        self.core.close()

    def enroll(self, device_id: str) -> dict:
        tok = self.core.create_device(device_id, device_id, True)
        return {"Authorization": f"Bearer {tok}"}

    # ---- calls ---------------------------------------------------------------------------------------
    def rooms(self, headers: dict | None = None, **params):
        return self.client.get("/v1/home/rooms", headers=headers or self.dev_a, params=params)

    def devices(self, room: str, headers: dict | None = None, **params):
        return self.client.get(f"/v1/home/rooms/{room}/devices", headers=headers or self.dev_a, params=params)

    def act(
        self,
        entity_id: str,
        action: str,
        *,
        confirmed: bool | None = None,
        key: str | None = "auto",
        headers: dict | None = None,
        body: dict | None = None,
    ):
        h = dict(headers or self.dev_a)
        if key == "auto":
            self._keys += 1
            key = f"key-{self._keys:08d}"
        if key is not None:
            h["Idempotency-Key"] = key
        payload = body if body is not None else {"entity_id": entity_id, "action": action}
        if body is None and confirmed is not None:
            payload["confirmed"] = confirmed
        return self.client.post("/v1/home/actions", headers=h, json=payload)

    def audit_rows(self) -> list[tuple]:
        import sqlite3
        from contextlib import closing

        db = self.tmp_path / "home.db"
        if not db.exists():
            return []
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute(
                "SELECT event, outcome, reason, device_id, room_id, entity_id, action, confirmed"
                " FROM audit ORDER BY seq"
            ).fetchall()
