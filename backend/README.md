[OysterOS](../README.md) / backend

# Companion backend

The optional companion that runs on your PC: a FastAPI service, packaged as `oyster-backend` (Python 3.12+).
It runs the AI agent loop, queues typed jobs for the Cardputer, validates and stores the evidence that comes
back, renders reports, and hosts the Control Deck, Smart Home, host health and voice routes. It listens twice:
HTTPS on port 8443 for the device (device routes only) and HTTP on `127.0.0.1:8000` for the CLI, the
simulator and the tests.

## What is where

Everything lives in the [`oyster/`](oyster/) package; configuration comes from `backend/.env`, created from
[`.env.example`](../.env.example).

| Path under `oyster/` | Role |
|---|---|
| `main.py`, `settings.py`, `cli.py` | Application factory, configuration, the `oyster` command line |
| `api/` | Core routes: health and clock hint, sessions and evidence upload, device jobs, AI runs, owner-only enrolment |
| `ai/` | The agent loop: orchestrator, tool catalogue, provider interface with an OpenAI-compatible adapter, redaction, bounded context and answer checks |
| `jobs/`, `policy/` | Job lifecycle and expiry sweep; the operation catalogue and the deterministic evaluators |
| `evidence/`, `reports/` | JSON-schema validation of incoming data; session reports as JSON and Markdown |
| `store.py`, `db/` | The persistence interface and its SQLite implementation |
| `auth.py`, `tls.py`, `provision.py`, `fileperms.py` | Device and owner tokens, the private dev CA, the PC side of USB serial provisioning, owner-only secret files |
| `contracts.py` | Pydantic models that mirror [`schemas/`](../schemas/README.md) |
| `companion/` | Pairing and read-only host health |
| `deck/` | Control Deck: profiles with slots 1–9, allowlisted typed actions, job state machine, audit, executors |
| `home/` | Smart Home: a Home Assistant REST client behind an owner-written allowlist |
| `voice/` | Voice questions: chunked audio upload and speech-to-text |
| `web/` | The optional `web_search` and `web_fetch` tools and their outbound URL rules |
| Approvals link | Records an allow or deny from the device for requests sent by the scripts in [`integrations/claude-code/`](../integrations/claude-code/README.md) |

| `oyster` command | What it does |
|---|---|
| `init-db`, `serve` | Create the data directory and SQLite schema; run both listeners |
| `make-dev-ca` | Create or refresh the private dev CA and a server certificate for the PC's LAN IP |
| `enroll-device`, `enroll-target`, `list-targets` | Enrol a device (its token is shown once); enrol or list target profiles |
| `provision-device` | Provision a Cardputer over USB serial |
| `seed-demo` | Enrol this PC as a read-only host and create the Desk profile for a device |
| `sessions`, `link-baseline`, `export-report` | List sessions, link a baseline session, export a report as Markdown or JSON |
| `validate-schemas` | Validate `schemas/examples` and the lab profile against the JSON schemas |
| `ai-check`, `ask` | Check the configured AI provider; ask a question about a session |

## Run it

```powershell
.\scripts\setup.ps1                                 # once: .venv, packages, backend\.env with random tokens
.\scripts\backend.ps1                               # oyster init-db, then oyster serve; Ctrl+C stops it
Invoke-RestMethod http://127.0.0.1:8000/v1/health   # from a second window
```

With a Cardputer on the same network:

```powershell
.\scripts\backend.ps1 -MakeDevCaIp 192.168.1.50     # dev CA and server certificate for this PC's LAN IP
.\scripts\provision.ps1 -Port COM5 --id cardputer-01 --ssid "YourNetwork"
.\scripts\oyster-up.ps1                             # or: certificate, backend and provisioning in one command
```

The CLI and the tests:

```powershell
. .\scripts\_env.ps1                                # puts oyster and the venv python on PATH for this window
oyster ai-check
python -m pytest tests/deck -o addopts="" -q -p no:cacheprovider
```

## Read next

- [Backend API](../docs/API.md) — listeners, authentication, endpoints and lifecycles
- [Architecture](../docs/ARCHITECTURE.md) — how the backend, firmware and simulator fit together
- [Companion API](../docs/COMPANION_API.md) — pairing and read-only host health
- [Privacy](../docs/PRIVACY.md) — what the AI provider receives
