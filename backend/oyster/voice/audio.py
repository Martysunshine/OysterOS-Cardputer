"""Audio formats of the voice upload and the WAV handed to the speech model.

The device sends 8 kHz mono audio either as 16-bit little-endian PCM (`pcm16_8k`, 16 000 bytes/s) or as
8-bit G.711 mu-law (`ulaw_8k`, 8 000 bytes/s; what the firmware uses). Both become 16-bit PCM here and are
wrapped in a plain RIFF/WAVE container (no library: the header is 44 bytes). The mu-law formula is the
same one as in the firmware (firmware/lib/oyster_core/src/voice_codec.cpp): bias 0x84, clip 32635.
"""

from __future__ import annotations

import struct

SAMPLE_RATE = 8000
FORMATS = {"pcm16_8k": 2, "ulaw_8k": 1}  # bytes per sample
_BIAS = 0x84
_CLIP = 32635


def bytes_per_second(fmt: str) -> int:
    return SAMPLE_RATE * FORMATS[fmt]


def ulaw_decode_sample(u: int) -> int:
    v = ~u & 0xFF
    exponent = (v >> 4) & 0x07
    mantissa = v & 0x0F
    s = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -s if v & 0x80 else s


_ULAW_TABLE = [ulaw_decode_sample(u) for u in range(256)]


def ulaw_encode_sample(pcm: int) -> int:
    """Linear 16-bit -> mu-law byte (the firmware's encoder; used by tests and the simulator)."""
    sign = 0
    if pcm < 0:
        pcm = -pcm
        sign = 0x80
    pcm = min(pcm, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (pcm & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (pcm >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def ulaw_to_pcm16(data: bytes) -> bytes:
    return struct.pack(f"<{len(data)}h", *(_ULAW_TABLE[b] for b in data))


def pcm16_from_samples(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def to_pcm16(fmt: str, data: bytes) -> bytes:
    """Upload bytes of `fmt` -> 16-bit little-endian PCM. A trailing odd PCM byte is dropped."""
    if fmt == "ulaw_8k":
        return ulaw_to_pcm16(data)
    if fmt == "pcm16_8k":
        return bytes(data[: len(data) - (len(data) % 2)])
    raise ValueError(f"unknown audio format {fmt!r}")


def duration_s(fmt: str, n_bytes: int) -> float:
    return n_bytes / bytes_per_second(fmt)


def wav_bytes(pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """16-bit mono PCM WAV (RIFF header + data)."""
    n = len(pcm16) - (len(pcm16) % 2)
    body = pcm16[:n]
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + n,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        n,
    )
    return header + body


def parse_wav(data: bytes) -> tuple[int, int, int, bytes]:
    """(channels, sample_rate, bits, pcm) of a WAV written by wav_bytes (tests)."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE" or data[12:16] != b"fmt " or data[36:40] != b"data":
        raise ValueError("not a plain PCM WAV")
    fmt_tag, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", data[20:36])
    if fmt_tag != 1:
        raise ValueError("not PCM")
    (n,) = struct.unpack("<I", data[40:44])
    return channels, rate, bits, data[44 : 44 + n]
