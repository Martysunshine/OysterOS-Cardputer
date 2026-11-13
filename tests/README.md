[OysterOS](../README.md) / tests

# Tests

The Python test suite, run with pytest and configured in the root [`pyproject.toml`](../pyproject.toml). It
covers the backend, the lab tools, the UI simulator, the generated assets and the shared contracts. Tests run
on the PC against temporary SQLite stores, controllable clocks and `httpx.MockTransport`, so a normal run
needs no network and no device. The firmware's C++ host tests live in [`firmware/test/`](../firmware/README.md).

## What is where

| Package | What it tests |
|---|---|
| [`ai/`](ai/) | The agent loop: orchestrator, provider adapter, rate limits, loop guard, redaction and bounded context, answer checks, web tools, the `ai-check` and `ask` commands |
| [`assets/`](assets/) | The static UI art in `firmware/src/assets_gen.cpp`: flash budget, shapes and the loader animation |
| [`auth_jobs/`](auth_jobs/) | The core API: authentication and ownership, job lifecycle, local confirmation, evidence upload, AI run endpoints, the dev CA, serial provisioning and the `oyster` CLI |
| [`companion/`](companion/) | Pairing, the host registry and read-only health, the audit log and route authentication |
| [`contracts/`](contracts/) | Every file in `schemas/examples` validates under its JSON schema and parses into the backend models; emitted wire shapes match the examples |
| [`deck/`](deck/) | Control Deck: action allowlist, profiles, job state machine, idempotency, confirmation, audit, the Windows executor against a fake OS layer, restart behaviour |
| [`home/`](home/) | Smart Home: the allowlist file, the Home Assistant client against a fake instance, the routes and their security rules |
| [`integration/`](integration/) | The confirmation-timeout race end to end: the real app, the orchestrator and the device simulator in one process |
| [`lab/`](lab/) | The device simulator (scenarios, local policy, `--serve` polling, the blind `withheld` scenario) and the BLE fixture (protocol state machine, CLI, synthetic object) |
| [`reports/`](reports/) | The deterministic evaluators and report rendering: schema-valid JSON, citations, inert Markdown |
| [`scripts/`](scripts/) | The two-slot flash layout: partition CSV, PlatformIO environment and the PC-side scripts in `-DryRun` |
| [`simulator/`](simulator/) | The UI simulator: navigation contract, route table against the docs and firmware sources, renderer pixels, theme and fonts, key emulation |
| [`voice/`](voice/) | Voice questions: audio helpers, the upload lifecycle, transcription, token and session ownership |
| [`web/`](web/) | `web_search` and `web_fetch`: the outbound URL policy, redirects, size caps and key handling |
| Approvals link | The remaining package: the approval state machine, long poll, authorisation matrix, redaction and usage line, plus the scripts in [`integrations/claude-code/`](../integrations/claude-code/README.md) |

Tests marked `live_provider` make a real AI provider call and are opt-in; every other test stays on the PC.

## Run it

```powershell
.\scripts\check.ps1                       # ruff, pytest, schema validation, header check, host tests, firmware build
.\scripts\check.ps1 -SkipFirmware         # the Python checks only
```

Pytest directly:

```powershell
. .\scripts\_env.ps1                                               # puts the venv python on PATH for this window
python -m pytest                                                   # everything under tests\
python -m pytest tests/deck -o addopts="" -q -p no:cacheprovider   # one package
.\scripts\ui-sim.ps1 -Test                                         # tests\simulator through the simulator launcher
```

The live provider checks, with `OYSTER_LIVE_TESTS=1`, `AI_API_KEY` and `AI_MODEL` set in the environment:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ai -m live_provider
```

## Read next

- [Backend API](../docs/API.md) — the contract most of these tests pin down
- [Control Deck](../docs/CONTROL_DECK.md) — acceptance criteria mapped to tests
- [UI simulator](../docs/SIMULATOR.md) — what the simulator tests compare against the firmware
- [Build matrix](../docs/BUILD_MATRIX.md) — the pinned Python and test tool versions
