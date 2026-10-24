# OysterOS Development Roadmap

A basic computer-science project with verifiable increments. Never begin by overwriting the user's current Cardputer firmware.

## Milestone 0 — Know the device and protect the original software
- Record exact Cardputer ADV revision, factory/current firmware, launcher version and inventory of original apps.
- Determine whether original app architecture supports third-party applications or verified multiboot.
- Record recovery, serial console, firmware images and backup feasibility; no credentials in repo.
- **Gate:** original apps preservation strategy documented and tested.

## Milestone 1 — Interactive 240×135 UI simulator
- Render every major OysterOS screen with design tokens from DESIGN_SYSTEM.md.
- Keyboard emulation: Up/Down/Enter/Back/Home/Help and on-screen key legend.
- Complete navigation stack, visible focus, modal confirmations, loading and errors.
- Test key behavior across every route. **No device flashing.**
- **Deliverable:** web or desktop simulator and automated navigation tests.

## Milestone 2 — Firmware shell
- Choose supported integration from audit.
- Port input layer and renderer; measure memory/time; implement stable Back/Home.
- Integrate actual Original Apps return/launch path or fail gate with explicit limitation.
- **Deliverable:** UI can switch back to existing apps without destroying them.

## Milestone 3 — Security tools
- Passive Wi-Fi survey: scan, result list, detail, save finding.
- BLE explorer: scan, list, detail; inspect services only when authorized.
- Show Wi-Fi disabled, busy, timeout, no results and interruptions.
- **Deliverable:** real scan data on device.

## Milestone 4 — Companion and Linux
- FastAPI mock → real authenticated pairing, capabilities and read-only host health.
- Implement Linux/Windows adapter and per-host access checks.
- **Deliverable:** view current host CPU, memory and service status; no shell injection surface.

## Milestone 5 — Smart Control Deck
- Configurable profiles and numeric slots.
- Backend allowlisted tasks with typed args, explicit confirmation, audit logs and idempotent job state.
- Progress/cancel/history UI and loss-of-connection reconciliation.
- **Deliverable:** reliably execute one approved predefined script on user's own computer.

## Milestone 6 — Home and automations
- Infrared profile configuration on device.
- Optional Home Assistant/MQTT companion adapters.
- Backend scheduler with durable definitions and execution history.
- **Deliverable:** demonstrate one predefined IR action and one scheduled companion task.

## Milestone 7 — Oyster AI and voice
- API proxy (credentials on backend), text query and paged answer.
- Explicit scan-context sharing for analysis.
- Optional mic recording → transcription → answer, only if supported in chosen firmware integration.
- **Deliverable:** AI can explain a saved Wi-Fi finding without being allowed to run commands unreviewed.

## Milestone 8 — Quality and documentation
- Power interruptions, offline operation, low memory, scan cancellation, startup recovery, security cases.
- GitHub Actions lint/tests for simulator and companion.
- Firmware build reproducibility and baseline footprint.
- End-to-end demo video/screenshots with original apps working.

## MVP acceptance
1. Original installed applications are preserved.
2. Reliable Back and Home controls operate on every OysterOS route.
3. Wi-Fi Survey is functional.
4. Linux host health can be viewed securely.
5. Control Deck starts one allowlisted macro with confirmation and a real result.
6. All unsupported features show transparent “Not available” messaging.

## Project workboard seed
| Priority | Issue | Dependency |
|---|---|---|
| P0 | Firmware inventory & backup evaluation | None |
| P0 | 240×135 simulator shell | None |
| P0 | Global navigation and key diagnostic screen | Simulator |
| P1 | Original launcher adapter spike | Firmware inventory |
| P1 | Wi-Fi scanner adapter | Firmware shell |
| P1 | Companion API mock + schema | Simulator |
| P1 | Read-only host metrics | Companion |
| P1 | Macro registry, confirmations and audit | Companion |
| P2 | BLE diagnostics | Firmware |
| P2 | IR and smart home integration | Companion |
| P2 | Scheduler | Jobs |
| P2 | AI gateway | Companion |
| P3 | Mic/voice | AI, audio compatibility |

Keep a log of decisions and test evidence. A working vertical slice is worth more than 20 disconnected mock screens.
