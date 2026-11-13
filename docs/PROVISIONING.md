[OysterOS](../README.md) / [Docs](README.md) / Provisioning

# Serial provisioning protocol (v1)

One-time local setup of a Cardputer over its USB-C serial port (115200 baud, USB CDC). Implemented by
`firmware/src/serial_provisioning.cpp` (device) and `oyster provision-device` (backend CLI, via pyserial).
Nothing secret is ever echoed back; replies name the field only.

## Framing

- ASCII lines terminated by `\n` (a preceding `\r` is ignored). Maximum line length 512 bytes.
- Values that may contain spaces or non-ASCII (Wi-Fi SSID/password) are sent **hex-encoded**.
- Device replies: `OK <field>`, `ERR <code> <short text>`, or `STATUS <json>`. Unknown command → `ERR unknown_command`.
- A session starts with `HELLO` and ends with `DONE` (commit + reboot) or `ABORT` (discard). Changes are staged in RAM and written to NVS/LittleFS only on `DONE`. This includes `SET-TIME`: the clock is set only when `DONE` is accepted. `FACTORY-RESET` is the exception: it takes effect immediately.
- If no command arrives for 60 s the session aborts (staged changes, including a staged time, discarded).

## Commands

| Command | Effect | Reply |
| --- | --- | --- |
| `HELLO oyster-prov/1` | Enter provisioning mode (UI shows a setup screen) | `OK hello <firmware-version> <board>` |
| `SET-ID <device_id>` | `[0-9a-zA-Z_-]{1,40}` | `OK device_id` |
| `SET-BACKEND <host> <port>` | host = LAN IP or hostname that the server certificate names | `OK backend` |
| `SET-WIFI <ssid_hex> <password_hex>` | 2.4 GHz network credentials | `OK wifi` |
| `SET-TOKEN <token>` | per-device token from `oyster enroll-device` | `OK token` |
| `SET-AI-MODEL <model>` | direct-AI model id (`[0-9a-zA-Z/._-]{1,64}`); the device calls the provider itself over Wi-Fi when the backend is not reachable | `OK ai_model <model>` |
| `SET-AI-KEY <key>` | direct-AI provider key (8-200 printable ASCII); a secret, never echoed back | `OK ai_key` |
| `CA-BEGIN` / `CA-LINE <pem line>` / `CA-END` | backend CA certificate (PEM, ≤ 4096 bytes) | `CA-BEGIN` and `CA-LINE` reply nothing on success (only `ERR ...` on failure); `OK ca <bytes>` after `CA-END`. The host sends the lines without waiting and fails on any `ERR` before the `CA-END` reply. |
| `SET-TIME <unix_seconds>` | staged clock seed, applied at `DONE` (`clock_quality: serial`) so the device can send authenticated requests before SNTP; discarded on `ABORT`/timeout. Range 2026-01-01 to 2100-01-01 | `OK time` |
| `TARGET-ADD <compact TargetProfile JSON>` | enrol a target locally (≤ 8 targets); validated against the target schema fields | `OK target <target_id>` |
| `TARGETS-CLEAR` | remove all locally enrolled targets | `OK targets` |
| `STATUS` | report non-secret state (staged values where set, else stored ones). `clock_quality` is the live clock's quality: a staged `SET-TIME` does not show until `DONE`. `ai_key_set` is whether a direct-AI key is stored (the key itself is never reported) | `STATUS {"device_id":..,"backend":..,"wifi_ssid_set":true,"token_set":true,"ai_key_set":true,"ca_bytes":1234,"targets":1,"clock_quality":"unknown","provisioned":false}` |
| `DONE` | validate completeness, persist, reboot | `OK done` or `ERR incomplete <missing fields>` |
| `ABORT` | discard staged changes | `OK abort` |
| `FACTORY-RESET` | immediately erase token, Wi-Fi password, CA and targets (keeps device_id), also the staged ones; the running firmware drops its cached credentials and targets at once | `OK factory_reset` |

## Rules

- The token, Wi-Fi password and CA are never printed, logged or written to the SD card.
- The device's local target list is authoritative for LocalPolicy; the backend cannot add targets remotely.
- Re-running provisioning replaces fields; `oyster enroll-device` with an existing id rotates (revokes) the old token.
