"""Wiring: `install(app)` adds the voice-question routes to a FastAPI app.

    from oyster.voice.mount import install
    install(app)            # after install_claude(app)

- Routes: device routes on both listeners (the device records the audio); there are no owner routes.
- State: one `VoiceRuntime` per app (the uploads live in memory and are process-local; no database). It
  holds the upload store, the speech transcriber and the per-device rate limiters.
- Speech settings: `AI_SPEECH_MODEL` (default `whisper-large-v3-turbo`) and the optional `AI_SPEECH_LANGUAGE`
  hint are read from the environment; the provider base URL, key and timeout come from `app.state.settings`
  (the same credentials as the AI runs). With no key or model the routes still work and `finish` returns a
  clean `transcription_unavailable`.
- Idempotent: installing twice on one app changes nothing.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

import httpx
from fastapi import FastAPI

from oyster.deck.jobs import RateLimiter
from oyster.voice.api import CHUNK_BODY_LIMIT, device_router
from oyster.voice.service import VoiceService
from oyster.voice.store import UploadStore
from oyster.voice.transcribe import SpeechTranscriber

log = logging.getLogger("oyster.voice")

DEFAULT_SPEECH_MODEL = "whisper-large-v3-turbo"
BEGINS_PER_MINUTE = 20
TRANSCRIPTIONS_PER_MINUTE = 12


class VoiceRuntime:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        language: str | None,
        timeout_s: float,
        clock: Callable[[], float] = time.time,
        transport: httpx.BaseTransport | None = None,
        ttl_s: float = 60.0,
    ):
        self.transcriber = SpeechTranscriber(
            base_url=base_url,
            api_key=api_key,
            model=model,
            language=language,
            timeout_s=timeout_s,
            transport=transport,
        )
        self.store = UploadStore(clock=clock, ttl_s=ttl_s, chunk_bytes_max=CHUNK_BODY_LIMIT)
        self.service = VoiceService(self.store, self.transcriber, clock=clock)
        self.begin_limiter = RateLimiter(BEGINS_PER_MINUTE, 60.0, clock)
        self.finish_limiter = RateLimiter(TRANSCRIPTIONS_PER_MINUTE, 60.0, clock)
        if not self.transcriber.configured:
            log.warning("voice input: AI_API_KEY or AI_SPEECH_MODEL not set; transcription is unavailable")

    def close(self) -> None:
        self.transcriber.close()


def _runtime_from_settings(app: FastAPI) -> VoiceRuntime:
    s = app.state.settings
    return VoiceRuntime(
        base_url=s.ai_base_url,
        api_key=s.ai_api_key,
        model=os.getenv("AI_SPEECH_MODEL") or DEFAULT_SPEECH_MODEL,
        language=os.getenv("AI_SPEECH_LANGUAGE") or None,
        timeout_s=float(s.ai_timeout_s),
    )


def install(app: FastAPI, runtime: VoiceRuntime | None = None) -> VoiceRuntime:
    """Register the voice routes on `app` (idempotent) and return its runtime."""
    existing = getattr(app.state, "voice", None)
    if existing is not None:
        return existing
    rt = runtime or _runtime_from_settings(app)
    app.state.voice = rt
    app.include_router(device_router)
    return rt
