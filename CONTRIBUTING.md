# Contributing

- Read [`docs/INTEGRATION.md`](docs/INTEGRATION.md) first: it shows how the design package (`README.md`, `docs/OysterOS_Project_Plan.md`, `docs/DESIGN_SYSTEM.md`, `docs/DEVELOPMENT_ROADMAP.md`, `docs/HARDWARE_AUDIT_TEMPLATE.md`) and the implementation fit together. The design documents are the specification; do not rewrite them to match the code, change the code or record the difference in `docs/INTEGRATION.md`.
- Contracts live in `schemas/`. Change a schema, bump `schema_version`, update `schemas/examples/`, and run `scripts/check.ps1`.
- Never report a hardware behaviour as verified unless it was observed on the device. Simulated data carries `source_mode: simulated` everywhere. Keyboard bindings stay marked UNVERIFIED until tried on a Cardputer (Home and Help included; Settings > Key tester shows what a key resolves to).
- Incomplete features must be visibly unavailable. Never add a screen or control that pretends to work.
- Firmware UI: keep to the design system ([`docs/DESIGN_SYSTEM.md`](docs/DESIGN_SYSTEM.md); the tokens are in `firmware/include/ui_theme.h`) and update [`docs/UI_SCREEN_MAP.md`](docs/UI_SCREEN_MAP.md) when a screen, menu path or key changes.
- No secrets and no personal data in the repo: `backend/.env`, device tokens, API keys, Wi-Fi passwords, raw surveys, SD dumps and flash backups (`_backups/`). Check `git status` before every commit and stage files by explicit path.
- Never flash a device without a verified backup of its flash (`scripts/firmware-backup.ps1`); `scripts/firmware-flash.ps1` enforces it. Do not weaken that gate.
- Firmware: PlatformIO only (`firmware/platformio.ini` pins everything). Record any dependency change in `docs/BUILD_MATRIX.md`.
- Backend/lab: `ruff check` and `pytest -q` must pass (`scripts/check.ps1`).
- AI-generated art: see `assets/PROVENANCE.md`, which records how each image was made. Add the same record for any new image.
- CI is not configured yet. `docs/CI_SUGGESTION.md` describes what a pipeline could run.
