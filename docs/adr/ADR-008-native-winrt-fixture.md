# ADR-008: Live GATT fixture uses the native WinRT API, not Bless

**Status:** accepted (2026-10-24)

**Context.** The original plan names Bless as a candidate and allows "a native host API". Checked with
Python 3.13 and bleak 3.0.2: `bless 0.2.6` imports `bleak_winrt`, which does not exist for Python >= 3.12, so
`import bless` fails; `bless 0.3.0` on PyPI pins `winrt-*==2.0.0b1`, which conflicts with current bleak and
has no Python 3.13 wheels. Bless also never sets `ENCRYPTION_AND_AUTHENTICATION_REQUIRED`.

**Decision.** `lab/oyster_lab/fixture` publishes the lab profile with pywinrt
(`winrt.windows.devices.bluetooth.genericattributeprofile`: `GattServiceProvider`,
`GattLocalCharacteristicParameters`, `GattProtectionLevel`, read/write-requested events with deferrals).
The `fixture` extra pins the `winrt-*` packages (already required by bleak).

**Consequences.** All four protection levels are available, so the authenticated-pairing assertion can be
measured on this route. Linux/macOS hosts are not supported by this fixture in v1 (documented); the
simulator covers orchestration everywhere. Adapter peripheral-role support is checked by
`oyster-fixture check` before use, never assumed.
