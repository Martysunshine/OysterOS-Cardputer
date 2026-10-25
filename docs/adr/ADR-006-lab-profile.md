# ADR-006: Lab profile and diagnostic transfer

**Status:** accepted (2026-10-24)

**Decision.** `schemas/lab-profile.json` is the single source of truth for the lab peripheral: fixed 128-bit
UUIDs (one random base, derived children), per-characteristic access policy, and deterministic assertions.
`scripts/gen_lab_profile_header.py` generates `oyster_core/lab_profile_gen.h`; `scripts/check.ps1` fails if
the header is stale.

**Transfer.** A custom profile, not a filesystem browser: a 55-byte manifest (version, object id, declared
length, SHA-256, chunk-max) plus `chunk_ctrl` (write) / `chunk_data` (read) request/response. Chunks obey
`len <= min(chunk_max, MTU-1-9)`, stream into a `.partial` file with incremental SHA-256, and finalize only
after length and hash verification. Local filenames are generated (`<object-id>.bin`); traversal, absolute
paths and unexpected object ids are rejected. Application-level denial (a single 0x01 byte) and link-level
ATT errors are recorded as separate facts.
