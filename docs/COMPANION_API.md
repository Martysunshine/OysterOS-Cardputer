# Companion API: pairing and read-only host health (Issue #3)

Status: **implemented and tested against mock adapters only.** The package is `backend/oyster/companion/`. There
is no real Linux adapter, nothing in it has touched a real host, and none of it has run on the Cardputer
(no firmware client exists). It implements the P1 item "typed authenticated API, pairing, `/health` and `/hosts`
health, only authorized host metrics, no arbitrary remote shell execution" from the design plan (sections 5 and
6) and milestone M4 of [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md).

It extends the backend described in [`API.md`](API.md): same bearer tokens, same error body
(`{"error","detail"}`), same 401 / 403 / 404 / 429 meanings, same 4 KiB device bodies.

## Wiring

```python
from oyster.companion.mount import install
install(app)          # after create_app(...); safe to call twice
```

- On the loopback app (`audience="loopback"`) all routes below exist. On the LAN-facing device app
  (`audience="device"`) only the device routes exist; the owner routes answer 404 and the owner token is
  refused with 403, as for every other device route.
- The routes are added; the existing application lifespan is wrapped, not replaced. The companion store opens at
  start-up (and lazily on first use) and closes at shutdown.
- Authentication reuses `oyster.auth` through the same dependencies the existing routers use. Device
  enrolment at pairing reuses `app.state.store.create_device`.
- `oyster serve` builds two apps. Calling `install` on both gives each its own connection to the same
  `companion.db`; pairing state changes are single conditional statements, so a code can still be claimed once.

## Relation to the existing `GET /v1/health`

`GET /v1/health` (unauthenticated, `{"status":"ok","api":"v1"}`) is unchanged and not redefined. It tells a
client that the backend is up. The companion adds `GET /v1/hosts` and `GET /v1/hosts/{id}/health`, which are the
*hosts'* health and always need a device token. The design plan's richer `/v1/health` (version, capabilities)
is not implemented; the device reports its own capabilities per session.

## Endpoints

| Method and path | Auth | Request | Response |
| --- | --- | --- | --- |
| `GET /v1/hosts` | device | none | `{"hosts":[{"host_id","display_name"}]}` for the hosts shared with this device |
| `GET /v1/hosts/{host_id}/health` | device | none | `HealthReport` (below); `502` host-unavailable body when the adapter fails |
| `POST /v1/pair` | none, throttled | `{"code","device_id","display_name"?}` | `{"device_id","token"}`; the token is shown once |
| `POST /v1/admin/hosts` | owner, loopback only | `HostEnrol` | `{"host": HostRecord, "created": bool}`; same `host_id` replaces |
| `GET /v1/admin/hosts` | owner, loopback only | none | `{"hosts":[HostRecord]}` with allowlists and device ids |
| `DELETE /v1/admin/hosts/{host_id}` | owner, loopback only | none | `{"deleted": host_id}`; 404 when unknown |
| `POST /v1/admin/pairing` | owner, loopback only | `{"device_id"?, "simulated"?}` (send `{}` for none) | `{"pairing_id","code","ttl_s","expires_at","device_id"}`; the code is shown once |
| `GET /v1/admin/audit` | owner, loopback only | `?limit=1..200&before_id=` | `{"entries":[...],"next_before_id"}` newest first |

Missing or invalid token: **401**. A device token on an owner route, or the owner token on a device route:
**403**. A host id that does not exist, is malformed, or is not shared with the calling device: **404**, with
one identical body for all three. Every model rejects unknown fields (**422**) and has bounded strings and
lists. There is no field anywhere that carries a command, script or arguments.

### Hosts and the allowlist

`HostEnrol`: `host_id` (`[0-9a-zA-Z_-]{1,40}`), `display_name` (1 to 64 printable characters), `adapter`
(`"mock"` only), `metrics` (1 to 6 distinct names from the fixed set), `services` (up to 16 distinct service
names, `[0-9a-zA-Z_.@-]{1,64}`), `device_ids` (up to 16 devices that may see the host; empty means nobody but
the owner, and ids need not exist yet). At most 16 hosts can be enrolled (409 beyond that).

The fixed metric set is `cpu_percent`, `load_1m`, `mem_used_percent`, `disk_used_percent`, `uptime_s` and
`services`. `services` must be allowlisted exactly when service names are enrolled, and then reports only those
names. Enrolment also checks that the adapter reports itself read-only and supports every requested metric.

The allowlist is enforced three times: the adapter is **asked** only for allowlisted names; anything else it
returns (extra metrics, extra services) is discarded; and every value must be a finite number inside its range
(for example `cpu_percent` 0 to 100, `uptime_s` a whole number of seconds), otherwise it counts as missing.

### Health report

```json
{"host_id":"host-1","state":"ok","collected_at":1800000000,
 "metrics":{"cpu_percent":12.5,"uptime_s":86400},
 "services":[{"name":"nginx","state":"active"}],
 "missing":[]}
```

- `state` is `ok`, or `partial` when some allowlisted names could not be read; `missing` lists them and their
  keys are absent. A service that is enrolled but not reported has state `unknown`
  (`active`, `inactive`, `failed`, `unknown`).
- `services` is present only when the host allowlists it. The response is bounded by construction (at most 6
  metrics and 16 services); the largest possible report and the largest host list are both below 4 KiB.
- An adapter that cannot be reached, times out (the API enforces its own 5 s deadline per read), raises any
  other error, or returns nothing usable yields **502**, never 500 and never a stack trace:

```json
{"error":"host_unavailable","detail":"host could not be reached","state":"unreachable","host_id":"host-1"}
```

`state` is `unreachable` or `timeout`. Error text from an adapter is logged by exception class only and is
never sent to the client.

## Pairing

1. The owner calls `POST /v1/admin/pairing`. The answer contains the code (8 characters from
   `ABCDEFGHJKMNPQRSTVWXYZ23456789`, no `0 O 1 I L U`) exactly once. Optional `device_id` pins the code to one
   device id; `simulated` sets the flag stored for the new device.
2. The code is valid for **300 s**, **single use**, and stored only as a salted PBKDF2-SHA256 hash (100 000
   rounds). Creating a new code burns any code still outstanding, so there is one live code at a time and a
   failed attempt is always charged to exactly one code.
3. The device calls `POST /v1/pair` with the code (case-insensitive) and the device id it wants. The code is
   compared in constant time (also when no code exists). Any failure (wrong, expired, reused, burned, id not
   matching a pinned code) returns the same `401 unauthorized` / "invalid or expired pairing code"; the reason
   is only in the audit log.
4. After **5 failed attempts** the live code is burned; the correct code no longer works.
5. Attempts are throttled per client address: more than 10 within a minute (successful or not) block that
   address for 60 s with `429` and `Retry-After`. A right code does not bypass the throttle.
6. On success the code is claimed with one conditional statement (exactly one caller wins) and then
   `create_device` of the existing store issues the bearer token, returned once with `Cache-Control: no-store`.
   A device id that already exists is refused with `409` and the code stays valid: pairing can add a device but
   never rotates or takes over an existing one. If the device store cannot say whether an id exists, pairing
   fails closed with `503`.

The token is the ordinary device token of [`API.md`](API.md); it is not stored in `companion.db`.

## Audit log

Append-only table `audit` in `companion.db`: `id`, `ts`, `actor` (`owner`, `device:<id>`, `anonymous`),
`action`, `target`, `outcome`, `detail`. Recorded actions: `pairing_created`, `pairing_redeemed`,
`pairing_failed` (reason and client address in `detail`), `host_enrolled`, `host_deleted`, `health_read`
(including denied reads of unknown or foreign ids). Callers pass identifiers only; every field is reduced to a
short constrained string, so neither the code, the token nor adapter error text can enter it. No statement
in the package updates or deletes an audit row, and database triggers abort any attempt. Retention is unbounded
in this version (a health poll adds one row per read); rotation is a known gap.

## Storage

`companion.db` in `OYSTER_DATA_DIR`, next to but separate from `oyster.db`: WAL, 10 s busy timeout, one
connection behind a lock, tables created with `IF NOT EXISTS` (idempotent). Tables: `meta`, `hosts`, `pairing`,
`audit`. The main database is not touched. Like `oyster.db`, the file inherits the directory permissions.

## Adapters

`HostAdapter` has two methods, `describe()` and `health(metrics: frozenset[str])`. There is deliberately no
method that takes a command, script or free-form argument, and a test asserts the protocol's whole surface.
`MockHostAdapter` returns deterministic fixtures and can inject faults (`unreachable`, `timeout`, `partial`) or
misbehave by returning more than was asked (`overshare`), to prove the API drops it.

**No real Linux adapter ships.** Whoever writes one (design plan section 5: SSH with host-key verification, or a
local OS adapter) must guarantee, and document here:

- strictly read-only access: a dedicated unprivileged account, no sudo, no write-capable credentials;
- an authenticated, encrypted transport with verified host identity (for SSH, a pinned host key);
- no value that came from a client or a model is ever interpolated into a command line, path or query; metric
  names map to a fixed table of constant read operations;
- a hard timeout on every call and failure reported with `HostUnreachable` / `HostTimeout`, not raw error text;
- the API's 5 s deadline does not stop a hung read: the adapter must stop itself (the pool has 4 threads).

Adding an adapter kind means extending `AdapterKind` in `models.py` and the factory in `adapters.py`; until
then enrolment accepts `"mock"` only.

## Issue requirement to endpoint, module and test

All tests are in `tests/companion/` and run without network, with an injected clock and RNG.

| Issue requirement | Endpoint / module | Tests |
| --- | --- | --- |
| Typed API | `models.py`: every model `extra="forbid"`, bounded strings and lists | `test_companion_safety::test_every_model_forbids_extra_fields_and_has_no_command_like_field`, `test_companion_hosts::test_enrol_rejects_bad_requests`, `::test_enrol_rejects_non_objects_and_oversized_bodies`, `test_companion_pairing::test_redemption_validates_its_body` |
| Authenticated | `oyster.auth` dependencies on every route except `POST /v1/pair` | `test_companion_auth::test_missing_or_invalid_token_is_401`, `::test_owner_routes_refuse_a_device_token`, `::test_device_routes_refuse_the_owner_token`, `::test_authentication_happens_before_the_body_is_read`, `::test_a_revoked_device_token_stops_working`, `::test_device_app_serves_no_owner_routes` |
| Pairing | `POST /v1/admin/pairing`, `POST /v1/pair`; `pairing.py` | `test_companion_pairing::test_redeeming_returns_a_working_device_token_once`, `::test_expired_code`, `::test_reuse_is_refused_and_does_not_disturb_the_first_device`, `::test_wrong_code_gives_the_same_answer_as_every_other_failure`, `::test_lockout_after_five_failed_attempts`, `::test_redemption_attempts_are_throttled_per_client`, `::test_the_plaintext_code_is_never_stored`, `::test_pairing_never_takes_over_an_existing_device`, `::test_the_code_is_short_unambiguous_and_expires_in_300_s`, `::test_codes_come_from_the_injected_rng` |
| `/health` | existing `GET /v1/health`, unchanged | `test_companion_auth::test_existing_health_route_is_untouched`, `test_companion_safety::test_the_existing_health_route_is_not_redefined` |
| `/hosts` and host health | `GET /v1/hosts`, `GET /v1/hosts/{id}/health`; `registry.py` | `test_companion_hosts::test_a_device_sees_only_hosts_shared_with_it`, `::test_health_returns_exactly_the_allowlisted_metrics`, `::test_unknown_and_foreign_ids_are_indistinguishable_404s`, `::test_health_response_stays_under_4_kib_at_maximum_size`, `::test_host_list_stays_under_4_kib_at_maximum_size` |
| Mock adapter tests | `adapters.py` (`MockHostAdapter`, faults) | `test_companion_hosts::test_adapter_faults_map_to_typed_502`, `::test_partial_data_is_a_200_with_a_missing_list`, `::test_an_unexpected_adapter_error_is_a_502_and_never_echoed`, `::test_a_hanging_adapter_hits_the_deadline`, `::test_out_of_range_or_mistyped_values_are_dropped_not_forwarded` |
| Only authorized host metrics | per-host allowlist and device visibility in `registry.py` | `test_companion_hosts::test_metrics_outside_the_allowlist_are_never_requested_or_returned`, `::test_only_enrolled_services_are_returned`, `::test_invalid_service_state_from_the_adapter_becomes_unknown`, `::test_deleting_a_host_closes_its_health_endpoint` |
| No arbitrary remote shell execution | no command field or method anywhere | `test_companion_safety::test_the_adapter_protocol_has_only_typed_reads`, `::test_the_mock_adapter_adds_no_callable_surface`, `::test_no_request_accepts_a_command_field`, `::test_the_package_never_runs_programs_or_evaluates_input`, `test_companion_hosts::test_device_cannot_modify_hosts` |
| Audit | `audit.py`, `GET /v1/admin/audit` | `test_companion_audit::test_events_are_recorded_without_secrets`, `::test_the_audit_log_has_no_way_to_change_or_remove_rows`, `::test_the_database_itself_refuses_updates_and_deletes_of_audit_rows`, `::test_pages_do_not_overlap_and_end`, `::test_audit_query_is_bounded` |
| Own database, idempotent start-up | `store.py`, `mount.py` | `test_companion_safety::test_the_companion_has_its_own_database_file`, `::test_start_up_creates_tables_idempotently_and_keeps_data`, `::test_the_original_lifespan_still_runs`, `::test_install_is_idempotent` |
| Exactly the intended routes | `api.py` | `test_companion_safety::test_openapi_lists_exactly_the_intended_routes` |

Run them with `python -m pytest tests/companion -o addopts="" -q -p no:cacheprovider`.

## Known gaps

- No real host adapter, so no real host has ever been read; adapter timeouts, SSH host-key handling and
  least-privilege accounts are unproven.
- No firmware client and no on-device pairing screen; the Cardputer cannot use these routes yet.
- `GET /v1/hosts/{id}/logs`, services control and any action job are out of scope (read-only metrics only).
- Audit retention is unbounded; there is no rotation or export. A device that polls health adds a row per read.
- Host visibility is per device id and must be edited by re-enrolling the host; there is no per-metric device
  scoping and no owner-side health read.
- The pairing throttle and the per-listener auth throttle are in memory (reset by a restart). A client on the
  LAN can burn the live pairing code by guessing five times; the owner then creates a new one.
- Pairing relies on `get_device` of the SQLite store, which is not part of the `Store` protocol; a store
  without it makes pairing fail closed (503).
