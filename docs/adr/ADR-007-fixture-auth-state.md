[OysterOS](../../README.md) / [Docs](../README.md) / ADR-007

# ADR-007: Fixture authorization state and capability limits

**Status:** accepted (2026-10-24)

**Decision.** The lab fixture's application-authorization state is global, with a TTL and an explicit
`reset-state`, and the lab uses one client at a time. The fixture mode (vulnerable/corrected) is never
exposed over GATT, so false-positive evaluation stays blind.

**Why / consequences.** The WinRT GATT-server backend does not expose per-peer identity on every event, so
per-connection authorization state is not reliable; a global TTL model is honest about that. Peripheral-role
support depends on your Bluetooth adapter and driver, so `oyster-fixture check` reports
`is_peripheral_role_supported` for the adapter in use. On a host where it is `True`, WinRT exposes
`GattProtectionLevel.ENCRYPTION_REQUIRED` **and** `ENCRYPTION_AND_AUTHENTICATION_REQUIRED`, so both a
link-encryption-required and an authenticated-pairing-required characteristic can be published. Where a
host/library cannot reproduce a pairing state, that assertion is marked unsupported rather than reported
as a pass.
