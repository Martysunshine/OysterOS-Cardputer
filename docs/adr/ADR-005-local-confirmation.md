# ADR-005: Local confirmation gate

**Status:** accepted (2026-10-24)

**Decision.** Whether an operation needs an on-device confirmation is derived **only** from the operation
type (writes and diagnostic transfer), in one place on each side: `operation_requires_local_confirmation()`
on the device (`oyster_core/local_policy.h`) and the tool catalogue on the backend. A confirmation is valid
only when it carries the exact `{job_id, nonce}` of the pending job, is approved on the device, and arrives
within the timeout.

**Why / consequences.** No backend boolean or model output can lower or bypass the requirement; a job that
arrives with `requires_local_confirmation=false` for a write is still gated by the device. A test
(`tests/auth_jobs`) proves a server/model-supplied approval flag cannot bypass the gate.

**Amendment (2026-10-24): access-policy assertions and expiry.** `ble_check_access_policy` is gated when the
selected assertion's `operation` in `schemas/lab-profile.json` is `write` or `transfer`; the rule is derived
from the enrolled target's profile plus the job's `test_id` (never from the model or a server flag), in the
backend catalogue (`job_requires_local_confirmation`), the AI tool catalogue and the device/simulator policy,
and a test checks all three agree for every `test_id`. The operation is also `changes_target_state`
everywhere (never retried automatically). When the AI run stops waiting for a confirmation it expires the
job; the backend then refuses the device's `running` report (409), and the device executes a gated job only
after that report was accepted, so a late approval cannot run it.
