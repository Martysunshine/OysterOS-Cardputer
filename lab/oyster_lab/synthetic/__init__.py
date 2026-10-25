"""Synthetic diagnostic object generator shared by the live fixture and the simulator."""

from oyster_lab.synthetic.diag_object import (
    APP_DENIAL,
    DEFAULT_CHUNK_MAX,
    DEFAULT_OBJECT_ID,
    MANIFEST_BYTES,
    MANIFEST_VERSION,
    OBJECT_SIZE,
    Manifest,
    ManifestDenied,
    is_valid_object_id,
    make_object,
    manifest_bytes,
    object_tag,
    parse_manifest,
    sha256_hex,
)

__all__ = [
    "APP_DENIAL",
    "DEFAULT_CHUNK_MAX",
    "DEFAULT_OBJECT_ID",
    "MANIFEST_BYTES",
    "MANIFEST_VERSION",
    "OBJECT_SIZE",
    "Manifest",
    "ManifestDenied",
    "is_valid_object_id",
    "make_object",
    "manifest_bytes",
    "object_tag",
    "parse_manifest",
    "sha256_hex",
]
