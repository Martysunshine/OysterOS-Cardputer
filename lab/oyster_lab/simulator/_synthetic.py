"""Local deterministic synthetic-object helper.

`oyster_lab.synthetic.make_object` is the canonical helper. This package can generate the same
bytes locally so the simulator can run standalone: `diagnostic_object()` prefers the canonical
helper and falls back to this deterministic generator.

The bytes are synthetic lab data only: no secrets, fully reproducible.
"""

from __future__ import annotations

import hashlib

DEFAULT_OBJECT_ID = "diag-synth-0001"
DEFAULT_OBJECT_BYTES = 512
DEFAULT_CHUNK_MAX = 180


def make_object(object_id: str = DEFAULT_OBJECT_ID, length: int = DEFAULT_OBJECT_BYTES) -> bytes:
    """Deterministic synthetic object bytes derived from the object id (SHA-256 counter stream)."""
    seed = object_id.encode("ascii", "ignore")
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


def diagnostic_object(object_id: str = DEFAULT_OBJECT_ID, length: int | None = None) -> bytes:
    """The canonical synthetic object.

    When `oyster_lab.synthetic` is importable the simulator uses its canonical object (its own default
    size), so reported SHA-256 / hash_verified match the fixture. Without the fixture it falls back to
    this package's deterministic generator at `length` (or DEFAULT_OBJECT_BYTES).
    """
    try:
        from oyster_lab.synthetic import make_object as fixture_make_object
    except Exception:
        return make_object(object_id, length or DEFAULT_OBJECT_BYTES)
    if length is None:
        return fixture_make_object(object_id)  # fixture's canonical size
    try:
        return fixture_make_object(object_id, length)  # type: ignore[call-arg]
    except TypeError:
        return fixture_make_object(object_id)  # type: ignore[call-arg]


def object_sha256(object_id: str = DEFAULT_OBJECT_ID, length: int | None = None) -> str:
    return hashlib.sha256(diagnostic_object(object_id, length)).hexdigest()
