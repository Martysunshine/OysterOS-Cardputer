"""Speech-to-text through the configured provider's OpenAI-compatible audio endpoint.

`POST {AI_BASE_URL}/audio/transcriptions` (multipart: `file`, `model`, `response_format=verbose_json`, an
optional `language` hint). Groq serves this with `whisper-large-v3-turbo`. The TLS context, the bearer
header and the secret scrubbing follow `oyster.ai.openai_compat`; the API key is never logged. The result
is text only; the caller clips and stores it. Errors are raised as `TranscriptionError` with a stable code
that the API maps to its `{"error": ...}` body.
"""

from __future__ import annotations

import httpx

from oyster.ai.openai_compat import check_base_url, tls_context
from oyster.ai.redact import clip, redact_text

MAX_TRANSCRIPT_WIRE = 4000  # the provider should return little; a defensive bound before any clipping


class TranscriptionError(RuntimeError):
    """A transcription attempt that cannot produce text. `code` is the wire error code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class SpeechTranscriber:
    """One per backend process; thread-safe (httpx.Client). `transport` is injected by the tests."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        language: str | None = None,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._configured = bool(api_key and model)
        self.model = model
        self.language = language or None
        self._secret = api_key
        self._base_url = base_url.rstrip("/")
        self._client: httpx.Client | None = None
        if self._configured:
            check_base_url(base_url)
            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=httpx.Timeout(float(timeout_s)),
                verify=tls_context(),
                transport=transport,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                follow_redirects=False,
            )

    @property
    def configured(self) -> bool:
        return self._configured

    def _scrub(self, text: str) -> str:
        return clip(redact_text(text, (self._secret,)), 200)

    def transcribe(self, wav: bytes, *, filename: str = "speech.wav") -> tuple[str, str | None, float | None]:
        """Return (text, language, duration_s). Raises TranscriptionError on any failure."""
        if not self._configured or self._client is None:
            raise TranscriptionError(
                "transcription_unavailable", "AI_API_KEY or AI_SPEECH_MODEL is not set"
            )
        data = {"model": self.model, "response_format": "verbose_json", "temperature": "0"}
        if self.language:
            data["language"] = self.language
        files = {"file": (filename, wav, "audio/wav")}
        try:
            resp = self._client.post("/audio/transcriptions", data=data, files=files)
        except httpx.TimeoutException as e:
            raise TranscriptionError("provider_error", f"speech provider timeout ({type(e).__name__})") from e
        except httpx.HTTPError as e:
            raise TranscriptionError(
                "provider_error", f"speech provider transport error: {self._scrub(str(e))}"
            ) from e
        if resp.status_code != 200:
            raise TranscriptionError(
                "provider_error", f"speech provider HTTP {resp.status_code}: {self._scrub(resp.text)}"
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise TranscriptionError("provider_error", "speech provider returned non-JSON") from e
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise TranscriptionError("provider_error", "speech provider response has no text")
        text = body["text"].strip()[:MAX_TRANSCRIPT_WIRE]
        language = body.get("language") if isinstance(body.get("language"), str) else None
        duration = None
        if isinstance(body.get("duration"), (int, float)):
            duration = round(float(body["duration"]), 2)
        return text, language, duration

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
