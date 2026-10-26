"""In-memory store of voice uploads. Each upload holds its chunks in RAM only, expires after a short TTL
and is deleted after transcription or cancellation. Nothing is written to disk (docs/PRIVACY.md).

Bounds, all enforced here: at most `max_per_device` live uploads per device, a per-upload byte ceiling
derived from its format and `max_seconds`, a single chunk at most `chunk_bytes_max`, and a TTL. Chunk
sequence numbers must not go backwards; a repeated sequence number is ignored (idempotent retry).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from oyster.voice.audio import FORMATS, bytes_per_second


class VoiceUploadError(Exception):
    """A rejected upload operation. `code` is the wire error code, `status` the HTTP status."""

    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass
class Upload:
    upload_id: str
    device_id: str
    session_id: str
    fmt: str
    max_bytes: int
    created_at: float
    last_seq: int = -1
    received_bytes: int = 0
    _chunks: dict[int, bytes] = field(default_factory=dict)

    def pcm_source_bytes(self) -> bytes:
        """The raw audio, chunks concatenated in sequence order. Raises when a sequence number is missing
        (an incomplete upload: the device must resend)."""
        if not self._chunks:
            return b""
        highest = max(self._chunks)
        missing = [i for i in range(highest + 1) if i not in self._chunks]
        if missing:
            raise VoiceUploadError(409, "upload_incomplete", f"missing chunk(s) {missing[:4]}")
        return b"".join(self._chunks[i] for i in range(highest + 1))


class UploadStore:
    """Thread-safe; `clock` is injectable for tests."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        ttl_s: float = 60.0,
        chunk_bytes_max: int = 4096,
        max_per_device: int = 2,
        new_id: Callable[[], str] | None = None,
    ):
        self._clock = clock
        self.ttl_s = ttl_s
        self.chunk_bytes_max = chunk_bytes_max
        self.max_per_device = max_per_device
        self._counter = 0
        self._new_id = new_id or self._default_id
        self._uploads: dict[str, Upload] = {}
        self._lock = threading.Lock()

    def _default_id(self) -> str:
        self._counter += 1
        return f"vup-{self._clock():.0f}{self._counter:04d}"

    def _sweep(self, now: float) -> None:
        for uid in [u.upload_id for u in self._uploads.values() if now - u.created_at > self.ttl_s]:
            self._uploads.pop(uid, None)

    def create(self, device_id: str, session_id: str, fmt: str, max_seconds: int) -> Upload:
        if fmt not in FORMATS:
            raise VoiceUploadError(422, "validation_failed", f"unknown format {fmt!r}")
        max_seconds = max(1, min(int(max_seconds), 12))
        with self._lock:
            now = self._clock()
            self._sweep(now)
            live = [u for u in self._uploads.values() if u.device_id == device_id]
            if len(live) >= self.max_per_device:
                raise VoiceUploadError(429, "rate_limited", "too many voice uploads in progress")
            up = Upload(
                upload_id=self._new_id(),
                device_id=device_id,
                session_id=session_id,
                fmt=fmt,
                max_bytes=bytes_per_second(fmt) * max_seconds,
                created_at=now,
            )
            self._uploads[up.upload_id] = up
            return up

    def _get_locked(self, upload_id: str, device_id: str, session_id: str, now: float) -> Upload:
        up = self._uploads.get(upload_id)
        if up is None or up.device_id != device_id or up.session_id != session_id:
            raise VoiceUploadError(404, "not_found", "unknown upload")
        if now - up.created_at > self.ttl_s:
            self._uploads.pop(upload_id, None)
            raise VoiceUploadError(404, "not_found", "upload expired")
        return up

    def add_chunk(self, upload_id: str, device_id: str, session_id: str, seq: int, data: bytes) -> int:
        if len(data) > self.chunk_bytes_max:
            raise VoiceUploadError(413, "payload_too_large", f"chunk exceeds {self.chunk_bytes_max} bytes")
        if seq < 0:
            raise VoiceUploadError(422, "validation_failed", "X-Chunk-Seq must be >= 0")
        with self._lock:
            now = self._clock()
            up = self._get_locked(upload_id, device_id, session_id, now)
            if seq in up._chunks:
                return up.received_bytes  # idempotent: a retried chunk is ignored
            if seq < up.last_seq:
                raise VoiceUploadError(409, "validation_failed", "chunk sequence went backwards")
            if up.received_bytes + len(data) > up.max_bytes:
                raise VoiceUploadError(413, "payload_too_large", "upload exceeds its audio budget")
            up._chunks[seq] = bytes(data)
            up.last_seq = seq
            up.received_bytes += len(data)
            return up.received_bytes

    def take(self, upload_id: str, device_id: str, session_id: str) -> Upload:
        """Remove and return the upload for transcription (it never lingers once finished)."""
        with self._lock:
            now = self._clock()
            up = self._get_locked(upload_id, device_id, session_id, now)
            self._uploads.pop(upload_id, None)
            return up

    def delete(self, upload_id: str, device_id: str, session_id: str) -> bool:
        with self._lock:
            up = self._uploads.get(upload_id)
            if up is None or up.device_id != device_id or up.session_id != session_id:
                return False
            self._uploads.pop(upload_id, None)
            return True

    def live_count(self) -> int:
        with self._lock:
            self._sweep(self._clock())
            return len(self._uploads)
