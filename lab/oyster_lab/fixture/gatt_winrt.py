"""WinRT (pywinrt) GATT server binding for the lab fixture (ADR-008). Windows only.

Publishes schemas/lab-profile.json with GattServiceProvider and bridges every read/write request to
protocol.FixtureState. Link-level policies map to GattProtectionLevel and are enforced by Windows;
application authorization is enforced by FixtureState only. WinRT raises request events on its own
threads: each handler takes a deferral and hands the request to the asyncio loop, so FixtureState is
only touched from one thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Coroutine
from typing import Any

import winrt.windows.foundation  # noqa: F401  (Deferral / IAsyncOperation projection)
import winrt.windows.foundation.collections  # noqa: F401  (IVectorView projection)
from winrt.windows.devices.bluetooth import BluetoothError
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties,
    GattLocalCharacteristic,
    GattLocalCharacteristicParameters,
    GattProtectionLevel,
    GattServiceProvider,
    GattServiceProviderAdvertisementStatus,
    GattServiceProviderAdvertisingParameters,
    GattWriteOption,
)
from winrt.windows.storage.streams import Buffer

from oyster_lab.fixture.profile import CharSpec, LabProfile
from oyster_lab.fixture.protocol import FixtureState

log = logging.getLogger("oyster_lab.fixture")

ATT_UNLIKELY_ERROR = 0x0E
ADVERTISING_START_TIMEOUT_S = 10.0

_PROPERTY_FLAGS = {
    "read": GattCharacteristicProperties.READ,
    "write": GattCharacteristicProperties.WRITE,
    "write_without_response": GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE,
    "notify": GattCharacteristicProperties.NOTIFY,
    "indicate": GattCharacteristicProperties.INDICATE,
}
# public and app_authorization are PLAIN at the link level; app authorization is FixtureState's job.
_PROTECTION = {
    "link_encryption": GattProtectionLevel.ENCRYPTION_REQUIRED,
    "link_authentication": GattProtectionLevel.ENCRYPTION_AND_AUTHENTICATION_REQUIRED,
}


class FixtureStartError(RuntimeError):
    pass


def properties_flags(properties: tuple[str, ...]) -> GattCharacteristicProperties:
    flags = GattCharacteristicProperties(0)
    for p in properties:
        if p not in _PROPERTY_FLAGS:
            raise ValueError(f"unsupported characteristic property {p!r}")
        flags |= _PROPERTY_FLAGS[p]
    return flags


def protection_level(policy: str | None) -> GattProtectionLevel:
    return _PROTECTION.get(policy or "", GattProtectionLevel.PLAIN)


def characteristic_parameters(spec: CharSpec) -> GattLocalCharacteristicParameters:
    params = GattLocalCharacteristicParameters()
    params.characteristic_properties = properties_flags(spec.properties)
    params.read_protection_level = protection_level(spec.policy_for("read"))
    params.write_protection_level = protection_level(spec.policy_for("write"))
    params.user_description = spec.name
    return params


def to_buffer(data: bytes) -> Buffer:
    buf = Buffer(len(data))
    buf.length = buf.capacity
    with memoryview(buf) as mv:
        mv[:] = data
    return buf


def _session_mtu(args: Any) -> int | None:
    try:
        return int(args.session.max_pdu_size)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


class WinRTFixtureServer:
    """Publishes the lab service, advertises it and serves requests from FixtureState."""

    def __init__(self, profile: LabProfile, state: FixtureState) -> None:
        self._profile = profile
        self._state = state
        self._loop: asyncio.AbstractEventLoop | None = None
        self._provider: GattServiceProvider | None = None
        self._chars: dict[str, GattLocalCharacteristic] = {}
        self._tokens: list[tuple[Any, str, Any]] = []  # (object, remove_method_name, token)
        self._tasks: set[asyncio.Task[None]] = set()
        self._handlers: list[Any] = []  # keep Python callables alive while registered

    # ----- lifecycle -----
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        result = await GattServiceProvider.create_async(uuid.UUID(self._profile.service_uuid))
        if result.error != BluetoothError.SUCCESS or result.service_provider is None:
            err = BluetoothError(result.error).name
            raise FixtureStartError(f"GattServiceProvider.create_async failed: {err}")
        self._provider = result.service_provider
        self._register(self._provider, "advertisement_status_changed", self._on_adv_status)
        service = self._provider.service
        for name, spec in self._profile.characteristics.items():
            params = characteristic_parameters(spec)
            res = await service.create_characteristic_async(uuid.UUID(spec.uuid), params)
            if res.error != BluetoothError.SUCCESS or res.characteristic is None:
                err = BluetoothError(res.error).name
                raise FixtureStartError(f"create_characteristic_async({name}) failed: {err}")
            ch = res.characteristic
            self._chars[name] = ch
            if "read" in spec.properties:
                self._register(ch, "read_requested", self._make_read_handler(name))
            if "write" in spec.properties or "write_without_response" in spec.properties:
                self._register(ch, "write_requested", self._make_write_handler(name))
            if "notify" in spec.properties or "indicate" in spec.properties:
                self._register(ch, "subscribed_clients_changed", self._make_subscribers_handler(name))
        adv = GattServiceProviderAdvertisingParameters()
        adv.is_connectable = True
        adv.is_discoverable = True
        self._provider.start_advertising_with_parameters(adv)
        await self._wait_advertising()

    async def _wait_advertising(self) -> None:
        assert self._provider is not None
        deadline = asyncio.get_running_loop().time() + ADVERTISING_START_TIMEOUT_S
        started = (
            GattServiceProviderAdvertisementStatus.STARTED,
            GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA,
        )
        while True:
            status = self._provider.advertisement_status
            if status in started:
                if status == GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA:
                    log.warning("advertising started without all advertisement data")
                return
            if status == GattServiceProviderAdvertisementStatus.ABORTED:
                raise FixtureStartError("advertising aborted by Windows")
            if asyncio.get_running_loop().time() >= deadline:
                raise FixtureStartError(
                    f"advertising did not start within {ADVERTISING_START_TIMEOUT_S:.0f} s "
                    f"(status {GattServiceProviderAdvertisementStatus(status).name})"
                )
            await asyncio.sleep(0.1)

    def stop(self) -> None:
        """Stop advertising and detach handlers. Idempotent."""
        if self._provider is not None:
            try:
                if self._provider.advertisement_status in (
                    GattServiceProviderAdvertisementStatus.STARTED,
                    GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA,
                ):
                    self._provider.stop_advertising()
                    log.info("advertising stopped")
            except OSError as exc:
                log.warning("stop_advertising failed: %s", exc)
        for obj, remove, token in reversed(self._tokens):
            with contextlib.suppress(OSError):
                getattr(obj, remove)(token)
        self._tokens.clear()
        self._handlers.clear()
        self._chars.clear()
        self._provider = None

    # ----- notifications -----
    async def notify_sensor(self) -> int:
        """Notify subscribed clients of the public sensor value. Returns the number of results."""
        ch = self._chars.get("public_sensor")
        if ch is None:
            return 0
        try:
            if len(ch.subscribed_clients) == 0:
                return 0
        except TypeError:
            pass
        results = await ch.notify_value_async(to_buffer(self._state.sensor_value()))
        return len(results) if results is not None else 0

    # ----- event plumbing -----
    def _register(self, obj: Any, event: str, handler: Any) -> None:
        token = getattr(obj, f"add_{event}")(handler)
        self._handlers.append(handler)
        self._tokens.append((obj, f"remove_{event}", token))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _post(self, coro: Coroutine[Any, Any, None], deferral: Any) -> None:
        try:
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._spawn, coro)
        except (AssertionError, RuntimeError):  # loop closed during shutdown
            coro.close()
            deferral.complete()

    def _on_adv_status(self, _sender: Any, args: Any) -> None:
        try:
            status = GattServiceProviderAdvertisementStatus(args.status).name
            err = BluetoothError(args.error).name
        except (OSError, ValueError):
            status, err = "unknown", "unknown"
        log.info("advertisement status: %s (error %s)", status, err)

    def _make_subscribers_handler(self, name: str) -> Any:
        def handler(sender: Any, _args: Any) -> None:
            try:
                n = len(sender.subscribed_clients)
            except (OSError, TypeError):
                n = -1
            log.info("%s: subscribed clients = %s", name, n if n >= 0 else "unknown")

        return handler

    def _make_read_handler(self, name: str) -> Any:
        def handler(_sender: Any, args: Any) -> None:
            deferral = args.get_deferral()
            self._post(self._serve_read(name, args, deferral), deferral)

        return handler

    def _make_write_handler(self, name: str) -> Any:
        def handler(_sender: Any, args: Any) -> None:
            deferral = args.get_deferral()
            self._post(self._serve_write(name, args, deferral), deferral)

        return handler

    async def _serve_read(self, name: str, args: Any, deferral: Any) -> None:
        request = None
        try:
            request = await args.get_request_async()
            if request is None:
                log.info("%s read: request not available (refused by Windows or no longer pending)", name)
                return
            result = self._state.handle_read(name, offset=int(request.offset))
            if result.att_error is not None:
                request.respond_with_protocol_error(result.att_error)
            else:
                request.respond_with_value(to_buffer(result.value or b""))
        except Exception:
            log.exception("%s read: handler error", name)
            if request is not None:
                with contextlib.suppress(OSError):
                    request.respond_with_protocol_error(ATT_UNLIKELY_ERROR)
        finally:
            deferral.complete()

    async def _serve_write(self, name: str, args: Any, deferral: Any) -> None:
        request = None
        try:
            request = await args.get_request_async()
            if request is None:
                log.info("%s write: request not available (refused by Windows or no longer pending)", name)
                return
            data = bytes(request.value) if request.value is not None else b""
            result = self._state.handle_write(name, data, offset=int(request.offset), mtu=_session_mtu(args))
            if request.option == GattWriteOption.WRITE_WITH_RESPONSE:
                if result.att_error is None:
                    request.respond()
                else:
                    request.respond_with_protocol_error(result.att_error)
        except Exception:
            log.exception("%s write: handler error", name)
            if request is not None and request.option == GattWriteOption.WRITE_WITH_RESPONSE:
                with contextlib.suppress(OSError):
                    request.respond_with_protocol_error(ATT_UNLIKELY_ERROR)
        finally:
            deferral.complete()
