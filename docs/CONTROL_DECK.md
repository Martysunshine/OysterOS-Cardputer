# Smart Control Deck (Issue #4)

Macro pad for the owner's own PC: profiles with slots 1-9, typed allowlisted actions, confirmation of
impactful actions on the Cardputer, an idempotent job state machine, an audit trail and history. Code:
`backend/oyster/deck/` (backend), `firmware/src/modules/deck.cpp` and
`firmware/lib/oyster_core/{include/oyster_core/deck_json.h,src/deck_json.cpp}` (device). Tests: `tests/deck/`,
`firmware/test/test_deck/`.

## Status, stated plainly

- **Two executors.** `MockExecutor` is the default: it records the typed action and executes **nothing on any
  host** (jobs carry `"executor": "mock"`, the device says "Mock executor: nothing was run on the PC").
  `WindowsExecutor` (`executor_windows.py` with the OS layer `os_windows.py`) acts on the machine the backend
  runs on; jobs carry `"executor": "windows"`. It is selected with `OYSTER_DECK_EXECUTOR=windows` (see
  "Configuration"); without that setting, and in every test run, nothing is executed.
- **Verified on the real PC, read-only parts only.** On one Windows 11 machine (not elevated) the real OS layer
  was constructed and asked for host health, the state of two real services and a missing one, the elevation
  state, and a restart of a service name that does not exist (answers "service not found" and touches
  nothing). **Not verified on a real session:** the media-key presses, the workstation lock, Wake-on-LAN
  packets and service restarts. Those are covered by unit tests against a fake OS layer only
  (`tests/deck/test_deck_executor_windows.py`); treat them as unverified until they have been run on the PC.
- **Firmware built, not verified on hardware.** The Control Deck module (screens D10, D11, D20, D21, D22) is
  implemented in the firmware and compiles; its parsers and texts are host-tested. It has **not run on a
  Cardputer** and has not talked to a real backend.
- **Wiring.** The module is self-contained. It becomes part of the backend with one call, `install(app)`
  (see "Wiring").

## Wiring

```python
from oyster.deck.mount import install

install(app)   # after errors.install(app); idempotent
```

- The device router (profiles list, run, confirm, cancel, job, history) is served on every app. The owner
  router (profile put/get/delete, job list, audit) is served only when `app.state.audience == "loopback"`,
  so the LAN-facing HTTPS listener never exposes owner functions (ADR-004), and the owner token is refused
  there as everywhere else.
- `install` creates (or reuses) one `DeckRuntime` per data directory per process, so the loopback and device
  apps of `oyster serve` share it: `deck.db`, the service, the runner and the rate limiters. The runner
  starts and stops with the app lifespan (reference counted) and also starts itself on the first submitted job,
  because `oyster serve` runs its apps with the lifespan off
  (`test_the_worker_starts_itself_on_the_first_job_when_no_lifespan_runs`); a clean worker stop at shutdown is
  `app.state.deck.stop()`. `install(app, runtime)` takes an explicit
  runtime, which is how the tests inject a clock, an executor and deterministic ids.
- Opening the runtime settles jobs a previous process left behind (see "Crash and restart").
- The executor of the shared runtime comes from the environment (`mount.executor_from_env`, see
  "Configuration"). An unknown `OYSTER_DECK_EXECUTOR` or an invalid setting stops start-up with a clear error
  (values are never echoed) instead of silently running with the mock. A pytest run never builds the real
  Windows layer on its own, even if the owner's `backend/.env` selects `windows`.

## Configuration

Read from the process environment (`backend/.env` is loaded into it by `load_settings`). Nothing is read from
a request, a profile or a job.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OYSTER_DECK_EXECUTOR` | `mock` | `mock` records and executes nothing; `windows` acts on this PC. |
| `OYSTER_DECK_LOCAL_HOST_ID` | `this-pc` | The one host id that means "this PC". Every other host id is refused (except Wake-on-LAN targets). Same pattern as a profile host id. |
| `OYSTER_DECK_WOL_HOSTS` | empty | `host_id=MAC;host_id=MAC`: the only source of Wake-on-LAN targets. A host not listed here cannot be woken. |
| `OYSTER_DECK_WOL_BROADCAST` | `255.255.255.255` | IPv4 broadcast address the magic packet is sent to (UDP port 9). |
| `OYSTER_DECK_RESTART_SERVICES` | empty | Comma-separated Windows service names the executor may restart, in addition to the profile's own `hosts[...].services`. Empty means no restart is ever attempted. |

## Model

### Allowlisted actions (`actions.py`)

Typed pydantic models (`extra="forbid"`, name-pattern-bounded strings), tagged by `type`:

| Type | Arguments | Impactful | Timeout ceiling |
| --- | --- | --- | --- |
| `noop` | none | no | 5 s |
| `host_health_check` | `host_id` | no | 15 s |
| `service_status` | `host_id`, `service` | no | 15 s |
| `wake_on_lan` | `host_id` | **yes** | 15 s |
| `service_restart` | `host_id`, `service` | **yes** | 60 s |
| `media_key` | `host_id`, `key` (`play_pause`, `next`, `prev`, `vol_up`, `vol_down`, `mute`) | no | 10 s |
| `lock_workstation` | `host_id` | **yes** | 15 s |

- `impactful` is a **class constant**, not a field. A request, a profile, a stored row or a server flag cannot
  set or lower it; a field named `impactful` is rejected like any other unknown field (ADR-005).
- Unknown action types are rejected when a profile is defined (422 `action_not_allowed`), again when a slot
  runs (409 `action_not_allowed`, no job is created) and a third time by the runner right before execution
  (job `failed`, reason `action_not_allowed`).
- There is no field that carries a command, a script, a path, a URL, an address or an argument list. Host ids
  and service names are validated **names** (`[0-9a-zA-Z_-]{1,40}` and `[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}`); the
  executor resolves them through its own registry. `key` is a closed set of six names; a Wake-on-LAN MAC
  address is not an action field at all (it comes from `OYSTER_DECK_WOL_HOSTS`).
- Media keys go through the system media keys, so any player that listens to them reacts (a music player, a browser
  tab, a video player); there is no OAuth and no player API. Which player answers is Windows' choice, and the
  job result only says the key was sent.

### Profiles (`profiles.py`)

```json
{
  "profile_id": "dev",
  "name": "Dev stack",
  "device_ids": ["pocket-01"],
  "hosts": { "mini-pc": { "services": ["nginx", "db"] } },
  "slots": {
    "1": { "label": "Health", "action": { "type": "host_health_check", "host_id": "mini-pc" } },
    "2": { "label": "Restart nginx",
           "actions": [ { "type": "service_status", "host_id": "mini-pc", "service": "nginx" },
                        { "type": "service_restart", "host_id": "mini-pc", "service": "nginx" } ] }
  }
}
```

| Rule | Limit |
| --- | --- |
| Slot keys | `"1"` to `"9"` only (`0`, `10`, `01`, `a` are rejected) |
| Slot content | `action` (one) **or** `actions` (ordered, 1 to 3); stored as `actions` |
| Labels and profile name | 1 to 24 printable ASCII characters |
| Bound devices | 1 to 16 unique device ids; only these devices see or run the profile |
| Hosts | at most 8, each with at most 16 service names |
| References | every action may only name a host listed in `hosts` and, for services, a service listed for that host |
| Profiles in total | 32 |

A slot is impactful when **any** of its actions is; one confirmation then covers the whole job. Replacing a
profile with different content, or deleting it, cancels its jobs that have not started (`queued`,
`awaiting_confirmation`; reason `profile_changed` / `profile_deleted`); a job already running is not touched.

## Job state machine (`jobs.py`)

```
queued -> awaiting_confirmation -> running -> succeeded | failed | unknown_outcome
queued -> running                                (non-impactful: no confirmation)
queued | awaiting_confirmation -> cancelled | expired
queued -> failed                                 (runner queue full, restart before the runner took it)
```

| From | Legal targets |
| --- | --- |
| `queued` | `awaiting_confirmation` (impactful only), `running` (non-impactful only), `cancelled`, `expired`, `failed` |
| `awaiting_confirmation` | `running`, `cancelled`, `expired` |
| `running` | `succeeded`, `failed`, `unknown_outcome` (impactful only) |
| `succeeded`, `failed`, `cancelled`, `expired`, `unknown_outcome` | none (final) |

`running` means the job was handed to the runner: an approved impactful job is `running` from the moment its
confirmation is accepted. A running job cannot be cancelled. The table is `jobs.TRANSITIONS`;
`can_transition` adds the impactful rules; the database also refuses a `state` outside the set.

| Deadline | Value | Result when exceeded |
| --- | --- | --- |
| Confirmation window | 120 s from creation | `expired` (`confirmation_timeout`); a later confirm is 409 `expired` |
| Waiting for the runner | 120 s | `expired` (`queue_timeout`) |
| Run budget | sum of the action timeouts + 10 s | impactful: `unknown_outcome`; otherwise `failed` (`run_timeout`) |
| One action | its timeout ceiling (table above) | impactful action: `unknown_outcome`; otherwise `failed` (`timeout`) |

Deadlines are applied lazily on every read and confirm, and by the runner thread while idle.

`unknown_outcome` (impactful actions only): the action may have run but its result was lost (timeout, lost
connection, unexpected executor error, crash). The job says `"outcome_known": false`. It is **never retried
automatically**; the same Idempotency-Key returns the stored job and a confirm is refused. The owner or user
checks the host and starts a new run with a new key.

## Idempotency

- `Idempotency-Key` header is required on run requests: 8 to 80 characters of `[A-Za-z0-9._:-]`.
- The key is scoped to the device. The identity of a request is `(profile_id, slot)`; the body is not part of it.
- Same key + same request: the stored job is returned with `"duplicate": true` (also while it is awaiting
  confirmation, with the same nonce, and after it finished). Nothing is queued again.
- Same key + a different profile or slot: **409** `conflict`; no job is created.
- A key is never used to start a second execution, whatever the state of the first job (including after a
  restart). A unique `(device_id, key_hash)` constraint backs this up; concurrent duplicates are serialised by
  the store's lock and produce one job.
- Only a hash of the key is stored.

## Local confirmation (ADR-005)

1. A run request for an impactful slot returns the job in `awaiting_confirmation` with a random `nonce`
   (visible only to the owning device, only while awaiting) and `confirm_deadline`. Nothing is queued.
2. `POST /v1/deck/jobs/{id}/confirm` with `{"job_id", "nonce", "approved"}` from the **same authenticated
   device**. The body is strict: unknown fields and a non-boolean `approved` are 422.
3. Rejected: unknown or foreign job (404), `job_id` not matching the URL (422), wrong nonce (403, compared in
   constant time, never echoed), job not awaiting (409 `already_decided`), window closed (409 `expired`, and the
   job becomes `expired`: a late confirm cannot start it). The nonce is single use and cleared once decided.
4. `approved: false` cancels the job. `approved: true` moves it to `running` and hands it to the runner.
5. Anything claiming a confirmation in the **run request** (`confirmed`, `approved`, `nonce`, `impactful`, ...)
   is never read; the run body is optional, at most 1 KiB, and has no effect.

Confirmation requirements come only from the action types in the slot. No stored or supplied flag lowers them.

| Action | Confirmation on the Cardputer | Why |
| --- | --- | --- |
| `noop`, `host_health_check`, `service_status` | no | read-only |
| `media_key` | no | changes playback or volume only; no session state, repeatable by pressing again |
| `lock_workstation` | **yes** | ends the user's interactive session |
| `wake_on_lan` | **yes** | powers a machine on |
| `service_restart` | **yes** | interrupts a service |

A slot with several actions asks once if any of them asks. On the device the confirmation page names the slot
label, the target host (`this-pc` is shown as "this PC") and what the impactful steps do (built from the job's
step types, hosts and services, never from free text); read-only steps of the same slot are not part of the
question.

## API

Errors are `{"error": "<code>", "detail": "<text>"}` as in `docs/API.md`: 401 missing or invalid token, 403
wrong role, 404 unknown id **or an id of another device**, 409 conflict/state, 413 body too large, 422
validation, 429 rate limit (with `Retry-After`), 503 runner queue full (with `Retry-After`).

| Method and path | Auth | Notes |
| --- | --- | --- |
| `GET /v1/deck/profiles` | device (owner: all, full detail) | Device: profiles bound to it, compact (`profile_id`, `name`, slots with `label` and `impactful`). `limit` (max 5 for a device), `after` cursor, `next_after` in the reply. |
| `POST /v1/deck/profiles/{id}/slots/{n}/run` | device | `n` in 1..9 (else 422). Header `Idempotency-Key`. Unknown profile, unbound device and empty slot are the same 404. 20 per minute per device. |
| `POST /v1/deck/jobs/{id}/confirm` | device | See above. Shares a 60 per minute budget with cancel. |
| `POST /v1/deck/jobs/{id}/cancel` | device | `queued` or `awaiting_confirmation` only; already cancelled answers 200; otherwise 409 `not_cancellable`. |
| `GET /v1/deck/jobs/{id}` | device (own) or owner | Job with `state`, `requires_confirmation`, `steps`, `reason`, `executor`. |
| `GET /v1/deck/history` | device (own entries) or owner (all) | `limit` 1..50 (default 10), `before` cursor (`next_before` in the reply), owner may filter by `device_id` and `job_id`. |
| `PUT /v1/deck/profiles/{id}` | owner, loopback only | Body is the profile; `profile_id` must match the URL. Reply `result`: `created`, `updated`, `unchanged`. |
| `GET /v1/deck/profiles/{id}` | owner, loopback only | |
| `DELETE /v1/deck/profiles/{id}` | owner, loopback only | |
| `GET /v1/deck/jobs` | owner, loopback only | `limit` 1..50, `before`, `device_id`, `state`. Never contains a nonce. |
| `GET /v1/deck/audit` | owner, loopback only | Same paging as history, all entries, with actor and key hash. |

Job reply (device view, awaiting confirmation):

```json
{"job_id": "dj-0a1b2c3d4e5f6071", "profile_id": "dev", "slot": 2, "state": "awaiting_confirmation",
 "requires_confirmation": true, "executor": "mock", "created_at": 1800000000, "confirm_deadline": 1800000120,
 "nonce": "…", "steps": [{"index": 0, "type": "service_status", "host_id": "mini-pc", "service": "nginx",
 "outcome": "pending"}, {"index": 1, "type": "service_restart", "host_id": "mini-pc", "service": "nginx",
 "outcome": "pending"}], "duplicate": false}
```

Step `outcome` is `pending`, `succeeded`, `failed`, `unknown` or `skipped` (after an earlier step stopped the
job). Step `summary` is at most 120 printable ASCII characters from the executor and must be redacted by it.
Device responses are bounded by construction (at most 3 steps) and stay under 4 KiB; the default history page
(10 entries) does too; a page of 50 does not, so a device should ask for what its screen needs.

## Device flow (Cardputer, `firmware/src/modules/deck.cpp`)

Home tile 4. The module draws one page at a time through the module framework (`firmware/include/modules.h`);
network work runs on the worker task, the UI never blocks. Not run on a real device yet.

| Page | Screen | Content and keys |
| --- | --- | --- |
| Profiles | D10 | Rows: the profiles bound to this device (`GET /v1/deck/profiles?limit=5`, `next_after` paging with "More profiles..." and "Previous profiles" rows), then "History". Enter opens, `r` reloads. The "Claude Code" row sits after the profiles (see "Claude Code row"). |
| Slots | D11 | `1  Label` per slot, `*` after the number marks an impactful slot (it asks first). Keys `1`-`9` or Enter run the slot at once. |
| Working | | "Sending to the backend...". Back stops waiting (the action may still run: see History). |
| Confirm | D20 | Operation = slot label, target = host (`this-pc` shown as "this PC"), effect = what the impactful steps do. Enter approves, Back or the emergency cancel rejects, running out of the window (the backend's 120 s minus 3 s) rejects. The UI ignores keys for the first 700 ms. |
| Result | D21 | Polls `GET /v1/deck/jobs/{id}` every 1.5 s until the job ends (at most about 2.5 minutes), then shows state, each step with its summary, the reason and, for the mock, "Mock executor: nothing was run on the PC." |
| History | D22 | `GET /v1/deck/history?limit=10`, finished entries only (the queued, running and awaiting rows of a job are dropped), newest first. Enter opens the job in the Result page. |

Rules the device follows:

- **One Idempotency-Key per key press**, `dk-` plus 24 hex digits from the hardware random generator and the
  millisecond clock. The run request is sent with `idempotent=false` (the client never resends it by itself).
  The key is kept only to repeat the **same** press: if the answer was lost, the Result page says "Status
  unknown" and Enter sends the same request with the same key, which returns the job the backend already has
  or creates it once. A new key press always gets a new key.
- **The nonce** returned for this device stays in module RAM, is never displayed or logged, goes only into the
  confirm body together with the job id, and is wiped when used, rejected or left behind.
- **Approving** sends `POST /v1/deck/jobs/{id}/confirm` once (not retried). **Rejecting** sends
  `POST /v1/deck/jobs/{id}/cancel`. If the device is left with Home while a confirmation is open, the job is
  not answered by the device; it expires on the backend after its window, and the next time the module is
  opened the leftover confirmation is cancelled.
- **Texts** (all plain, no hang): backend offline or not set up ("Nothing was sent"), device clock not set,
  rejected device token (401/403), not found (404), conflicts by code (`expired`: "The confirmation window
  closed. Nothing ran.", `already_decided`, `not_cancellable`, `conflict`), rejected request (413/422), rate limit
  (429, "Wait N s"), backend busy (503), other server errors. `unknown_outcome` says the action may have run and
  is never retried. A reply the device cannot parse is rejected whole, not shown in part.
- **Home tile**: "N profiles" / "no profile" / "offline"; "checking..." until the first answer and "error" when
  the backend refused the list. The tile fetches the first page once in the background when Home is drawn, and
  retries on a later look at Home only after 30 s and only if that failed.

## Claude Code row (`deck.cpp`, `oyster_core/claude_json.*`)

Answer "may Claude Code run this tool?" from the Cardputer and read a one-line usage status. Backend contract:
`docs/CLAUDE_CODE_LINK.md`. **Status: compiles and is host-tested against a scripted fake backend
(`firmware/test/test_claude/`). Not run on a Cardputer, not run against the real backend routes, never against a
live Claude Code session.**

| Page | Content and keys |
| --- | --- |
| First page, row `Claude Code  <state>` | After the profiles. State: `N pending`, `none pending`, `checking...`, `offline` (no network or no answer), `not set up` (404: a backend without the link), `not accepted` (401/403), `error` (other or unreadable reply). Read once when the module opens and again on `r`; never on a redraw. The row is offered once the profile list or the approvals list was read, so a device with no profile still has it. Enter opens the approvals page, `s` the usage page. |
| Approvals | Up to 5 rows `tool summary` clipped to the row, remaining seconds at the right (counted down locally from `expires_in_s`, in backend order = newest first); with none pending a note row ("No requests pending", or why the list is unavailable); last row: the usage line, e.g. `Usage Opus $0.0123 ctx 8%`. Enter on a request opens Confirm, on the usage row the usage page. `r` refreshes, `s` opens usage. The page asks again every 3 s (usage every 15 s) **only while it is open**: the module's `tick` is only called for the module on screen, so Home, Help and other modules cause no polling. A request whose window closed disappears at once. The selection restarts when the set of pending requests changes, so Enter never lands on a request that moved. |
| Confirm | Operation `Claude Code: <tool>`, target `Details shortened`, effect = the summary (an empty one shows `(no details)`), followed by `Allow once. Deny what you cannot judge.` when rows are left. The confirm widget shows the target on one row only, so the summary is in the effect field (three wrapped rows; if word wrap would need a fourth, it is cut at the row width so all 80 characters stay visible). Enter = allow once, Back, the emergency cancel and Home = deny. The UI ignores keys for the first 700 ms. When the window closes the page becomes "Expired" and nothing is sent. |
| Result | `Allowed` / `Denied` (the answer was recorded on the backend; for allow: Claude Code may still ask or refuse by its own rules), or a plain failure: `Too late` (409 expired), `Already answered` (409), `Rejected` (403 stale code, 401), `No longer there` (404), `Rate limit` (429, "Wait N s"), `Not sent` (offline, clock not set, radio busy, worker queue full) and `Status unknown` (no usable answer). |
| Usage | Text page from `GET /v1/claude/status`: model, cost (labelled estimate), context %, the two plan percentages **only when present**, age (kept counting between refreshes), or "No Claude Code status received yet (see docs/CLAUDE_CODE_LINK.md)". Refreshes every 10 s and on `r`. |

Rules the device follows:

- **One answer per press, never retried.** `POST /v1/claude/approvals/{id}/decide` with the nonce made for this
  device and approval, `idempotent=false`, no key. If the reply is lost (or unreadable) the page says "Status
  unknown", the answer is not sent again, and the request stays in the list until a refresh says otherwise.
- **The nonce** lives in module RAM only, is never shown or logged, is wiped when used, when the page closes, and
  when the module is entered again; the worker's reply scratch is wiped after every call.
- **Honest limits on screen:** the device sees at most 80 characters of a command, so the Confirm page says
  "Details shortened" and advises denying what you cannot judge.
- **Memory:** state 1696 bytes plus 3072 bytes of worker scratch (one static reply buffer instead of a heap
  block per refresh). No new tasks or timers.
- Unverified: the key feel and layout on the real 240x135 screen, behaviour against the real backend, TLS
  timing of the extra calls, and everything that happens inside Claude Code after the answer.

## Audit and history (`audit.py`)

One append-only row per job state change (`job.queued`, `job.awaiting_confirmation`, `job.running`,
`job.succeeded`, `job.failed`, `job.cancelled`, `job.expired`, `job.unknown_outcome`), per denied request
(`request.denied` with a reason such as `not_bound`, `empty_slot`, `idempotency_conflict`, `bad_nonce`,
`not_awaiting`, `expired`, `unknown_job`, `action_not_allowed`, `busy`) and per owner profile change
(`profile.put`, `profile.deleted`). A row holds who (device id, `owner`, or `system` for deadlines, the runner
and startup recovery), what (profile id, slot, action **type names**), when, outcome, job id and a SHA-256 of
the idempotency key. It never holds a token, a nonce, the plain key, an action argument or a request body.
The state change and its row commit in one transaction.

`audit.py` contains only `INSERT` and `SELECT`, and the table has triggers that abort any modification or
removal. Rate-limited requests are not audited (they are bounded by the limiter and would only grow the table);
unauthenticated requests are handled by the existing authentication throttle. Rows are never pruned
automatically, so the file grows by roughly 250 bytes per state change.

## Storage (`store.py`)

`deck.db` in `settings.data_dir`, separate from `oyster.db`: tables `profiles`, `jobs`, `audit`, `meta`. One
connection, WAL, `busy_timeout`, `synchronous=FULL`, an `RLock`, every write in a `BEGIN IMMEDIATE`
transaction, table creation idempotent. The nonce of an awaiting job is stored in clear text (it is single use,
short lived and bound to its job and device); the idempotency key is stored only as a hash.

## Runner and executor contract (`runner.py`, `actions.py`)

One worker thread drains a bounded queue (16 by default). A full queue is a 503 before a job is created (or, in
a race, a job that ends `failed` with reason `queue_full`). For each job the runner claims it through the service
(a cancelled, expired or already started job is skipped: a job cannot run twice), re-validates each action
against the allowlist, and executes the actions in order, each with its timeout in a helper thread. The first
failed action stops the job and the rest are `skipped`. Outcomes:

| Executor result | Impactful action | Non-impactful action |
| --- | --- | --- |
| `ActionResult(ok=True)` | step succeeded | step succeeded |
| `ActionResult(ok=False)` or `ActionFailed` | job `failed` | job `failed` |
| timeout, `ActionOutcomeUnknown`, any other exception, a non-`ActionResult` value | job `unknown_outcome` | job `failed` |

Nothing is retried. Exception messages from the executor are dropped (they may carry host details).

### The Windows executor (`executor_windows.py`, `os_windows.py`)

`WindowsExecutor` implements `ActionExecutor` (`kind = "windows"`) on top of an `OsLayer` protocol that holds
**all** operating-system access; `WindowsOsLayer` (Win32 through `ctypes`, `psutil`, one UDP socket) is the only
module of the package that touches the OS, and the test `test_only_the_os_layer_uses_ctypes` keeps it that way.
There is no shell, no child process, no code evaluation and no path or command taken from a request.

| Action | What happens | Results |
| --- | --- | --- |
| `noop` | nothing | `ok` |
| `host_health_check` | CPU, memory and system-drive percentages (psutil) | `ok`, summary `cpu 12% mem 63% disk 71%` |
| `service_status` | state of one Windows service (psutil) | `ok` with `Name: running`; `service_not_found`; `access_denied` |
| `media_key` | presses and releases one media key with `SendInput` | `ok` ("key sent; player reaction not checked"); `input_blocked` |
| `lock_workstation` | `LockWorkStation()` | `ok` ("lock request accepted by Windows"); `lock_failed` |
| `wake_on_lan` | one magic packet to the broadcast address, MAC from `OYSTER_DECK_WOL_HOSTS` | `ok` ("magic packet sent; wake-up not verified"); `host_not_allowed`; `wol_send_failed` |
| `service_restart` | stop, wait, start, wait (20 s each) through the service control manager | `ok`; `service_not_allowed`; `needs_admin`; `restart_incomplete`; unknown outcome when a state is not reached in time |

Rules:

- **One host.** Everything except Wake-on-LAN is refused with `host_not_local` ("refused: this executor only
  controls this PC") unless `host_id` equals `OYSTER_DECK_LOCAL_HOST_ID`. Nothing is sent to another machine.
- **Wake-on-LAN.** The MAC address is looked up by `host_id` in `OYSTER_DECK_WOL_HOSTS`; an unlisted host is
  refused with `host_not_allowed`. The action model has no address field, so a request cannot carry one.
- **Service restart** needs three things: the service listed in the profile (validated at definition and at
  run), the service listed in `OYSTER_DECK_RESTART_SERVICES` (empty by default: nothing can be restarted), and
  an already elevated backend process. Without administrator rights the job ends `failed` with
  `needs_admin` and the step summary "needs administrator rights; nothing was restarted". The executor never
  asks for elevation (no UAC prompt, no `runas`); to allow restarts, start the backend elevated yourself.
- **Honest results.** A refusal or a failure with a known effect is `ok=False` (job `failed`, nothing
  happened). A result that cannot be told (a service state not reached in time, the OS layer raising
  `ActionOutcomeUnknown` or anything unexpected) propagates, so the runner ends an impactful job
  `unknown_outcome`; a non-impactful one `failed`. Exception text is dropped by the runner.
- **Rights.** It runs with the rights of the account that started the backend, which must be the interactive
  user's session for media keys and the lock to reach the desktop. A backend started as a Windows service or
  in another session cannot press keys on your desktop; `SendInput` then reports `input_blocked`.
- **Volume** moves one Windows step per key press (about 2 %): press the slot repeatedly.

### Seed profile (`seed.py`)

`ensure_desk_profile(runtime_or_service, device_ids, local_host_id="this-pc")` defines the profile `desk`
("Desk") through the normal service path, so validation and audit apply, and returns `created`, `updated` (the
content differed: unstarted jobs of the old profile are cancelled) or `unchanged`. Calling it twice changes
nothing. The slots:

| Slot | Label | Action | Asks first |
| --- | --- | --- | --- |
| 1 | PC health | `host_health_check` | no |
| 2 | Play/Pause | `media_key` `play_pause` | no |
| 3 | Next | `media_key` `next` | no |
| 4 | Prev | `media_key` `prev` | no |
| 5 | Vol+ | `media_key` `vol_up` | no |
| 6 | Vol- | `media_key` `vol_down` | no |
| 7 | Mute | `media_key` `mute` | no |
| 8 | Lock PC | `lock_workstation` | **yes** |

### Contract for any executor

A real executor must: take only the typed models (never a command string or template); use no shell and no
string interpolation, choosing the host-side operation by action type from a fixed table and passing arguments
as discrete values; resolve `host_id` and `service` through its own registry and treat them as names, never as
addresses or paths; enforce the per-action timeout; use no more rights than the account that started the
backend (never elevate itself); return at most a short redacted summary; and raise `ActionOutcomeUnknown`
whenever it cannot tell whether an impactful action took effect.

## Crash and restart

Opening the runtime calls `recover_after_restart`: a job found `running` is settled `unknown_outcome` if it is
impactful and `failed` otherwise; a job found `queued` is `failed` (reason `backend_restarted`); nothing is
re-run. Settling is conservative: an approved impactful job whose runner never started it is also
`unknown_outcome`. Jobs `awaiting_confirmation` keep their nonce and deadline and can still be confirmed (or
expire).

## Limits

| What | Limit |
| --- | --- |
| Run requests | 20 per minute per device (429) |
| Confirm and cancel requests | 60 per minute per device (429) |
| Run request body | 1 KiB, never read |
| Confirm body | 4 KiB (device body limit); profile body 16 KiB (owner) |
| Runner queue | 16 jobs |
| History / job pages | 50 per page, default 10 |

## Acceptance criteria mapped to tests

Issue #4: "Macro profiles with slots 1-9, confirmation dialog, job state transitions, idempotency,
authorization, audit and history. Only allowlisted typed actions."

| Issue requirement | Module / endpoint | Tests (`tests/deck/`) |
| --- | --- | --- |
| Macro profiles, slots 1-9 | `profiles.py`; `PUT/GET/DELETE /v1/deck/profiles/{id}`, `GET /v1/deck/profiles` | `test_deck_profiles.py`: `test_define_get_list_and_delete_roundtrip`, `test_slot_numbers_outside_1_to_9_are_rejected_at_definition`, `test_slots_1_and_9_are_accepted_and_runnable`, `test_running_slot_0_or_10_is_rejected`, `test_slot_labels_are_bounded_printable_ascii`, `test_a_slot_holds_one_action_or_an_ordered_list_of_at_most_three`, `test_actions_may_only_reference_listed_hosts_and_services`, `test_structural_limits`, `test_device_sees_only_profiles_bound_to_it_in_compact_form`, `test_device_profile_list_is_paginated_and_stays_under_4_kib`, `test_replacing_a_profile_cancels_unstarted_jobs_but_not_running_ones`, `test_deleting_a_profile_cancels_its_unstarted_jobs` |
| Only allowlisted typed actions | `actions.py`; checked at definition, at run and in the runner | `test_deck_actions.py`: `test_allowlist_is_exactly_the_documented_set`, `test_unknown_action_types_are_rejected`, `test_unknown_action_type_is_rejected_when_a_profile_is_defined`, `test_unknown_action_type_in_a_stored_profile_is_rejected_when_the_slot_runs`, `test_a_tampered_job_row_is_refused_by_the_runner`, `test_extra_fields_are_rejected`, `test_host_ids_are_validated_names`, `test_service_names_are_validated_names`, `test_no_shell_or_exec_like_field_exists_on_any_action_model`, `test_package_source_has_no_shell_exec_or_eval`, `test_impact_is_fixed_per_action_type`, `test_impact_cannot_be_supplied_or_overridden` |
| Confirmation dialog (backend side of D20) | `DeckService.confirm`; `POST /v1/deck/jobs/{id}/confirm` | `test_deck_confirmation.py`: `test_impactful_run_waits_for_confirmation_and_executes_nothing`, `test_approval_with_the_right_device_job_and_nonce_runs_it_once`, `test_wrong_nonce_is_rejected_and_the_job_stays_confirmable`, `test_a_nonce_is_bound_to_its_own_job`, `test_another_device_cannot_confirm_even_with_the_nonce`, `test_a_late_confirmation_cannot_start_an_expired_job`, `test_an_already_decided_job_cannot_be_confirmed_again`, `test_a_confirmation_carried_in_the_run_request_is_ignored`, `test_a_stored_flag_cannot_lower_the_requirement`, `test_confirm_requests_reject_unknown_flags_and_non_boolean_approvals`, `test_requirement_comes_from_the_action_types_in_the_slot`, `test_non_impactful_jobs_skip_confirmation` (state machine file) |
| Job state transitions | `jobs.py` (`TRANSITIONS`, `can_transition`) | `test_deck_state_machine.py`: `test_every_pair_of_states_is_either_legal_or_illegal_exactly_as_documented` (all 64 pairs x impactful/non-impactful), `test_terminal_states_are_final`, `test_the_database_refuses_a_state_outside_the_table`, `test_service_refuses_an_illegal_transition`, `test_impactful_path_queued_awaiting_running_succeeded`, `test_queued_to_cancelled_and_never_started`, `test_confirmation_window_is_120_seconds`, `test_queued_job_expires_when_the_runner_never_takes_it`, `test_running_non_impactful_job_past_its_budget_fails_and_impactful_becomes_unknown`, `test_a_job_is_claimed_for_execution_once` |
| Idempotency | `DeckService.run_slot`; `Idempotency-Key` header; unique `(device_id, key_hash)` | `test_deck_idempotency.py`: `test_the_key_header_is_required`, `test_key_length_and_charset`, `test_replay_returns_the_stored_job_and_executes_once`, `test_same_key_with_a_different_request_is_a_conflict`, `test_a_key_is_never_reused_after_completion`, `test_keys_are_scoped_to_the_device`, `test_concurrent_duplicate_runs_execute_once`, `test_concurrent_duplicate_impactful_runs_make_one_job_and_one_nonce`, `test_concurrent_confirmations_start_the_job_once`; `test_deck_restart.py`: `test_a_replay_after_a_restart_returns_the_stored_job_without_executing`, `test_the_unique_key_constraint_backs_up_idempotency` |
| Authorization | `oyster.auth` dependencies in `api.py`; device vs owner routers | `test_deck_api_authz.py`: `test_missing_token_is_401_on_every_route`, `test_invalid_token_is_401_on_every_route`, `test_owner_token_is_403_on_device_only_routes`, `test_device_token_is_403_on_owner_only_routes`, `test_owner_routes_work_for_the_owner`, `test_a_device_that_is_not_bound_gets_404_not_403`, `test_a_foreign_job_id_is_404_for_every_job_route`, `test_a_device_sees_no_foreign_profiles_or_history`, `test_device_listener_serves_device_routes_only`, `test_a_rotated_token_stops_working`, `test_error_bodies_are_uniform_and_generic`; rate limits: `test_run_requests_are_rate_limited_per_device`, `test_confirm_attempts_are_rate_limited` |
| Audit | `audit.py`, `store.py`; `GET /v1/deck/audit` | `test_deck_audit.py`: `test_every_state_change_writes_one_audit_entry`, `test_entries_say_who_what_when_outcome_job_and_key_hash`, `test_denied_requests_are_audited`, `test_profile_changes_are_audited_for_the_owner`, `test_audit_never_contains_secrets`, `test_audit_module_has_no_modifying_statements`, `test_database_refuses_to_modify_or_remove_audit_rows` |
| History | `GET /v1/deck/history`, `GET /v1/deck/jobs` | `test_deck_audit.py`: `test_a_device_sees_only_its_own_history_and_the_owner_sees_all`, `test_history_is_newest_first_paginated_and_capped_at_50`, `test_owner_job_list_is_paginated_and_filterable` |
| Failures, timeouts, lost results (M5: "no double run") | `runner.py` | `test_deck_failures.py`: `test_executor_failure_ends_failed_for_both_kinds`, `test_unknown_result_of_an_impactful_action_is_unknown_outcome`, `test_unknown_result_of_a_read_only_action_is_a_plain_failure`, `test_an_unknown_outcome_is_never_retried_automatically`, `test_a_hanging_executor_is_cut_off_by_the_runner_timeout`, `test_the_first_failed_step_stops_the_job_and_later_steps_are_skipped`, `test_executor_error_text_is_never_exposed`, `test_the_runner_queue_is_bounded` (idempotency file), `test_the_worker_thread_runs_jobs_and_stops_cleanly` |
| Crash and restart | `DeckService.recover_after_restart`, `DeckRuntime` | `test_deck_restart.py`: `test_jobs_found_running_or_queued_after_a_restart_are_settled_and_never_re_run`, `test_the_unknown_outcome_after_a_restart_is_visible_and_not_retriable`, `test_a_job_awaiting_confirmation_survives_a_restart_with_its_deadline_and_nonce`, `test_an_awaiting_job_whose_window_passed_during_the_outage_expires`, `test_recovery_is_idempotent_and_audited`, `test_store_pragmas_and_idempotent_creation` |
| Own database file | `store.py` | `test_the_deck_has_its_own_database_file`, `test_store_pragmas_and_idempotent_creation` (`test_deck_restart.py`) |
| Real executor for the owner's PC, against a fake OS layer | `executor_windows.py`, `os_windows.py`, `mount.executor_from_env` | `test_deck_executor_windows.py`: every media key, lock, health, service status, Wake-on-LAN (allowlist only, no request field can carry a MAC), restart (allowlist, `needs_admin`, elevated), refusals for any other host, known failure vs unknown outcome, hang cut off by the runner, jobs labelled `windows`, configuration parsing and executor selection (unknown name stops start-up, pytest never builds the real layer), and read-only probes of the real layer (`test_the_real_layer_answers_read_only_questions`, `test_restarting_a_service_that_does_not_exist_changes_nothing`) |
| New action types stay on the allowlist | `actions.py` | `test_deck_actions.py`: `media_key` / `lock_workstation` impact, key set, unknown keys, missing arguments; `test_only_the_os_layer_uses_ctypes` |
| Seed profile `desk` | `seed.py` | `test_deck_seed.py`: slots and labels, only slot 8 asks, idempotency (`created`, `unchanged`, `updated`), device view, mock run |
| Device parsers and texts | `firmware/lib/oyster_core/src/deck_json.cpp` | `firmware/test/test_deck/` (Unity, native): profiles, jobs, history and error bodies, bounds, hostile input (truncation at every length, 3000 random byte mutations, nesting, oversized values, NUL bytes), request paths and the confirm body, key format, screen texts |

Run the backend tests with `python -m pytest tests/deck -o addopts="" -q -p no:cacheprovider` and the device
parser tests with `pio test -e native -f test_deck` (from `firmware/`, host compiler on PATH, see
`scripts/_env.ps1`). The backend tests use a fake clock, deterministic ids and nonces, `MockExecutor` or a fake
OS layer and temporary SQLite files; none uses the network, presses a key or locks a screen.

## Known gaps

- **Unverified on a real session:** media-key presses, the workstation lock, Wake-on-LAN packets and service
  restarts have only been exercised against a fake OS layer. Whether a given player reacts depends on
  Windows routing the media key to it; the job result only says the key was sent. Whether a woken host came up
  is not checked.
- Service restarts need the backend to run elevated and a service on `OYSTER_DECK_RESTART_SERVICES`; the
  `ctypes` service-control code has been checked only against a service name that does not exist.
- The device module compiles and its parsers are host-tested, but it has not run on a Cardputer or against a
  running backend. Leaving the module with Home while a confirmation is open does not tell the module (the
  job expires on the backend; the leftover is cancelled the next time the module opens).
- The nonce proves "the same authenticated device, holding this job's nonce"; the physical key press on the
  device is not observable by the backend. The device must show the confirmation and send `approved` only after
  the user chose it (as for lab jobs, ADR-005).
- `unknown_outcome` has no reconcile endpoint; the owner reads it, checks the host and starts a new run.
- Only the local PC is controlled; there is no remote host executor (SSH, WinRM) and no per-host credential
  handling. A profile that names another host fails with `host_not_local`.
- Scheduled automations (M6) and conditional macro steps are out of scope: a slot is one action or an ordered
  list of at most three.
- Audit rows are not pruned or exported; the file grows slowly without bound.
- A job approved just before a crash is reported `unknown_outcome` even if the runner never started it
  (deliberately conservative).
