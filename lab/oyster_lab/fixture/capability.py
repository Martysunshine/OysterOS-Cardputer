"""Read-only capability check for the live fixture host (ADR-007, ADR-008).

Queries the default Bluetooth adapter through WinRT. It never advertises, connects, pairs or changes
radio state. The matrix separates what the adapter reports, what the WinRT API offers, and what has
not been verified end-to-end with the Cardputer.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNKNOWN = "unknown"
UNVERIFIED = "unverified"


@dataclass(frozen=True)
class AdapterInfo:
    present: bool
    low_energy: bool | None = None
    peripheral_role: bool | None = None
    central_role: bool | None = None
    le_secure_connections: bool | None = None
    extended_advertising: bool | None = None
    max_advertisement_data_length: int | None = None
    radio_state: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ApiInfo:
    gatt_server: bool
    protection_levels: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class CapabilityRow:
    capability: str
    status: str
    detail: str


def winrt_api_info() -> ApiInfo:
    """Import-check the pywinrt GATT server API (no Bluetooth calls)."""
    if sys.platform != "win32":
        return ApiInfo(False, error="WinRT is only available on Windows")
    try:
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattProtectionLevel,
            GattServiceProvider,
        )
        from winrt.windows.storage.streams import Buffer  # noqa: F401
    except ImportError as exc:
        return ApiInfo(False, error=f"pywinrt import failed: {exc.name or exc}")
    ok = hasattr(GattServiceProvider, "create_async")
    levels = tuple(n for n in ("PLAIN", "ENCRYPTION_REQUIRED", "ENCRYPTION_AND_AUTHENTICATION_REQUIRED")
                   if hasattr(GattProtectionLevel, n))
    return ApiInfo(ok, levels, None if ok else "GattServiceProvider.create_async missing")


async def query_adapter() -> AdapterInfo:
    """Read the default adapter's properties. Read-only."""
    if sys.platform != "win32":
        return AdapterInfo(False, error="not Windows")
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter
    except ImportError as exc:
        return AdapterInfo(False, error=f"pywinrt import failed: {exc.name or exc}")
    try:
        adapter = await BluetoothAdapter.get_default_async()
    except OSError as exc:
        return AdapterInfo(False, error=f"adapter query failed: {exc}")
    if adapter is None:
        return AdapterInfo(False, error="no default Bluetooth adapter")
    radio_state = None
    try:
        from winrt.windows.devices.radios import RadioState

        radio = await adapter.get_radio_async()
        if radio is not None:
            radio_state = RadioState(radio.state).name.lower()
    except (ImportError, OSError, ValueError):
        radio_state = None
    return AdapterInfo(
        present=True,
        low_energy=bool(adapter.is_low_energy_supported),
        peripheral_role=bool(adapter.is_peripheral_role_supported),
        central_role=bool(adapter.is_central_role_supported),
        le_secure_connections=bool(adapter.are_low_energy_secure_connections_supported),
        extended_advertising=bool(adapter.is_extended_advertising_supported),
        max_advertisement_data_length=int(adapter.max_advertisement_data_length),
        radio_state=radio_state,
    )


def _flag(v: bool | None) -> str:
    return UNKNOWN if v is None else (SUPPORTED if v else UNSUPPORTED)


def not_ready_reasons(adapter: AdapterInfo, api: ApiInfo) -> list[str]:
    """Why the fixture cannot run on this host (empty when it can)."""
    reasons = []
    if not api.gatt_server:
        reasons.append(f"WinRT GATT server API unavailable ({api.error or 'unknown'})")
    if not adapter.present:
        reasons.append(f"no usable Bluetooth adapter ({adapter.error or 'not found'})")
        return reasons
    if not adapter.low_energy:
        reasons.append("adapter does not support Bluetooth LE")
    if not adapter.peripheral_role:
        reasons.append("adapter does not support the BLE peripheral role")
    if adapter.radio_state in ("off", "disabled"):
        reasons.append(f"Bluetooth radio is {adapter.radio_state}: turn Bluetooth on in Windows "
                       "Settings > Bluetooth & devices (this check never changes radio state)")
    return reasons


def is_ready(adapter: AdapterInfo, api: ApiInfo) -> bool:
    """The fixture can run: LE + peripheral role + GATT server API, and the radio is not reported off."""
    return not not_ready_reasons(adapter, api)


def build_matrix(adapter: AdapterInfo, api: ApiInfo) -> list[CapabilityRow]:
    rows = [
        CapabilityRow("Bluetooth adapter", SUPPORTED if adapter.present else UNSUPPORTED,
                      "default adapter found" if adapter.present else (adapter.error or "not found")),
        CapabilityRow("Bluetooth LE", _flag(adapter.low_energy), "BluetoothAdapter.is_low_energy_supported"),
        CapabilityRow("BLE peripheral role", _flag(adapter.peripheral_role),
                      "is_peripheral_role_supported (needed for GATT server + advertising)"),
        CapabilityRow("Radio state", adapter.radio_state or UNKNOWN,
                      "Bluetooth radio must be on to advertise (Windows Settings)"),
        CapabilityRow("LE Secure Connections", _flag(adapter.le_secure_connections),
                      "are_low_energy_secure_connections_supported"),
        CapabilityRow("Extended advertising", _flag(adapter.extended_advertising),
                      "informational; the fixture uses legacy connectable advertising"),
        CapabilityRow("WinRT GATT server API", SUPPORTED if api.gatt_server else UNSUPPORTED,
                      "pywinrt GattServiceProvider" if api.gatt_server else (api.error or "unavailable")),
    ]
    enc = "ENCRYPTION_REQUIRED" in api.protection_levels
    mitm = "ENCRYPTION_AND_AUTHENTICATION_REQUIRED" in api.protection_levels
    rows += [
        CapabilityRow("Link encryption policy (restricted_enc)", SUPPORTED if enc else UNSUPPORTED,
                      "WinRT GattProtectionLevel.ENCRYPTION_REQUIRED, enforced by Windows"
                      if enc else "protection level not available"),
        CapabilityRow("Authenticated pairing policy (restricted_mitm)", UNVERIFIED if mitm else UNSUPPORTED,
                      "supported by protection level ENCRYPTION_AND_AUTHENTICATION_REQUIRED; "
                      "end-to-end with the Cardputer unverified"
                      if mitm else "protection level not available"),
        CapabilityRow("Application authorization (app_auth)", SUPPORTED,
                      "HMAC-SHA256 challenge/response in the fixture; global state with TTL"),
        CapabilityRow("Per-connection authorization state", UNSUPPORTED,
                      "WinRT gives no reliable per-peer identity on every request (ADR-007)"),
        CapabilityRow("Notifications (public_sensor)", SUPPORTED if api.gatt_server else UNSUPPORTED,
                      "GattLocalCharacteristic.notify_value_async"),
    ]
    return rows


def format_matrix(rows: list[CapabilityRow]) -> str:
    w1 = max(len(r.capability) for r in rows)
    w2 = max(len(r.status) for r in rows)
    lines = [f"{'capability':<{w1}}  {'status':<{w2}}  detail", f"{'-' * w1}  {'-' * w2}  {'-' * 6}"]
    lines += [f"{r.capability:<{w1}}  {r.status:<{w2}}  {r.detail}" for r in rows]
    return "\n".join(lines)


def matrix_json(adapter: AdapterInfo, api: ApiInfo, rows: list[CapabilityRow]) -> str:
    return json.dumps(
        {"adapter": asdict(adapter), "api": asdict(api), "ready": is_ready(adapter, api),
         "matrix": [asdict(r) for r in rows]},
        indent=2,
    )


async def run_check() -> tuple[AdapterInfo, ApiInfo, list[CapabilityRow]]:
    api = winrt_api_info()
    adapter = await query_adapter()
    return adapter, api, build_matrix(adapter, api)
