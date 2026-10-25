"""Synthetic diagnostic object and manifest layout (schemas/lab-profile.json transfer.manifest_layout)."""

import hashlib
import struct

import pytest
from oyster_lab.synthetic import (
    APP_DENIAL,
    DEFAULT_CHUNK_MAX,
    DEFAULT_OBJECT_ID,
    MANIFEST_BYTES,
    OBJECT_SIZE,
    ManifestDenied,
    is_valid_object_id,
    make_object,
    manifest_bytes,
    object_tag,
    parse_manifest,
    sha256_hex,
)

# Pinned so an accidental generator change is caught (firmware/simulator tests may rely on it).
DEFAULT_OBJECT_SHA256 = "2db337398df3833ebdb27c83be1ebc57158c7479cbd605145b2d729ef06d5aba"


def test_object_is_deterministic_48k_and_labelled_synthetic():
    a = make_object(DEFAULT_OBJECT_ID)
    b = make_object(DEFAULT_OBJECT_ID)
    assert a == b
    assert len(a) == OBJECT_SIZE == 48 * 1024
    assert a.startswith(b"# OYSTER-LAB SYNTHETIC DIAGNOSTIC OBJECT v1\n")
    assert b"synthetic=true" in a[:200]
    assert f"object_id={DEFAULT_OBJECT_ID}".encode() in a[:200]
    a.decode("ascii")  # plain ASCII text


def test_object_sha256_pinned():
    assert sha256_hex(make_object(DEFAULT_OBJECT_ID)) == DEFAULT_OBJECT_SHA256


def test_size_argument_returns_prefix():
    full = make_object(DEFAULT_OBJECT_ID)
    assert make_object(DEFAULT_OBJECT_ID, 512) == full[:512]
    assert make_object(DEFAULT_OBJECT_ID, size=1) == full[:1]
    with pytest.raises(ValueError):
        make_object(DEFAULT_OBJECT_ID, 0)


def test_different_object_ids_give_different_objects():
    assert make_object("diag-synth-0001") != make_object("diag-synth-0002")


@pytest.mark.parametrize("bad", ["", "a" * 17, "../x", "a/b", "a\\b", "a:b", "a b", "ä", ".."])
def test_invalid_object_ids_rejected(bad):
    assert not is_valid_object_id(bad)
    with pytest.raises(ValueError):
        make_object(bad)
    with pytest.raises(ValueError):
        manifest_bytes(b"x", bad, 10)


def test_manifest_layout_55_bytes():
    obj = make_object(DEFAULT_OBJECT_ID)
    m = manifest_bytes(obj, DEFAULT_OBJECT_ID, DEFAULT_CHUNK_MAX)
    digest = hashlib.sha256(obj).digest()
    assert len(m) == MANIFEST_BYTES == 55
    assert m[0] == 1
    assert m[1:17] == b"diag-synth-0001\x00"  # 16 bytes ASCII zero-padded
    assert struct.unpack("<I", m[17:21])[0] == len(obj) == 49152
    assert m[21:53] == digest
    assert struct.unpack("<H", m[53:55])[0] == DEFAULT_CHUNK_MAX == 237
    assert object_tag(obj) == digest[:4] == m[21:25]


def test_manifest_parse_round_trip_and_errors():
    obj = make_object(DEFAULT_OBJECT_ID)
    m = manifest_bytes(obj, DEFAULT_OBJECT_ID, 100)
    parsed = parse_manifest(m, max_object_bytes=65536)
    assert parsed.object_id == DEFAULT_OBJECT_ID
    assert parsed.declared_length == len(obj)
    assert parsed.sha256 == hashlib.sha256(obj).digest()
    assert parsed.chunk_max == 100
    assert parsed.tag == object_tag(obj)
    with pytest.raises(ManifestDenied):
        parse_manifest(APP_DENIAL)
    with pytest.raises(ValueError):
        parse_manifest(m[:54])
    with pytest.raises(ValueError):
        parse_manifest(b"\x02" + m[1:])
    with pytest.raises(ValueError):
        parse_manifest(m, max_object_bytes=1000)


@pytest.mark.parametrize("chunk_max", [0, 65536, -1])
def test_manifest_chunk_max_bounds(chunk_max):
    with pytest.raises(ValueError):
        manifest_bytes(b"abc", DEFAULT_OBJECT_ID, chunk_max)
