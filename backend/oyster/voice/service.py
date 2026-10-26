"""Voice service: ties the upload store, the transcriber and the per-device rate limiter together, builds
the WAV and returns the clipped transcript. Audio is decoded to 16-bit PCM only to build the WAV and is
dropped as soon as the transcription returns; it is never persisted."""

from __future__ import annotations

import time
from collections.abc import Callable

from oyster.voice.audio import duration_s, to_pcm16, wav_bytes
from oyster.voice.store import Upload, UploadStore, VoiceUploadError
from oyster.voice.transcribe import SpeechTranscriber, TranscriptionError

TRANSCRIPT_MAX_CHARS = 240  # the firmware's question limit (cfg::kMaxQuestionLen)
MIN_AUDIO_S = 0.4  # below this a transcription is not attempted


def clip_transcript(text: str, limit: int = TRANSCRIPT_MAX_CHARS) -> str:
    """Bound the transcript to `limit` characters, cutting at a word boundary and marking the cut."""
    text = " ".join(text.split())  # fold whitespace/newlines (the entry is a single-line field)
    if len(text) <= limit:
        return text
    head = text[: limit - 3]
    cut = head.rsplit(" ", 1)[0] if " " in head else head
    return (cut.rstrip() or head.rstrip()) + "..."


class VoiceService:
    def __init__(
        self,
        store: UploadStore,
        transcriber: SpeechTranscriber,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.transcriber = transcriber
        self._clock = clock

    def begin(self, device_id: str, session_id: str, fmt: str, max_seconds: int) -> dict:
        up = self.store.create(device_id, session_id, fmt, max_seconds)
        return {
            "upload_id": up.upload_id,
            "chunk_bytes_max": self.store.chunk_bytes_max,
            "expires_in_s": int(self.store.ttl_s),
        }

    def add_chunk(
        self, device_id: str, session_id: str, upload_id: str, seq: int, data: bytes
    ) -> dict:
        received = self.store.add_chunk(upload_id, device_id, session_id, seq, data)
        return {"received_bytes": received}

    def finish(self, device_id: str, session_id: str, upload_id: str) -> dict:
        up: Upload = self.store.take(upload_id, device_id, session_id)
        raw = up.pcm_source_bytes()  # raises upload_incomplete on a gap
        if duration_s(up.fmt, len(raw)) < MIN_AUDIO_S:
            raise VoiceUploadError(422, "too_short", "recording too short to transcribe")
        pcm16 = to_pcm16(up.fmt, raw)
        wav = wav_bytes(pcm16)
        try:
            text, language, provider_duration = self.transcriber.transcribe(wav)
        except TranscriptionError as e:
            status = 503 if e.code == "transcription_unavailable" else 502
            raise VoiceUploadError(status, e.code, e.detail) from None
        finally:
            del raw, pcm16, wav  # the decoded audio does not outlive the request
        transcript = clip_transcript(text)
        if not transcript:
            raise VoiceUploadError(422, "no_speech", "no speech recognised")
        return {
            "transcript": transcript,
            "language": language,
            "duration_s": provider_duration
            if provider_duration is not None
            else round(duration_s(up.fmt, up.received_bytes), 2),
        }

    def cancel(self, device_id: str, session_id: str, upload_id: str) -> bool:
        return self.store.delete(upload_id, device_id, session_id)
