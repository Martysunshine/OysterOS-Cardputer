# CI suggestion (not configured)

This repository does not include CI configuration yet. A pipeline could run the checks below.
Nothing on this page is installed.

Until a pipeline exists, the same checks run locally with:

```powershell
.\scripts\check.ps1          # PASS / FAIL / SKIPPED per step, exit 1 on any FAIL
```

## What a pipeline could run

| Job | Runner | Steps | Needs secrets? |
| --- | --- | --- | --- |
| Backend + lab | Windows or Linux | Python 3.13; `pip install -c constraints.txt -e backend[dev] -e lab` (add `[fixture]` on Windows only); `ruff check .`; `pytest` (tests marked `live_provider` / `live_ble` skip themselves); `oyster validate-schemas`; `python scripts/gen_lab_profile_header.py --check` | no |
| Firmware build | Linux or Windows | cache `~/.platformio`; `pio run -e cardputer-adv` in `firmware/`; publish `firmware.bin` size and SHA-256 (as `scripts/firmware-build.ps1` prints them) | no |
| Firmware host tests | Linux (system GCC) or Windows (WinLibs) | `pio test -e native` in `firmware/` | no |
| Simulation integration | same as backend | start the backend, run the `oyster-sim` scenarios against it with the **fake provider** (labelled `is_fake`), assert report and evidence contracts | no |

The checks these jobs would cover are: schema validation, wrong owner/device and stale scope
rejection, the confirmation gate that cannot be bypassed, idempotent uploads, evidence-citation checks,
prompt-injection data, budgets, transfer edge cases, JSONL recovery, and the firmware build.

## What CI cannot do

- **No hardware results.** CI never touches a Cardputer. A green pipeline is not a hardware pass;
  [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md) stays the only place for those.
- **No live BLE.** Hosted runners have no Bluetooth adapter. The live fixture stays a manual check.
- **No real model by default.** A live-provider test would need the Groq key as a repository secret and
  would cost provider usage. It should stay opt-in and manual.

## Practical notes

- Prefer triggers that run only when wanted (on pull requests, or manual dispatch).
- Cache `~/.platformio` and pip downloads. The ESP32-S3 toolchain is several hundred MB.
- WinRT-specific checks (the live BLE fixture) need a Windows machine with a Bluetooth adapter and stay manual.
