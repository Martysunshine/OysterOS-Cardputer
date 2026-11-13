[OysterOS](../README.md) / lab

# Lab

Two tools for working without a Cardputer on the desk, packaged as `oyster-lab` (Python 3.12+). The
**device simulator** is a virtual Cardputer that speaks the same HTTP job and result contract as the firmware,
so the backend, the AI agent loop and the reports can be exercised end to end. The **BLE lab fixture** turns a
Windows PC into the synthetic Bluetooth LE peripheral that the Security tile inspects.

## What is where

| Path | What it holds |
|---|---|
| [`oyster_lab/simulator/`](oyster_lab/simulator/) | `oyster-sim`: registers a session, polls jobs, applies its own local policy and confirmation gate, runs a scenario and uploads events labelled `source_mode: "simulated"` |
| [`oyster_lab/fixture/`](oyster_lab/fixture/) | `oyster-fixture`: a live GATT server on the native WinRT API that publishes the lab profile in `vulnerable` or `corrected` mode; `protocol.py` is its pure state machine |
| [`oyster_lab/synthetic/`](oyster_lab/synthetic/) | The deterministic synthetic diagnostic object and its 55-byte manifest, shared by the fixture and the simulator |
| [`scenarios/`](scenarios/) | Ten JSON scenarios for the simulator: `success`, `denied_access`, `timeout`, `malformed_advertisement`, `unexpected_pairing_transition`, `address_change`, `transfer_interrupted`, `vulnerable_target`, `corrected_target` and the blind pick `withheld` |
| [`pyproject.toml`](pyproject.toml) | The package and its two commands; the `fixture` extra adds the WinRT and Bleak packages |

The simulator is an independent implementation of the device side: it imports no backend code and validates
its own events against [`schemas/event.schema.json`](../schemas/event.schema.json). The fixture serves
[`schemas/lab-profile.json`](../schemas/lab-profile.json) and holds synthetic data only.

## Run it

The device simulator, against a local backend:

```powershell
# window 1
.\scripts\backend.ps1
# window 2
.\scripts\sim.ps1 -List                             # available scenarios
.\scripts\sim.ps1 -Scenario success -AutoConfirm
.\scripts\sim.ps1 -Scenario withheld -Seed 7 -SessionFile .\backend\data\withheld-session.txt
```

The end-to-end demonstration (a real AI model, the simulated device, an exported Markdown report; needs
`AI_MODEL` and `AI_API_KEY` in `backend\.env`):

```powershell
.\scripts\demo-m1.ps1
```

The BLE lab fixture:

```powershell
.\scripts\fixture.ps1 check                         # read-only Bluetooth adapter capability check
.\scripts\fixture.ps1 run -Mode vulnerable          # advertise the lab GATT profile until Ctrl+C
.\scripts\fixture.ps1 reset-state                   # clear authorisation, challenge, transfer and virtual-LED state
.\scripts\fixture.ps1 synth                         # write the synthetic object, its manifest and SHA-256
```

## Read next

- [BLE lab profile](../docs/BLE_LAB_PROFILE.md) — characteristics, assertions and the transfer protocol
- [Backend API](../docs/API.md) — the job and result contract the simulator speaks
- [ADR-007](../docs/adr/ADR-007-fixture-auth-state.md) and [ADR-008](../docs/adr/ADR-008-native-winrt-fixture.md) — fixture state and the WinRT choice
