[OysterOS](../README.md) / [Docs](README.md) / Hardware audit template

# Cardputer ADV Firmware Preservation Audit

**Do not commit device credentials, raw flash images or personal network data.**

## Hardware and installation
- [ ] Exact board/model markings:
- [ ] Device purchase/vendor reference:
- [ ] Current firmware and version:
- [ ] Original boot splash and launcher screenshots (redact sensitive details):
- [ ] Built-in apps (Compass, PikaScript, Voice, GPS-labelled, others):
- [ ] External accessories/sensors installed:
- [ ] USB serial / bootloader process (verified from vendor):
- [ ] Settings that must be preserved:

## Launcher integration research
- [ ] Does firmware support custom app plugins/scripts?
- [ ] Is M5Launcher or equivalent currently installed?
- [ ] Is multi-firmware loading supported?
- [ ] Does launcher expose documented return/back API?
- [ ] Are app package formats and flash constraints known?
- [ ] Is there a verified original firmware reinstall path?
- [ ] Is backed-up firmware recoverable on this exact board?
- [ ] Is there an approved way to build OysterOS as a non-destructive module?

## Keyboard validation
Record physical key press → observed event → proposed OysterOS action. Verify ESC, Fn, Backspace, Enter, navigation, reset. Do not assume earlier suggested key mappings are authoritative.

## Preservation gate
Before flashing or changing any boot/flash configuration:
1. Verify backup/recovery workflow independently.
2. Demonstrate original apps work before changes.
3. Document a tested way to access them after.
4. Obtain explicit user approval for changes.
5. If preservation cannot be guaranteed, stop and keep developing the simulator.

## Observations
- Date:
- Firmware:
- Evidence:
- Outstanding risks:
- Decision: [ ] Go [ ] No-go
