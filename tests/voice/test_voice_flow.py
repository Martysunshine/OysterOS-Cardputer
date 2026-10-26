"""The upload lifecycle: begin, stream chunks, finish; and the chunk rules (order, duplicates, oversize,
budget), the TTL and cancel."""

from __future__ import annotations

from _voice_support import VoiceKit, code_of, ulaw_audio


def test_full_flow_returns_a_transcript_and_sends_a_wav(kit: VoiceKit):
    start = kit.begin()
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["upload_id"].startswith("vup-")
    assert body["chunk_bytes_max"] == 4096
    assert body["expires_in_s"] == 60
    uid = body["upload_id"]

    assert kit.chunk(uid, 0, ulaw_audio(4096)).json() == {"received_bytes": 4096}
    assert kit.chunk(uid, 1, ulaw_audio(4096, start=5000)).json() == {"received_bytes": 8192}

    fin = kit.finish(uid)
    assert fin.status_code == 200, fin.text
    out = fin.json()
    assert out["transcript"] == kit.speech.text
    assert out["language"] == "en"
    assert out["duration_s"] == 1.8

    # exactly one provider call, carrying a real WAV and the speech model
    assert len(kit.speech.requests) == 1
    sent = kit.speech.last_body
    assert b"RIFF" in sent and b"WAVE" in sent
    assert b"whisper-large-v3-turbo" in sent


def test_chunk_sequence_header_is_accepted(kit: VoiceKit):
    uid = kit.begin().json()["upload_id"]
    assert kit.chunk_hdr(uid, 0, ulaw_audio(4096)).json() == {"received_bytes": 4096}
    assert kit.chunk_hdr(uid, 1, ulaw_audio(4000)).json() == {"received_bytes": 8096}
    assert kit.finish(uid).status_code == 200


def test_duplicate_chunk_is_ignored(kit: VoiceKit):
    uid = kit.begin().json()["upload_id"]
    kit.chunk(uid, 0, ulaw_audio(4096))
    kit.chunk(uid, 1, ulaw_audio(1000))
    again = kit.chunk(uid, 0, ulaw_audio(4096))  # retransmit of seq 0
    assert again.json() == {"received_bytes": 5096}  # not counted twice


def test_chunk_sequence_cannot_go_backwards(kit: VoiceKit):
    uid = kit.begin().json()["upload_id"]
    kit.chunk(uid, 0, ulaw_audio(100))
    kit.chunk(uid, 2, ulaw_audio(100))
    back = kit.chunk(uid, 1, ulaw_audio(100))
    assert back.status_code == 409
    assert code_of(back) == "validation_failed"


def test_missing_chunk_makes_finish_incomplete(kit: VoiceKit):
    uid = kit.begin().json()["upload_id"]
    kit.chunk(uid, 0, ulaw_audio(4096))
    kit.chunk(uid, 2, ulaw_audio(4096))  # seq 1 never arrives
    fin = kit.finish(uid)
    assert fin.status_code == 409
    assert code_of(fin) == "upload_incomplete"


def test_oversize_chunk_is_rejected(kit: VoiceKit):
    uid = kit.begin().json()["upload_id"]
    big = kit.chunk(uid, 0, ulaw_audio(5000))
    assert big.status_code == 413
    assert code_of(big) == "payload_too_large"


def test_upload_budget_is_bounded_by_max_seconds(kit: VoiceKit):
    uid = kit.begin(max_seconds=1).json()["upload_id"]  # 8000 bytes of ulaw
    assert kit.chunk(uid, 0, ulaw_audio(4096)).status_code == 200
    over = kit.chunk(uid, 1, ulaw_audio(4096))  # 8192 > 8000
    assert over.status_code == 413
    assert code_of(over) == "payload_too_large"


def test_max_seconds_is_clamped(kit: VoiceKit):
    too_long = kit.begin(max_seconds=99)
    assert too_long.status_code == 422  # pydantic le=12


def test_too_short_recording_is_refused(kit: VoiceKit):
    uid = kit.begin().json()["upload_id"]
    kit.chunk(uid, 0, ulaw_audio(100))  # ~12 ms
    fin = kit.finish(uid)
    assert fin.status_code == 422
    assert code_of(fin) == "too_short"
    assert kit.speech.requests == []  # the provider is never called


def test_ttl_expires_the_upload(kit: VoiceKit):
    uid = kit.begin().json()["upload_id"]
    kit.chunk(uid, 0, ulaw_audio(4096))
    kit.clock.advance(61)
    late = kit.chunk(uid, 1, ulaw_audio(4096))
    assert late.status_code == 404
    assert code_of(late) == "not_found"
    assert kit.runtime.store.live_count() == 0


def test_cancel_drops_the_upload(kit: VoiceKit):
    uid = kit.begin().json()["upload_id"]
    kit.chunk(uid, 0, ulaw_audio(4096))
    assert kit.cancel(uid).json() == {"cancelled": True}
    fin = kit.finish(uid)
    assert fin.status_code == 404
    assert kit.speech.requests == []
    assert kit.cancel(uid).json() == {"cancelled": False}  # already gone


def test_at_most_two_uploads_per_device(kit: VoiceKit):
    kit.begin()
    kit.begin()
    third = kit.begin()
    assert third.status_code == 429
    assert code_of(third) == "rate_limited"
