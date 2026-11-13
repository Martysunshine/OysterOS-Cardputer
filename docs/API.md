[OysterOS](../README.md) / [Docs](README.md) / Backend API

# Backend API contract (v1)

All bodies are JSON. Shapes are defined by `schemas/*.schema.json` and mirrored in
`backend/oyster/contracts.py`. Device requests and responses stay under 4 KiB; larger data is paginated.

## Listeners

| Listener | Bind | Used by | Serves |
| --- | --- | --- | --- |
| HTTPS | `OYSTER_BIND_HOST:8443`, dev-CA server certificate | Cardputer | **Device routes only**: `/v1/health`, `/v1/time`, `/v1/sessions` (+ `/events`, `/assistant/messages`, `/report`), `/v1/runs/{id}` (+ `/cancel`), `/v1/device/jobs`, `/v1/jobs/{id}/status`, `/v1/jobs/{id}/result`. No `/v1/admin/*` (404) and the owner token is refused (403). |
| HTTP | `127.0.0.1:8000` only | CLI, simulator, tests on the backend PC | Every route, including `/v1/admin/*` and owner access. |

`oyster serve` builds two apps over one store, one run worker and one expiry sweeper: the LAN-facing HTTPS app
(`audience="device"`) never exposes owner functions, so a leaked or guessed `OYSTER_ADMIN_TOKEN` is useless from
the LAN (ADR-004). Owner commands send the admin token only to loopback hosts unless `--allow-remote-admin`
is given.

## Authentication

- **Device:** `Authorization: Bearer <device-token>`. Issued once by `POST /v1/admin/devices`; stored only as a salted hash.
- **Owner:** `Authorization: Bearer <OYSTER_ADMIN_TOKEN>` for `/v1/admin/*` and for owner access to sessions, runs and reports (single-owner deployment), accepted **on the loopback HTTP listener only**. `oyster serve` refuses an admin token that is empty, a placeholder, shorter than 24 characters or too uniform; generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Bearer tokens are at most 512 characters (one limit for device and owner tokens).
- Every session, job, run and event id is bound to one device. A device presenting another device's id gets **404** (so ids cannot be probed). Missing/invalid token: **401**. Device token on an admin route: **403**. Owner token on the HTTPS (device) listener: **403** (and it counts as a failed authentication).
- **Throttling:** more than 10 failed authentications from one client address within 60 s block that address for 60 s: every authenticated request is answered **429** `rate_limited` with `Retry-After` (counters are in memory, per listener, reset by a restart).

## Endpoints

| Method and path | Auth | Request / response |
| --- | --- | --- |
| `GET /v1/health` | none | → `{"status":"ok","api":"v1"}`. Nothing sensitive. |
| `GET /v1/time` | none | → `{"unix": 1761300000, "iso": "2026-10-24T10:00:00Z"}`. A clock *hint* only (`clock_quality: backend_hint`); it is not endpoint authentication. |
| `POST /v1/admin/devices` | owner (loopback only) | `{"device_id","display_name","simulated":bool}` → `{"device_id","token"}`. The token is returned exactly once. Re-enrolling an existing id rotates (revokes) its token. |
| `POST /v1/admin/targets` | owner (loopback only) | `TargetProfile` → stored target. Raising `scope_version` makes older jobs stale. |
| `GET /v1/admin/targets` | owner (loopback only) | → `{"targets":[TargetProfile]}` |
| `POST /v1/sessions` | device | `{"session_id": optional, "source_mode": "live"/"simulated", "capabilities": [operation...], "room_label": optional}` → `{"session_id","created":bool}`. Idempotent for the same device. Device-generated ids (`sess-<hex8>-<nnn>`) are accepted so offline sessions can sync later; an id owned by another device → **409**. `capabilities` lists the operations this device actually implements; the AI is only offered those. |
| `POST /v1/sessions/{id}/events` | device | `{"events":[Event...]}` (≤ 4 KiB body) → `{"accepted":n,"duplicates":n,"acked_through":seq}`. Each event is validated against `event.schema.json`; `session_id`/`device_id` must match the URL and token. Idempotent by `event_id` (a duplicate with identical content is counted, a conflicting one → **409**). |
| `POST /v1/sessions/{id}/assistant/messages` | device (owner on loopback) | `{"question": "..."}` (≤ 1000 chars) → `{"run_id","status"}`. Starts an asynchronous AI run. If the provider is not configured → run status `ai_unavailable` (never a fabricated answer). Admission limits, each **429** `rate_limited` with `Retry-After`: one active (`queued`/`running`/`awaiting_*`) run per session, four queued runs overall, 30 runs per device per hour (`RUN_LIMIT_ACTIVE_PER_SESSION`, `RUN_LIMIT_QUEUED_TOTAL`, `RUN_LIMIT_PER_DEVICE_HOUR`). |
| `GET /v1/runs/{id}` | device or owner | → `run.schema.json` (bounded status, steps, answer, pending action, usage). `answer.findings` lists the evaluator findings (authoritative; the prose is not). A device response stays under 4 KiB: at most 16 findings, most decision-relevant first (confirmed, candidate, inconclusive, ...), the number dropped in `answer.findings_truncated`; the owner response is complete. |
| `POST /v1/runs/{id}/cancel` | device or owner | → `{"status"}`. Cooperative: queued jobs of the run become `cancelled`; a job already running finishes or stops per its own contract. |
| `GET /v1/device/jobs` | device | → `{"jobs":[Job...]}` (at most 1 per poll). Delivery moves the job `queued → delivered`. |
| `POST /v1/jobs/{id}/status` | device | `{"status":"awaiting_local_confirmation"/"running"}` → `{"status"}`. Only forward lifecycle transitions are accepted (**409** otherwise, including any report for a job that already ended, e.g. expired while awaiting confirmation). On a confirmation-gated job `running` is accepted only from `awaiting_local_confirmation` (**409** from `delivered`): the confirmation step cannot be skipped. A **409** means "do not execute": the device runs a confirmation-gated job only after its `running` report was accepted, and then submits no result. |
| `POST /v1/jobs/{id}/result` | device | `JobResult` → `{"status","duplicate":bool}`. Idempotent by `idempotency_key`: a retried identical submission returns the stored result, it never re-executes anything. All `event_ids` must already be uploaded to the job's session (**409** `events_missing` otherwise; the device uploads, then retries). On a confirmation-gated job only the executed outcomes `succeeded` and `unknown_outcome` must carry the approved `confirmation` with the job's `job_id` + `nonce`, and are accepted only while the job is `running` (which implies it passed through `awaiting_local_confirmation`); otherwise **409** `confirmation_required`; `failed`/`cancelled`/`expired`/`unsupported` (rejected or never run) are accepted without one. |
| `GET /v1/sessions/{id}/report?format=md` or `format=json` | device or owner | Markdown, or JSON per `report.schema.json`. Cites only evidence from this session or an explicitly linked baseline. |

More routes, documented with their own limits and tests:

- Pairing and read-only host health (`/v1/pair`, `/v1/hosts`, `/v1/admin/hosts`, `/v1/admin/pairing`, `/v1/admin/audit`; adapters `mock` and `local`, the backend PC read through psutil): [`COMPANION_API.md`](COMPANION_API.md).
- Control Deck (`/v1/deck/...`: profiles, slot runs, confirmation, history, owner administration; executors `mock` and `windows`, chosen by `OYSTER_DECK_EXECUTOR`): [`CONTROL_DECK.md`](CONTROL_DECK.md).
- Smart Home / Home Assistant bridge (`/v1/home/...`, summarised below): [`HOME_ASSISTANT.md`](HOME_ASSISTANT.md).

All three follow the listener rules above: device routes on both listeners, owner routes on the loopback listener only. The firmware modules that call them are Hosts, Control Deck and Smart Home.

### Home Assistant bridge routes (`/v1/home/...`)

| Method and path | Auth | Request / response |
| --- | --- | --- |
| `GET /v1/home/rooms` | device (owner on loopback) | `?limit=&after=` → `{"configured": bool, "reason"?, "rooms": [{"id","name","count"}], "next_after"}`. Always answers 200 with `configured` and, when it is false, a `reason`: `no_url`, `bad_url`, `no_token`, `bad_token`, `no_allowlist`, `bad_allowlist`, `no_rooms`. Home Assistant is not contacted when unconfigured. |
| `GET /v1/home/rooms/{room_id}/devices` | device (owner on loopback) | `?limit=&after=` → `{"room", "devices": [{"entity_id","label","state","impactful","actions"}], "next_after"}`, at most 8 per page. Only entities in the owner's allowlist file (`OYSTER_HOME_ALLOWLIST`, example `examples/home_allowlist.example.json`) can be read or controlled. |
| `POST /v1/home/actions` | **device token only** | `{"entity_id","action","confirmed"?}` with an `Idempotency-Key` header (required) → `{"entity_id","action","result":"ok","state","duplicate"}`. Locks, covers and alarm panels are always impactful and need `confirmed: true`, sent only after a key press on the device (alarm panels can only be armed). The owner token is refused. |
| `GET /v1/home/status` | owner (loopback only) | What is configured (`url_set`, `token_set`, `tls`, `ca_file_set`) and the allowlist problems; never the URL or the token. |
| `GET /v1/home/audit` | owner (loopback only) | Append-only audit, newest first, at most 50 per page; a `pending` row is written before each Home Assistant call. |

`HOME_ASSISTANT_URL` and `HOME_ASSISTANT_TOKEN` (and optionally `HOME_ASSISTANT_CA`) are read from `backend/.env`; they are never returned by any route, never logged and never sent to the device. Limits: 20 actions and 60 reads per minute per device (**429** `rate_limited`).

Error codes of these routes, in the usual `{"error","detail"}` body: `home_not_configured` (503), `home_unreachable` (503), `home_timeout` (503; on an action the outcome is unknown), `home_auth_failed` (502), `home_refused` (502), `home_upstream_error` (502), `confirmation_required` (409, nothing was sent), `action_not_allowed` (403), `idempotency_conflict` and `request_in_progress` (409), `audit_unavailable` (503, nothing was done), plus `not_found`, `validation_failed`, `payload_too_large` and `rate_limited` as elsewhere.

Errors are `{"error": "<code>", "detail": "<text>"}` with codes such as `unauthorized`, `forbidden`, `not_found`, `conflict`, `events_missing`, `validation_failed`, `stale_scope`, `payload_too_large`, `rate_limited` (**429**, with `Retry-After`: auth throttling or run admission limits). Request bodies are capped (4 KiB device, 16 KiB owner) whether or not a `Content-Length` is sent.

## Job lifecycle

```
queued -> delivered -> [awaiting_local_confirmation] -> running -> succeeded | failed | cancelled | expired | unknown_outcome
```

- Each job carries: `job_id`, `session_id`, `device_id`, `target_id`, `operation`, typed `arguments`, `scope_version`, `deadline_s`, `limits`, `requires_local_confirmation`, `nonce`.
- `requires_local_confirmation` is derived by the backend from the operation type, and for `ble_check_access_policy` from the selected assertion's `operation` in the enrolled lab profile (`write`/`transfer` are gated) (ADR-005). The device re-derives it independently and **never** lowers it based on this field.
- `expired`: deadline passed before a result arrived (backend sweeps), or the AI run stopped waiting for the local confirmation (status reason `confirmation_timeout`). A job that cannot have started (queued, `awaiting_local_confirmation`, or delivered and confirmation-gated) always ends `expired` at its deadline; only a job that may already be executing and changes the target becomes `unknown_outcome`. A late result for a job the backend ended after delivery is stored for audit but does not change the job or the run.
- When a run ends unsuccessfully, its queued jobs and a pending job that has not started (awaiting confirmation) become `cancelled` (status reason `run_cancelled` / `run_ended`); a job already running finishes per its own contract.
- `unknown_outcome`: the operation may have executed before connectivity was lost. Preserved until reconciled; target-changing operations are **never** retried automatically.
- The backend rejects: wrong device, unknown/foreign ids, stale `scope_version`, unsupported operations, arguments over limits, duplicate job ids. The device rejects the same set independently (`oyster_core/local_policy.h`).
- `ble_read_profile_value` accepts only characteristics whose documented read policy is `public`, and never `app_auth` (every read of it consumes the fixture's single challenge); protected reads are `ble_check_access_policy` assertions.
- At delivery the backend re-checks the target as it is now: a queued job whose target `scope_version` moved, whose operation is no longer in the target's `allowed_operations`, or whose arguments/confirmation class no longer fit the target's profile ends `failed` (status reason `stale_scope` / `target_policy_changed`) instead of being delivered.
- After a crash or restart the backend marks every non-terminal AI run `failed` ("backend restarted") and cancels those runs' jobs that cannot have started (reason `backend_restarted`); jobs that may be executing keep their state until a result or the deadline.
- Reports show model-written text only as an inert plain-text block labelled "Model explanation (not verified)" (links, images and HTML removed, URLs defanged); findings and the evidence index come only from stored records. A long session lists its newest 2000 events and states the omitted count.

## AI run lifecycle

```
queued -> running -> (awaiting_device | awaiting_local_confirmation)* -> succeeded | failed | cancelled | expired | budget_exhausted | ai_unavailable
```

Budgets (configurable, enforced in code): 12 tool actions, 6 provider requests, 180 s wall time excluding
time paused in local confirmation, 120 s maximum confirmation wait, 12 000 input / 1 500 output tokens per
provider request. One schema-repair attempt per invalid provider response, then the run fails visibly.
