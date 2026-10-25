# BLE lab profile `oyster-lab-v1`

This page explains `schemas/lab-profile.json` in readable form. **The JSON file is the source of truth**
([ADR-006](adr/ADR-006-lab-profile.md)). If this page and the JSON ever disagree, the JSON wins. The firmware
header `oyster_core/lab_profile_gen.h` is generated from the JSON by `scripts/gen_lab_profile_header.py`,
and `scripts/check.ps1` fails when the header is stale.

The profile describes a **synthetic** lab peripheral. It is served by `oyster-fixture` on the PC
(native WinRT GATT server, [ADR-008](adr/ADR-008-native-winrt-fixture.md)) and modelled by the
simulator. It contains no real data. The fixture's mode (vulnerable or corrected) is **never** exposed
over GATT, so a test cannot see which answer is expected. That keeps false-positive evaluation blind.

| Field | Value |
| --- | --- |
| `profile_id` / `profile_version` | `oyster-lab-v1` / `1` |
| Primary service UUID | `862f20d9-fa73-5f89-b130-86ee93e78261` |
| Advertised name (fixture default) | `OysterLab` (`LAB_FIXTURE_NAME`) |
| Enrolled target example | `schemas/examples/target.lab-01.json` (`target-lab-01`) |

## 1. Characteristics

All UUIDs are 128-bit, fixed once, and derived from one random base.

| Id | UUID | Properties | Access policy | Max bytes | Value |
| --- | --- | --- | --- | --- | --- |
| `public_sensor` | `9f049067-d6ce-5453-9ff9-4dbb2b3342d4` | read, notify | read: public, notify: public | 2 | int16 LE, synthetic temperature in centi-degrees C |
| `restricted_enc` | `1b140da2-75c7-505d-8836-2f2408b9c79d` | read | read: **link_encryption** | 16 | ASCII `LAB-ENC-OK` |
| `restricted_mitm` | `cb2e3a41-f11e-5589-975b-e19fa35a4e82` | read | read: **link_authentication** | 16 | ASCII `LAB-MITM-OK` |
| `app_auth` | `979903f6-da0e-5dc1-9051-74b1c112d851` | read, write | read: public, write: public | 32 | read: `[state u8][challenge 16]`; write: HMAC-SHA256 response (32) |
| `diag_manifest` | `f40f4bd8-e156-5f1b-9b7a-3da5a50170f9` | read | read: **app_authorization** | 64 | 55-byte manifest v1; application denial = single byte `0x01` |
| `chunk_ctrl` | `e24475fc-f9f2-5a32-96b6-28d8434220ba` | write | write: **app_authorization** | 13 | chunk request / cancel / reset |
| `chunk_data` | `38853531-990c-50b7-b0c1-c47516ff0d5a` | read | read: **app_authorization** | 246 | 9-byte header + payload |
| `diag_status` | `a46f2246-89bf-57ce-beae-7d9f819f063a` | read | read: public | 3 | `[last_status u8][last_seq u16 LE]` |
| `virtual_led` | `b4bbe699-5a09-522a-90bd-0ca88cc5a1a3` | read, write | read: public, write: **app_authorization** | 1 | u8 colour: 0 off, 1 red, 2 green, 3 blue, 4 white |
| `device_info` | `605552ac-9e34-5c62-9e9a-74b6a27c44dc` | read | read: public | 32 | ASCII `oyster-lab-v1;1` (profile id; version). Does not reveal the mode. The Cardputer reads it first, as an identity consistency check ([section 8](#8-target-identity-and-remote-reads-on-the-cardputer)) |

### What each policy means

| Policy | Enforced by | Satisfied when |
| --- | --- | --- |
| `public` | nobody | always. Reading it is an observation, never a vulnerability |
| `link_encryption` | Windows (`GattProtectionLevel.ENCRYPTION_REQUIRED`) | the BLE link is encrypted |
| `link_authentication` | Windows (`ENCRYPTION_AND_AUTHENTICATION_REQUIRED`) | the link is encrypted **and** the pairing was authenticated (MITM-protected). Just Works pairing does not count |
| `app_authorization` | the fixture's application logic | a valid HMAC response to a fresh challenge was written within the TTL |

Pairing, link encryption, authenticated pairing, and application authorisation are **separate facts**
and are recorded separately. An application-level denial is not proof that link-layer authentication is
enforced, and the reverse is also true.

### How the fixture denies access

- `diag_manifest` before authorisation (corrected mode): the read **succeeds at ATT level** and returns the
  single byte `0x01`. This is an application-level denial (`access: denied_app`), not an ATT error.
- `chunk_data` before authorisation: a header with status `not_authorized` (1) and no payload.
- `virtual_led` write before authorisation: rejected with ATT error `0x08` (Insufficient Authorization), in
  **both** modes.
- `restricted_enc` / `restricted_mitm`: Windows enforces the protection level. A central typically sees
  ATT `0x0F` (Insufficient Encryption) or `0x05` (Insufficient Authentication), or its stack starts pairing
  automatically. Any such automatic security change must be recorded (`security_before`, `security_after`,
  `security_transition`).

### Fixture modes

| Mode | `diag_manifest` / `chunk_ctrl` / `chunk_data` | `virtual_led` write | Link-level characteristics |
| --- | --- | --- | --- |
| `vulnerable` | returned **without** application authorisation (deliberately wrong) | needs authorisation | enforced by Windows |
| `corrected` | need application authorisation | needs authorisation | enforced by Windows |

## 2. Assertions

Deterministic tests the backend evaluator classifies. The model never sets the classification.

| `test_id` | Characteristic / operation | Precondition | Expected | Severity if violated | Informational |
| --- | --- | --- | --- | --- | --- |
| `public_sensor_is_public` | `public_sensor` / read | `connected_unencrypted` | allowed | informational | **yes** |
| `restricted_value_requires_link_encryption` | `restricted_enc` / read | `connected_unencrypted` | denied | medium | no |
| `restricted_value_requires_authenticated_pairing` | `restricted_mitm` / read | `encrypted` | denied | medium | no |
| `restricted_diagnostics_requires_app_authorization` | `diag_manifest` / transfer | `app_unauthorized` | denied | medium | no |
| `diagnostics_available_after_app_authorization` | `diag_manifest` / transfer | `app_authorized` | allowed | low | no |
| `virtual_led_write_requires_app_authorization` | `virtual_led` / write | `app_unauthorized` | denied | low | no |

Notes from the profile:

- `public_sensor_is_public`: a public value is readable. This is an observation and never a vulnerability.
- `restricted_value_requires_link_encryption`: if the stack escalated security first, a value returned under
  encryption satisfies the policy. The transition is recorded.
- `restricted_value_requires_authenticated_pairing`: inconclusive when the authentication state of the link
  cannot be read.
- `diagnostics_available_after_app_authorization`: after a legitimate exchange, the object transfers and its
  SHA-256 matches the manifest.
- `virtual_led_write_requires_app_authorization`: a write before authorisation is rejected in every fixture mode.

## 3. Harmless write values

| Characteristic | Type | Allowed values | Reversible |
| --- | --- | --- | --- |
| `virtual_led` | `enum_u8` | `off`=0, `red`=1, `green`=2, `blue`=3, `white`=4 | yes |

A write needs local confirmation on the Cardputer for the exact target, operation and value
([ADR-005](adr/ADR-005-local-confirmation.md)). The device records the starting value, the requested
change, the observed result and the restoration. A write is never retried automatically after a
transport timeout. The state is reconciled first.

## 4. Application-authorisation protocol (`app_auth`)

The enrolled test credential is shared out of band. On the PC it is `LAB_FIXTURE_AUTH_TOKEN` (at least
32 bytes; the HMAC key is its UTF-8 bytes), and the enrolled client holds the same value. The simulator
models that client today. Handling it on the Cardputer is part of M4 and is not built yet. **The token
itself never travels over the air.**

```
Central                                           Fixture
  |  READ app_auth (offset 0)                        |
  | -----------------------------------------------> |  new random 16-byte challenge (single use, 60 s)
  |  [state u8: 0=unauthorized, 1=authorized]        |
  |  [challenge 16 bytes]                            |
  | <----------------------------------------------- |
  |  WRITE app_auth = HMAC-SHA256(key, challenge)    |
  |  (32 bytes)                                      |
  | -----------------------------------------------> |  well-formed write -> ATT success either way
  |  READ app_auth                                   |
  | -----------------------------------------------> |
  |  state = 1 if the response was valid             |  authorised for 120 s (authorization_ttl_s)
  | <----------------------------------------------- |
```

| Parameter | Value |
| --- | --- |
| Algorithm | HMAC-SHA256 |
| Challenge / response | 16 / 32 bytes |
| Authorisation TTL | 120 s |
| Scope | **fixture-global**, one client at a time; cleared by `reset-state` ([ADR-007](adr/ADR-007-fixture-auth-state.md)) |

WinRT does not expose a reliable per-peer identity on every request, so per-connection authorisation
state is **unsupported** on this route and is listed that way in the capability matrix. A failed attempt
leaves the current state unchanged. `.\scripts\fixture.ps1 reset-state` clears authorisation, challenge,
transfer and virtual-LED state of a running fixture. A freshly started fixture is always unauthorised.

## 5. Diagnostic transfer protocol

A custom profile for **one fixed synthetic object**. It is not a filesystem browser and has no
"download everything" function.

| Parameter | Value |
| --- | --- |
| Object id | `diag-synth-0001` |
| Max object size | 65 536 bytes |
| Object tag | first 4 bytes of the manifest SHA-256; binds every chunk request to one object version |
| Chunk header | 9 bytes |
| Chunk length rule | `chunk_len <= min(manifest.chunk_max, ATT_MTU - 1 - 9)` |

The ATT MTU is negotiated and queried, never assumed. With the default MTU of 23 a chunk carries at most 13
payload bytes. With MTU 247 it carries 237, the most `chunk_data` (246 bytes) allows.

### Manifest (`diag_manifest`, 55 bytes)

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | version = 1 |
| 1 | 16 | object id, ASCII, zero-padded |
| 17 | 4 | declared length, u32 LE |
| 21 | 32 | SHA-256 of the object |
| 53 | 2 | `chunk_max`, u16 LE |

A one-byte value `0x01` instead of a manifest is the application-level denial.

### Request (`chunk_ctrl` write, 13 bytes; fits a default 20-byte ATT write)

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | op: `1` request, `2` cancel, `3` reset |
| 1 | 4 | object tag |
| 5 | 4 | offset, u32 LE |
| 9 | 2 | length, u16 LE |
| 11 | 2 | sequence, u16 LE |

### Response (`chunk_data` read)

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | status |
| 1 | 2 | sequence, u16 LE |
| 3 | 4 | offset, u32 LE |
| 7 | 2 | len, u16 LE |
| 9 | len | payload |

### Status codes (`chunk_data` status and `diag_status.last_status`)

| Code | Name |
| --- | --- |
| 0 | `ok` |
| 1 | `not_authorized` |
| 2 | `bad_request` |
| 3 | `stale_object` |
| 4 | `out_of_range` |
| 5 | `cancelled` |
| 6 | `no_request` |

### Receiver rules (device and simulator)

1. Read and parse the manifest. Reject a bad version, an unexpected object id, a size above the maximum,
   or a zero `chunk_max`.
2. Request chunks in order. Each answer must match the expected sequence, offset and length. Duplicates
   and out-of-order or overflowing chunks are rejected and recorded.
3. Stream the bytes into `artifacts/<object-id>.partial` and compute SHA-256 incrementally. Never buffer
   the whole object in RAM.
4. Finalise to `<object-id>.bin` **only** when the received length equals the declared length **and** the
   hash matches. Otherwise keep the clearly labelled `.partial` and report the mismatch.
5. **Resume:** re-read the manifest. Resume only if the declared length and SHA-256 are unchanged.
   Otherwise discard the `.partial` and restart.
6. **Cancel:** write op `2`. The receiver keeps the partial state labelled as cancelled.
7. Local file names are generated (`<object-id>.bin`). Traversal, absolute paths and unexpected object
   ids are rejected. Nothing the peer sends is used as a path.

To check a transferred file on the PC: `.\scripts\fixture.ps1 synth` writes the object, its manifest and
`<object-id>.sha256` to `lab\data\synth`.

## 6. Access-check event details (`ble_check_access_policy`)

Each access check produces one event. Its `details` object contains these fields (same contract for the
simulator now and the firmware at M4):

| Field | Type | Meaning |
| --- | --- | --- |
| `profile_id` | string | `oyster-lab-v1` |
| `test_id` | string | one of the assertion ids above |
| `characteristic_id` | string | for example `diag_manifest` |
| `precondition` | string | the precondition required by the assertion |
| `precondition_established` | bool | whether that state was actually reached before the access |
| `access` | enum | `returned`, `denied_app`, `denied_att`, `error`, `timeout`, `not_attempted` |
| `app_status` | string or null | application-level status, for example `not_authorized` |
| `raw_att_status` | int or null | ATT error code when the stack exposes it |
| `security_state` | object | `{encrypted, authenticated_pairing, application_authorized}`, each a nullable bool (null = the stack cannot report it) |
| `security_before` / `security_after` | object | security state before and after the access |
| `security_transition` | bool | whether security changed during the access (for example automatic pairing) |
| `value_length` | int or null | bytes returned |
| `value_sha256` | string or null | hash of the returned value. Only public-policy values may also carry `value_hex` |
| `hash_verified` | bool or null | transfer assertions only: the object matched the manifest hash |

`ble_read_profile_value` events supply the evidence for the public read.

## 7. How results are classified

The deterministic evaluator (`oyster/policy/evaluators.py`, `evaluate_assertion`) applies these rules:

| Situation | Classification | Severity |
| --- | --- | --- |
| Informational assertion (public read), any outcome | `observation` | informational |
| Precondition not established, or access `error` / `timeout` / `not_attempted` | `inconclusive` | not_assessed |
| Expected **denied**, access `denied_app` or `denied_att` | `passed` | informational |
| Expected denied, `returned`, policy field (see below) = `false` | violation -> `confirmed` | profile `severity_if_violated` |
| Expected denied, `returned`, policy field = `true` | `passed` (transition recorded as a limitation if `security_transition`) | informational |
| Expected denied, `returned`, policy field = `null` | `candidate` | profile `severity_if_violated` |
| Expected **allowed**, `returned` and `hash_verified` = true | `passed` | informational |
| Expected allowed, denied or hash mismatch | `candidate` | profile `severity_if_violated` |
| Operation not available on this route | `unsupported` | not_assessed |

The policy field checked on a `returned` value is `encrypted` for `link_encryption`,
`authenticated_pairing` for `link_authentication`, and `application_authorized` for `app_authorization`.

Confidence of a confirmed finding: `medium` for one observation, `high` with two or more consistent
observations. A finding is `source_mode: simulated` if any input event was simulated. A readable public
attribute is never a finding, and a timeout or unsupported state is never a pass or a confirmed finding.

## 8. Target identity and remote reads on the Cardputer

These rules are in the firmware (`oyster_core/lab_identity.h`, `src/ble_explorer.cpp`,
`src/operations.cpp`). They are written and host-tested where possible, but **unverified on hardware**.

### Choosing and verifying the target (fail closed)

1. **Candidates.** An enrolled address is the only candidate; discovery rows are then ignored. Without an
   enrolled address, every discovery row that advertises the enrolled service UUID or carries the exact
   advertised name is a candidate. Two or more candidates are refused as ambiguous ("enrol an address"),
   so a look-alike is never silently preferred.
2. **First action after connecting.** One raw read of the public `device_info` value (ATT Read By Type
   over the whole handle range), with bonding off and no service discovery.
3. **Decision.** Anything but an exact `oyster-lab-v1;1` (one trailing NUL is tolerated) disconnects
   before any other read: a different value, no `device_info`, more than one, an error or a timeout.
4. **Bonding** is enabled for that link only after the value matched.

`device_info` is public, so a match is a consistency check against the enrolled profile, **not
authentication**: any device can serve the same string. The evidence records how the target was matched
(`matched_by`, `match_is_hint`, `candidates`) and what the check saw (`identity_ok`, `device_info_hex`).

**Recommendation: enrol the target's address.** Take it (and its type) from the detail view of the
`OysterLab` row in a Cardputer discovery result. Windows Bluetooth settings also show the PC adapter's
address, but whether the fixture advertises from that address has not been checked.

### What a remote job may read

- A job from the backend/AI may read only characteristics whose read policy is `public`, **excluding
  `app_auth`**: every read of it issues a new challenge and so replaces the fixture's single outstanding
  one. Today that leaves `public_sensor`, `diag_status`, `virtual_led` and `device_info`. Anything else is
  refused before the radio is used and recorded as `denied`.
- Protected access happens only through the access-policy assertions (section 2), gated by local
  confirmation when the assertion writes or transfers. The device side of those is M4.
- Reads chosen on the device by the person holding it are not restricted.
