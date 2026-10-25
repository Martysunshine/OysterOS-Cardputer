# Smart Control Deck (Issue #4)

Backend module for macro profiles with slots 1-9, confirmation of impactful actions, an idempotent job state
machine, an audit trail and history. Code: `backend/oyster/deck/`. Tests: `tests/deck/`.

## Status, stated plainly

- **Mock executor only.** The only executor that ships is `MockExecutor`: it records the typed action it was
  given and executes **nothing on any host**. Every job carries `"executor": "mock"` and every mock step
  summary says "nothing was executed on any host". The module proves the control path (allowlist,
  confirmation, idempotency, state machine, audit, authorization); it does not prove that a service was
  restarted or a machine woken. A real executor is future work (see "Executor contract").
- **Not verified on hardware.** The Control Deck screens D10, D11, D20, D21 and D22 are **not implemented in
  the firmware** (the Home tile still shows "not available in this build", see `docs/UI_SCREEN_MAP.md`). The
  device-side flow is meant to be shown by the host simulator (Issue #2) against this API; nothing here has
  run on a Cardputer.
- **Wiring.** The module is self-contained. It becomes part of the backend with one call, `install(app)`
  (see "Wiring"); until that call is made in `oyster.main.create_app` the routes are not served.

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

- `impactful` is a **class constant**, not a field. A request, a profile, a stored row or a server flag cannot
  set or lower it; a field named `impactful` is rejected like any other unknown field (ADR-005).
- Unknown action types are rejected when a profile is defined (422 `action_not_allowed`), again when a slot
  runs (409 `action_not_allowed`, no job is created) and a third time by the runner right before execution
  (job `failed`, reason `action_not_allowed`).
- There is no field that carries a command, a script, a path, a URL or an argument list. Host ids and service
  names are validated **names** (`[0-9a-zA-Z_-]{1,40}` and `[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}`); the executor
  resolves them through its own registry.

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

A real executor must: take only the typed models (never a command string or template); use no shell and no
string interpolation, choosing the host-side operation by action type from a fixed table and passing arguments
as discrete values; resolve `host_id` and `service` through its own registry and treat them as names, never as
addresses or paths; enforce the per-action timeout; run as a dedicated unprivileged account limited to the
listed hosts and services; return at most a short redacted summary; and raise `ActionOutcomeUnknown` whenever it
cannot tell whether an impactful action took effect.

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

Run them with `python -m pytest tests/deck -o addopts="" -q -p no:cacheprovider`. The tests use a fake clock,
deterministic ids and nonces, `MockExecutor` and temporary SQLite files; none uses the network.

## Known gaps

- No real executor, so no real result. Wake-on-LAN, service restarts and health checks are only simulated by
  the mock; the actions' semantics for a real host are specified above but untested.
- Firmware screens D10-D22 are not implemented; the device-side retry and "status unknown" reconciliation
  (UI flow D) are specified by this API (same Idempotency-Key, `GET /v1/deck/jobs/{id}`) but not built.
- The nonce proves "the same authenticated device, holding this job's nonce"; the physical key press on the
  device is not observable by the backend. The device must show the confirmation and send `approved` only after
  the user chose it (as for lab jobs, ADR-005).
- `unknown_outcome` has no reconcile endpoint; the owner reads it, checks the host and starts a new run.
- Scheduled automations (M6) and conditional macro steps are out of scope: a slot is one action or an ordered
  list of at most three.
- Audit rows are not pruned or exported; the file grows slowly without bound.
- A job approved just before a crash is reported `unknown_outcome` even if the runner never started it
  (deliberately conservative).
