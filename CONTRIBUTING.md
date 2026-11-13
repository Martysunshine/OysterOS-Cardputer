# Contributing

Thank you for looking at OysterOS. Bug reports, ideas and pull requests are welcome.

## Before you start

- **Scope.** OysterOS is for learning and for testing equipment you own: passive recon, inspection of
  enrolled devices and automation of your own systems. Changes that add deauthentication, beacon spam, Karma
  or credential-harvesting portals are declined; see [`SECURITY.md`](SECURITY.md).
- **Get it running.** Follow "Run it yourself" in the [README](README.md#run-it-yourself). Everything except
  flashing works without a device.
- **Find your way around.** [`docs/README.md`](docs/README.md) is the documentation index and every main
  folder has its own README.

## Rules of the house

- **One gate.** `.\scripts\check.ps1` must pass before a pull request: lint (`ruff`), the Python tests
  (`pytest`), schema validation, the generated lab-profile header, the firmware host tests and a firmware
  build.
- **The design documents are the specification.** [`docs/OysterOS_Project_Plan.md`](docs/OysterOS_Project_Plan.md)
  and [`docs/DESIGN_SYSTEM.md`](docs/DESIGN_SYSTEM.md) describe what the product should be. Change the code to
  match them, or propose the design change in the same pull request and say why.
- **Contracts live in `schemas/`.** When you change a schema, bump its `schema_version`, update
  `schemas/examples/` and run the gate.
- **Firmware UI.** Keep to the design system (the tokens are in `firmware/include/ui_theme.h`) and update
  [`docs/UI_SCREEN_MAP.md`](docs/UI_SCREEN_MAP.md) when a screen, a menu path or a key changes.
- **Firmware toolchain.** PlatformIO only; `firmware/platformio.ini` pins every version. Record a dependency
  change in [`docs/BUILD_MATRIX.md`](docs/BUILD_MATRIX.md).
- **Be exact about hardware.** Say that something works on the device only when you have seen it on a
  device. Simulated data carries `source_mode: simulated` everywhere.
- **Nothing pretends.** A screen or control must do what it shows. If a feature cannot work in the current
  state — the companion is off, there is no SD card — the interface says so.
- **Protect the original firmware.** Never flash a device without a verified backup of its flash
  (`scripts/firmware-backup.ps1`); `scripts/firmware-flash.ps1` enforces it. Do not weaken that gate.
- **No secrets, no personal data.** Keep `backend/.env`, device tokens, API keys, Wi-Fi passwords, raw
  surveys, SD card dumps and flash backups (`_backups/`) out of commits. [`.gitignore`](.gitignore) covers
  them; still check `git status` and stage files by explicit path.
- **Artwork.** [`assets/PROVENANCE.md`](assets/PROVENANCE.md) records how each image was made. Add the same
  record for any new image, keep PNGs free of metadata and never redraw the mascot.

## Commits and pull requests

- Use the prefixes already in the history: `feat(scope):`, `fix(scope):`, `docs:`, `chore:`.
- Keep a pull request to one topic and describe what you tested and where (PC, simulator, device).
- By contributing you agree that your contribution is released under the project's
  [MIT licence](LICENSE).
