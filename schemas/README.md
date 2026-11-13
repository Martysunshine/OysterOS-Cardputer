[OysterOS](../README.md) / schemas

# Schemas

The wire contracts of OysterOS: nine JSON Schema files (draft 2020-12) for everything that crosses between
the Cardputer, the backend and the AI agent loop, the lab profile that describes the synthetic BLE peripheral,
and a set of worked example documents. Firmware, backend, lab tools and tests all read from this folder, so
a shape is defined once.

## What is where

| File | What it defines |
|---|---|
| [`job.schema.json`](job.schema.json) | Device job: a bounded unit of work for the Cardputer or the simulator |
| [`job-result.schema.json`](job-result.schema.json) | The result a device submits for a job; submission is idempotent |
| [`event.schema.json`](event.schema.json) | Observation event envelope: one append-only evidence event |
| [`finding.schema.json`](finding.schema.json) | Finding record, classified by the deterministic evaluators |
| [`report.schema.json`](report.schema.json) | Session report in its JSON form |
| [`run.schema.json`](run.schema.json) | AI run status as returned by `GET /v1/runs/{id}` |
| [`ai-action.schema.json`](ai-action.schema.json) | Normalised AI action: the tool call extracted from one provider response |
| [`target-profile.schema.json`](target-profile.schema.json) | Enrolled target: a peripheral the owner enrolled, bound to a profile and a scope version |
| [`lab-profile.schema.json`](lab-profile.schema.json) | Assessment profile: GATT layout, access policy and deterministic assertions |
| [`lab-profile.json`](lab-profile.json) | The lab profile `oyster-lab-v1` itself: ten characteristics, six assertions, the diagnostic transfer and application-authorisation parameters |
| [`examples/`](examples/) | Twelve example documents, such as `job.ble-read.json`, `event.wifi-survey.json` and `target.lab-01.json` |

## Who uses them

| Consumer | How |
|---|---|
| Backend | `backend/oyster/evidence/validate.py` checks incoming events, job results and targets against the schemas; `contracts.py` mirrors them as Pydantic models; the operation catalogue and AI tool catalogue read `lab-profile.json` |
| Firmware | `scripts/gen_lab_profile_header.py` generates `lab_profile_gen.h` from `lab-profile.json`; the wire names in `oyster_core/types.h` match the schemas |
| Lab | The device simulator validates its own events against `event.schema.json`; the BLE fixture publishes `lab-profile.json` |
| Tests | `tests/contracts/` proves that every example validates and parses into the backend models |

## Run it

```powershell
. .\scripts\_env.ps1                                                # puts oyster and the venv python on PATH
oyster validate-schemas                                             # every example and the lab profile
python scripts/gen_lab_profile_header.py                            # after editing lab-profile.json
oyster enroll-target --file .\schemas\examples\target.lab-01.json   # enrol the example target on a running backend
```

`.\scripts\check.ps1` runs the validation and confirms that the generated header matches the profile.

## Read next

- [Backend API](../docs/API.md) — the endpoints that carry these shapes
- [BLE lab profile](../docs/BLE_LAB_PROFILE.md) — `lab-profile.json` in readable form
- [ADR-006](../docs/adr/ADR-006-lab-profile.md) — why the profile is the single source of truth
- [Architecture](../docs/ARCHITECTURE.md) — contracts, evidence and trust boundaries
