"""Audio helpers: the mu-law table matches the firmware's encoder, and the WAV we hand the model is a valid
16-bit mono PCM RIFF container."""

from __future__ import annotations

from oyster.voice.audio import (
    bytes_per_second,
    duration_s,
    parse_wav,
    pcm16_from_samples,
    to_pcm16,
    ulaw_decode_sample,
    ulaw_encode_sample,
    wav_bytes,
)


def test_ulaw_reference_points_match_the_firmware():
    # Same anchors asserted in firmware/test/test_voice/test_voice_codec.cpp.
    assert ulaw_encode_sample(0) == 0xFF
    assert ulaw_encode_sample(32767) == 0x80
    assert ulaw_encode_sample(-32768) == 0x00
    assert ulaw_encode_sample(-1) == 0x7F
    assert ulaw_decode_sample(0xFF) == 0
    assert ulaw_decode_sample(0x80) == 32124
    assert ulaw_decode_sample(0x00) == -32124


def test_ulaw_round_trip_sign_and_bound():
    for v in range(-32768, 32768, 101):
        back = ulaw_decode_sample(ulaw_encode_sample(v))
        if v > 0x84:
            assert back > 0
        if v < -0x84:
            assert back < 0
        assert -32768 <= back <= 32767


def test_to_pcm16_decodes_ulaw_and_passes_pcm_through():
    ulaw = bytes(ulaw_encode_sample(s) for s in (1000, -2000, 3000))
    assert to_pcm16("ulaw_8k", ulaw) == pcm16_from_samples(
        [ulaw_decode_sample(b) for b in ulaw]
    )
    pcm = pcm16_from_samples([5, -5, 100])
    assert to_pcm16("pcm16_8k", pcm) == pcm
    assert to_pcm16("pcm16_8k", pcm + b"\x01") == pcm  # odd trailing byte dropped


def test_wav_is_valid_pcm_container():
    pcm = pcm16_from_samples([ulaw_decode_sample(ulaw_encode_sample(v)) for v in range(-5000, 5000, 100)])
    wav = wav_bytes(pcm)
    channels, rate, bits, body = parse_wav(wav)
    assert (channels, rate, bits) == (1, 8000, 16)
    assert body == pcm


def test_duration_and_rate():
    assert bytes_per_second("ulaw_8k") == 8000
    assert bytes_per_second("pcm16_8k") == 16000
    assert duration_s("ulaw_8k", 8000) == 1.0
    assert duration_s("pcm16_8k", 8000) == 0.5
