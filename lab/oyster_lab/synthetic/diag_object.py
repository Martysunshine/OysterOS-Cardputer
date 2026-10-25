"""Deterministic synthetic diagnostic object and its 55-byte manifest (schemas/lab-profile.json "transfer").

The object is generated text that looks like a device diagnostic log. It contains synthetic data only:
no real device, network or user data. The same object id always yields the same bytes and SHA-256.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

DEFAULT_OBJECT_ID = "diag-synth-0001"
OBJECT_SIZE = 48 * 1024
MANIFEST_VERSION = 1
OBJECT_ID_BYTES = 16
# [version u8][object_id 16 bytes ASCII zero-padded][declared_length u32 LE][sha256 32][chunk_max u16 LE]
MANIFEST = struct.Struct("<B16sI32sH")
MANIFEST_BYTES = MANIFEST.size  # 55
APP_DENIAL = b"\x01"  # single byte returned by diag_manifest on application-level denial
# chunk_data max_length (246) minus the 9-byte chunk header
DEFAULT_CHUNK_MAX = 237

_OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_CODES = ("OK", "OK", "OK", "WARN", "OK", "RETRY", "OK", "INFO")


def is_valid_object_id(object_id: str) -> bool:
    """Same rule as firmware ids.h: [A-Za-z0-9_-], 1..16 characters (the manifest field width)."""
    return isinstance(object_id, str) and bool(_OBJECT_ID_RE.fullmatch(object_id))


def _check_object_id(object_id: str) -> None:
    if not is_valid_object_id(object_id):
        raise ValueError("object_id must be 1..16 characters of [A-Za-z0-9_-]")


def make_object(object_id: str = DEFAULT_OBJECT_ID, size: int = OBJECT_SIZE) -> bytes:
    """Return the deterministic synthetic diagnostic object (48 KiB by default).

    A smaller ``size`` returns a prefix of the same stream (the simulator uses short objects).
    """
    _check_object_id(object_id)
    if not 1 <= size <= 0xFFFFFFFF:
        raise ValueError("size out of range")
    out = bytearray(
        b"# OYSTER-LAB SYNTHETIC DIAGNOSTIC OBJECT v1\n"
        + f"# object_id={object_id}\n".encode("ascii")
        + b"# synthetic=true; generated test data, contains no real device or user data\n"
    )
    i = 0
    while len(out) < size:
        d = hashlib.sha256(f"{object_id}:{i}".encode("ascii")).digest()
        temp_cc = 1800 + d[0] * 3
        rssi = -40 - d[1] % 50
        vbat = 3300 + int.from_bytes(d[2:4], "little") % 900
        line = (
            f"{i:06d} t_ms={i * 250:09d} temp_cC={temp_cc:5d} rssi_dBm={rssi:4d} "
            f"vbat_mV={vbat:4d} code={_CODES[d[4] % len(_CODES)]:<5} blob={d[5:13].hex()}\n"
        )
        out += line.encode("ascii")
        i += 1
    return bytes(out[:size])


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_tag(obj: bytes) -> bytes:
    """First 4 bytes of the object's SHA-256 (the manifest sha256 field)."""
    return hashlib.sha256(obj).digest()[:4]


def manifest_bytes(obj: bytes, object_id: str, chunk_max: int) -> bytes:
    """Encode the 55-byte manifest v1 for ``obj``."""
    _check_object_id(object_id)
    if not 1 <= chunk_max <= 0xFFFF:
        raise ValueError("chunk_max must be 1..65535")
    if len(obj) > 0xFFFFFFFF:
        raise ValueError("object too large for a u32 length")
    return MANIFEST.pack(
        MANIFEST_VERSION,
        object_id.encode("ascii"),  # struct zero-pads to 16 bytes
        len(obj),
        hashlib.sha256(obj).digest(),
        chunk_max,
    )


@dataclass(frozen=True)
class Manifest:
    version: int
    object_id: str
    declared_length: int
    sha256: bytes
    chunk_max: int

    @property
    def tag(self) -> bytes:
        return self.sha256[:4]


class ManifestDenied(ValueError):
    """diag_manifest returned the single-byte application-level denial."""


def parse_manifest(data: bytes, *, max_object_bytes: int | None = None) -> Manifest:
    """Decode a manifest; raises ManifestDenied for the 0x01 denial and ValueError when malformed."""
    if bytes(data) == APP_DENIAL:
        raise ManifestDenied("application-level denial")
    if len(data) < MANIFEST_BYTES:
        raise ValueError("manifest too short")
    version, raw_id, length, digest, chunk_max = MANIFEST.unpack(bytes(data[:MANIFEST_BYTES]))
    if version != MANIFEST_VERSION:
        raise ValueError("unsupported manifest version")
    object_id = raw_id.rstrip(b"\x00").decode("ascii", errors="replace")
    if not is_valid_object_id(object_id):
        raise ValueError("invalid object id in manifest")
    if max_object_bytes is not None and length > max_object_bytes:
        raise ValueError("declared length exceeds the maximum object size")
    if chunk_max == 0:
        raise ValueError("chunk_max is zero")
    return Manifest(version, object_id, length, digest, chunk_max)
