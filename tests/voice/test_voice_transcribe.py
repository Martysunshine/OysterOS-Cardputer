"""Transcription: the provider errors map to clean codes, the transcript is clipped to the firmware's 240
characters, and no audio is kept once the request returns."""

from __future__ import annotations

from _voice_support import VoiceKit, code_of, ulaw_audio
from oyster.voice.service import clip_transcript


def _audio() -> list[bytes]:
    return [ulaw_audio(4096), ulaw_audio(4096, start=5000)]


def test_unconfigured_provider_reports_unavailable(kit_factory):
    kit = kit_factory(configured=False)
    fin = kit.upload_and_finish(_audio())
    assert fin.status_code == 503
    assert code_of(fin) == "transcription_unavailable"


def test_provider_http_error_maps_to_provider_error(kit: VoiceKit):
    kit.speech.status = 500
    fin = kit.upload_and_finish(_audio())
    assert fin.status_code == 502
    assert code_of(fin) == "provider_error"


def test_provider_non_json_maps_to_provider_error(kit: VoiceKit):
    kit.speech.raw = "<html>gateway</html>"
    fin = kit.upload_and_finish(_audio())
    assert fin.status_code == 502
    assert code_of(fin) == "provider_error"


def test_empty_transcript_is_no_speech(kit: VoiceKit):
    kit.speech.text = "   "
    fin = kit.upload_and_finish(_audio())
    assert fin.status_code == 422
    assert code_of(fin) == "no_speech"


def test_transcript_is_clipped_to_240_chars(kit: VoiceKit):
    kit.speech.text = "word " * 80  # 400 chars
    fin = kit.upload_and_finish(_audio())
    assert fin.status_code == 200
    transcript = fin.json()["transcript"]
    assert len(transcript) <= 240
    assert transcript.endswith("...")
    assert "  " not in transcript  # whitespace folded to single spaces


def test_clip_transcript_cuts_at_a_word_boundary():
    assert clip_transcript("short answer") == "short answer"
    long = "alpha bravo charlie " * 20
    clipped = clip_transcript(long, limit=40)
    assert len(clipped) <= 40
    assert clipped.endswith("...")
    assert not clipped[:-3].endswith(" ")  # trailing space trimmed before the marker
    # a single very long token is still bounded
    assert len(clip_transcript("x" * 100, limit=40)) <= 40


def test_audio_is_not_persisted(kit: VoiceKit):
    fin = kit.upload_and_finish(_audio())
    assert fin.status_code == 200
    # nothing audio-like was written anywhere under the data directory
    for pattern in ("*.wav", "*.pcm", "*.ulaw", "*.raw"):
        assert list(kit.tmp_path.rglob(pattern)) == []
    # the finished upload is gone from memory too
    assert kit.runtime.store.live_count() == 0
    # the response carries text only, not the audio bytes
    assert set(fin.json()) == {"transcript", "language", "duration_s"}


def test_language_hint_is_forwarded_when_set(kit_factory):
    kit = kit_factory()
    kit.runtime.transcriber.language = "cs"
    kit.upload_and_finish(_audio())
    assert b'name="language"' in kit.speech.last_body
    assert b"cs" in kit.speech.last_body
