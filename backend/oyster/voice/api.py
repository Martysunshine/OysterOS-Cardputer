"""Voice question routes (docs/VOICE_INPUT.md). Device routes, served on both listeners (the device records
the audio); same auth and the same 4 KiB body ceiling as the other device routes.

  POST   .../voice                       {format, max_seconds} -> {upload_id, chunk_bytes_max, expires_in_s}
  POST   .../voice/{upload_id}/chunks     raw octet-stream (<= chunk_bytes_max); the sequence number comes
                                          from the `X-Chunk-Seq` header or the `seq` query -> {received_bytes}
  POST   .../voice/{upload_id}/finish     -> {transcript, language, duration_s} or a clean error
  DELETE .../voice/{upload_id}            cancels and drops the audio -> {cancelled}

Error codes: validation_failed (422), not_found (404), payload_too_large (413), too_short/no_speech (422),
upload_incomplete (409), transcription_unavailable (503), provider_error (502), rate_limited (429).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from oyster.api.common import Device, DeviceBody, parse_model
from oyster.api.errors import ApiError
from oyster.voice.store import VoiceUploadError

device_router = APIRouter()

CHUNK_BODY_LIMIT = 4096


class VoiceStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = Field(pattern=r"^(pcm16_8k|ulaw_8k)$")
    max_seconds: int = Field(default=12, ge=1, le=12)


def _rt(request: Request):
    return request.app.state.voice


def _owns_session(request: Request, session_id: str, device_id: str | None) -> None:
    """404 before anything about the upload is revealed, exactly like the other session routes."""
    request.app.state.store.get_session(session_id, device_id)


def _throttle(limiter, key: str) -> None:
    wait = limiter.check(key)
    if wait is not None:
        raise ApiError(
            429, "rate_limited", f"too many requests; retry in {wait} s", {"Retry-After": str(wait)}
        )


def _as_api_error(e: VoiceUploadError) -> ApiError:
    return ApiError(e.status, e.code, e.detail)


async def _raw_chunk(request: Request) -> bytes:
    """Read at most CHUNK_BODY_LIMIT bytes of the raw body (413 beyond), no JSON parsing."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > CHUNK_BODY_LIMIT:
        raise ApiError(413, "payload_too_large", f"body exceeds {CHUNK_BODY_LIMIT} bytes")
    buf = bytearray()
    async for part in request.stream():
        buf += part
        if len(buf) > CHUNK_BODY_LIMIT:
            raise ApiError(413, "payload_too_large", f"body exceeds {CHUNK_BODY_LIMIT} bytes")
    return bytes(buf)


ChunkBody = Annotated[bytes, Depends(_raw_chunk)]


@device_router.post("/v1/sessions/{session_id}/voice")
def start_voice(session_id: str, request: Request, p: Device, body: DeviceBody) -> dict:
    _owns_session(request, session_id, p.device_id)
    rt = _rt(request)
    _throttle(rt.begin_limiter, p.device_id or "?")
    req = parse_model(VoiceStart, body)
    try:
        return rt.service.begin(p.device_id, session_id, req.format, req.max_seconds)
    except VoiceUploadError as e:
        raise _as_api_error(e) from None


@device_router.post("/v1/sessions/{session_id}/voice/{upload_id}/chunks")
def add_chunk(
    session_id: str,
    upload_id: str,
    request: Request,
    p: Device,
    data: ChunkBody,
    seq: Annotated[int | None, Query(ge=0)] = None,
) -> dict:
    _owns_session(request, session_id, p.device_id)
    header_seq = request.headers.get("x-chunk-seq")
    if header_seq is not None:
        if not header_seq.isdigit():
            raise ApiError(422, "validation_failed", "X-Chunk-Seq must be a non-negative integer")
        seq = int(header_seq)
    if seq is None:
        raise ApiError(422, "validation_failed", "a chunk needs an X-Chunk-Seq header or a seq query")
    try:
        return _rt(request).service.add_chunk(p.device_id, session_id, upload_id, seq, data)
    except VoiceUploadError as e:
        raise _as_api_error(e) from None


@device_router.post("/v1/sessions/{session_id}/voice/{upload_id}/finish")
def finish_voice(session_id: str, upload_id: str, request: Request, p: Device) -> dict:
    _owns_session(request, session_id, p.device_id)
    rt = _rt(request)
    _throttle(rt.finish_limiter, p.device_id or "?")
    try:
        return rt.service.finish(p.device_id, session_id, upload_id)
    except VoiceUploadError as e:
        raise _as_api_error(e) from None


@device_router.delete("/v1/sessions/{session_id}/voice/{upload_id}")
def cancel_voice(session_id: str, upload_id: str, request: Request, p: Device) -> dict:
    _owns_session(request, session_id, p.device_id)
    return {"cancelled": _rt(request).service.cancel(p.device_id, session_id, upload_id)}
