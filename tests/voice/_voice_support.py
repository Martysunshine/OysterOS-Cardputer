"""Support for the voice-question tests: an in-process app over a temporary data directory, a controllable
clock, a device with a registered session, and a fake speech endpoint (httpx.MockTransport). Nothing here
touches a network, and no audio is written to disk."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from oyster.db.sqlite_store import SqliteStore
from oyster.deck.mount import DeckRuntime
from oyster.main import create_app
from oyster.settings import Settings
from oyster.voice.audio import ulaw_encode_sample
from oyster.voice.mount import VoiceRuntime

ADMIN = "test-admin-token-0123456789abcdef"
SESSION = "sess-voicetest01"


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
        ai_base_url="https://api.groq.com/openai/v1",
        ai_model="openai/gpt-oss-20b",
        ai_api_key="gsk_test_speech_key_0123456789abcdef",
        ai_timeout_s=5,
        ai_max_input_tokens=12000,
        ai_max_context_events=40,
        ai_max_output_tokens=1500,
        ai_price_input_per_mtok=None,
        ai_price_output_per_mtok=None,
    )


def ulaw_audio(n_bytes: int, start: int = 1000) -> bytes:
    """n_bytes of plausible mu-law audio (a slow ramp, not silence)."""
    samples = [((start + 37 * i) % 20000) - 10000 for i in range(n_bytes)]
    return bytes(ulaw_encode_sample(s) for s in samples)


class FakeSpeech:
    """A stand-in for the provider's /audio/transcriptions endpoint."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.text = "Is my living room sensor publicly readable"
        self.language = "en"
        self.duration = 1.8
        self.status = 200
        self.error_body = {"error": {"message": "boom"}}
        self.raw: str | None = None  # non-JSON body to return instead

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raw is not None:
            return httpx.Response(self.status, text=self.raw)
        if self.status != 200:
            return httpx.Response(self.status, json=self.error_body)
        return httpx.Response(
            200, json={"text": self.text, "language": self.language, "duration": self.duration}
        )

    @property
    def last_body(self) -> bytes:
        return self.requests[-1].content if self.requests else b""


class VoiceKit:
    def __init__(self, tmp_path: Path, *, configured: bool = True):
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.settings = make_settings(tmp_path)
        self.store = SqliteStore(":memory:", clock=self.clock)
        self.deck = DeckRuntime(tmp_path, synchronous="OFF", autostart=False)
        self.app = create_app(self.settings, store=self.store, worker=None, deck_runtime=self.deck)
        self.speech = FakeSpeech()
        self._ids = itertools.count(1)
        self.app.state.voice = VoiceRuntime(
            base_url=self.settings.ai_base_url,
            api_key=self.settings.ai_api_key if configured else "",
            model="whisper-large-v3-turbo" if configured else "",
            language=None,
            timeout_s=5,
            clock=self.clock,
            transport=httpx.MockTransport(self.speech.handler),
        )
        # deterministic upload ids for readable assertions
        self.app.state.voice.store._new_id = lambda: f"vup-{next(self._ids):08d}"  # noqa: SLF001
        self.client = TestClient(self.app)
        self.token = self.store.create_device("dev-a", "dev-a", False)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.store.register_session("dev-a", SESSION, "live", [], None)

    @property
    def runtime(self) -> VoiceRuntime:
        return self.app.state.voice

    def device_app(self) -> TestClient:
        app = create_app(
            self.settings, store=self.store, worker=None, audience="device", deck_runtime=self.deck
        )
        app.state.voice = self.runtime
        return TestClient(app)

    # --- request helpers (session fixed to SESSION) ---
    def begin(self, fmt: str = "ulaw_8k", max_seconds: int = 12, headers: dict | None = None):
        return self.client.post(
            f"/v1/sessions/{SESSION}/voice",
            headers=self.headers if headers is None else headers,
            json={"format": fmt, "max_seconds": max_seconds},
        )

    def chunk(self, upload_id: str, seq: int, data: bytes, headers: dict | None = None):
        hdr = {**(self.headers if headers is None else headers), "Content-Type": "application/octet-stream"}
        return self.client.post(
            f"/v1/sessions/{SESSION}/voice/{upload_id}/chunks?seq={seq}",
            headers=hdr,
            content=data,
        )

    def chunk_hdr(self, upload_id: str, seq: int, data: bytes):
        return self.client.post(
            f"/v1/sessions/{SESSION}/voice/{upload_id}/chunks",
            headers={**self.headers, "Content-Type": "application/octet-stream", "X-Chunk-Seq": str(seq)},
            content=data,
        )

    def finish(self, upload_id: str, headers: dict | None = None):
        return self.client.post(
            f"/v1/sessions/{SESSION}/voice/{upload_id}/finish",
            headers=self.headers if headers is None else headers,
        )

    def cancel(self, upload_id: str):
        return self.client.delete(f"/v1/sessions/{SESSION}/voice/{upload_id}", headers=self.headers)

    def upload_and_finish(self, chunks: list[bytes]) -> httpx.Response:
        uid = self.begin().json()["upload_id"]
        for i, c in enumerate(chunks):
            assert self.chunk(uid, i, c).status_code == 200
        return self.finish(uid)

    def close(self) -> None:
        self.runtime.close()
        self.deck.close()
        self.store.close()


def code_of(resp: httpx.Response) -> str:
    try:
        return json.loads(resp.content)["error"]
    except (ValueError, KeyError):
        return ""
