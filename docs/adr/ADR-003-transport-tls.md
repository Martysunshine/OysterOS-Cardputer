# ADR-003: Transport, TLS and time bootstrap

**Status:** accepted (2026-10-24)

**Decision.** Device-initiated HTTPS REST with bounded polling over one persistent connection. A private
dev CA (`oyster make-dev-ca --ip <LAN-IP>`) issues a server certificate whose CN and SAN are the backend's
LAN IP (ESP32 mbedTLS 2.x matches the host it connected to). The CA PEM and a per-device token are
provisioned into NVS/LittleFS over USB serial and are never printed back.

**Clock.** Validation needs a sane clock: seed time over serial at provisioning, then SNTP (bounded), then
an unauthenticated `GET /v1/time` hint. `clock_quality` (`ntp|serial|backend_hint|unknown`) is recorded on
every event. There is no "skip certificate verification" path in the firmware.

**Host trust note.** Some hosts run TLS-inspecting security software that re-signs outbound HTTPS with its
own root. `scripts/make_ca_bundle.py` builds a bundle of certifi + the Windows trust stores so PlatformIO and
Python tooling validate those chains **with verification on**; this only affects the build host and never
relaxes the device's own trust.

## Amendment (2026-10-24): certificate validity dates are NOT enforced on the device

Verified in the pinned Arduino-ESP32 2.0.17 prebuilt SDK (`tools/sdk/esp32s3/*/include/sdkconfig.h`, all six
S3 variants): `CONFIG_MBEDTLS_HAVE_TIME` is set but `CONFIG_MBEDTLS_HAVE_TIME_DATE` is not, so mbedTLS does
not check a certificate's notBefore/notAfter. The certificate **chain** (pinned private CA) and the **host
name / IP** are still verified.

**Consequences.** An expired or not-yet-valid server certificate issued by the pinned CA would be accepted.
The clock gate above is therefore a firmware *policy* (needed for evidence timestamps and to avoid TLS
before time is known), not something the TLS library enforces. Mitigations in this design: the device
trusts only the owner's private CA (never a public bundle); revocation = re-provision a new CA; keep the
CA key only on the backend PC. Fixing it properly needs a rebuilt mbedTLS (custom ESP-IDF build or a
firmware-side X.509 date check on the peer certificate) and is out of scope for v1; it is listed as a
known limitation in `docs/CAPABILITY_MATRIX.md`.
