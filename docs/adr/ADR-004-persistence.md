# ADR-004: Backend persistence and admin surface

**Status:** accepted (2026-10-24)

**Decision.** SQLite (stdlib `sqlite3`) for metadata + filesystem artifacts under
`backend/data/sessions/<session-id>/`. No external database service.

**Consequences.** Device tokens are stored only as salted hashes. Every session/job/event/run id is bound
to the authenticated owner+device; knowing an id does not grant access (foreign ids → 404). Admin
enrolment endpoints (`/v1/admin/devices`, `/v1/admin/targets`) require a separate `OYSTER_ADMIN_TOKEN` and
are kept apart from ordinary device job access. `.env`, `data/`, and local databases are kept out of version control.

**Amendment (2026-10-24): listener audiences.** The LAN-facing HTTPS listener serves only the device routes
(health, time, sessions, events, assistant, runs, device jobs, job status/result, report) and refuses the owner
token with 403; `/v1/admin/*` and owner access exist only on the 127.0.0.1 HTTP listener
(`create_app(..., audience="device" | "loopback")`, one store, worker and sweeper shared). `oyster serve`
refuses an admin token that is empty, a placeholder or shorter than 24 characters. Failed authentications are
throttled per client address (more than 10 per minute: 429 for 60 s, in memory). SQLite runs with
`synchronous=FULL`; on startup interrupted AI runs are failed and their unstarted jobs cancelled.
