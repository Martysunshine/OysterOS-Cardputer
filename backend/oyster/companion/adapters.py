"""Host adapters: the only way the companion reads a host. Typed reads only, by construction.

`HostAdapter` has exactly two methods, `describe()` and `health(metrics)`. There is deliberately no method
that takes a command, a script or a free-form argument, and `health` receives a frozenset of names from
the fixed metric set (models.METRIC_NAMES), so nothing a client sends can reach a host as input.

STATUS: two adapters ship.
  - `MockHostAdapter`: deterministic fixtures and injectable faults, for tests and demos.
  - `LocalHostAdapter`: read-only metrics of the machine the backend itself runs on, through psutil (no
    network, no credentials, no child processes). Verified on a Windows PC only.
There is NO remote (SSH) adapter yet and nothing here has touched another host. Whoever adds one must
guarantee, and document in docs/COMPANION_API.md:
  - strictly read-only access (a dedicated unprivileged account, no sudo, no write-capable credentials);
  - an authenticated, encrypted transport with verified host identity (for SSH: pinned host key);
  - no value that came from a client or from a model is ever interpolated into a command line, a path or a
    query: metric names are looked up in a fixed table of constant read operations;
  - every call has a hard timeout and reports failure with the exceptions below, never with raw error text.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import psutil

from oyster.companion.models import MAX_SERVICES, METRIC_NAMES, SERVICE_NAME_PATTERN, HostRecord

log = logging.getLogger("oyster.companion")


class AdapterError(Exception):
    """Base of the failures an adapter may report. The message is for logs only and is never sent on."""


class HostUnreachable(AdapterError):
    """The host could not be contacted or answered with garbage."""


class HostTimeout(AdapterError):
    """The host did not answer within the adapter's deadline."""


@dataclass(frozen=True)
class AdapterInfo:
    kind: str
    read_only: bool
    supported_metrics: frozenset[str]


@dataclass(frozen=True)
class AdapterReading:
    """Raw adapter output. Untrusted: the registry keeps only allowlisted, in-range values."""

    metrics: Mapping[str, Any] = field(default_factory=dict)
    services: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class HostAdapter(Protocol):
    def describe(self) -> AdapterInfo: ...

    def health(self, metrics: frozenset[str]) -> AdapterReading:
        """Read exactly the named metrics ('services' = the state of the services enrolled for the host)."""
        ...


Fault = Literal["unreachable", "timeout", "partial"]

DEFAULT_FIXTURE: Mapping[str, float | int] = {
    "cpu_percent": 12.5,
    "load_1m": 0.42,
    "mem_used_percent": 41.0,
    "disk_used_percent": 63.2,
    "uptime_s": 86_400,
}
DEFAULT_SERVICES: Mapping[str, str] = {"nginx": "active", "postgresql": "active", "backup": "inactive"}
# what a misbehaving adapter might hand back on top of what was asked for (see `overshare`)
OVERSHARE_EXTRA: Mapping[str, Any] = {"hostname": "mock-internal-name", "open_ports": [22, 5432]}
OVERSHARE_SERVICES: Mapping[str, str] = {"sshd": "active", "vpn-secret-service": "active"}


class MockHostAdapter:
    """Deterministic in-memory host. Not a model of any real system.

    fault:      "unreachable" / "timeout" raise the matching AdapterError; "partial" omits `drop`.
    overshare:  also returns every fixture value and unrequested extras, to prove the API drops them.
    `requested` records every metrics set the adapter was asked for (tests assert it never exceeds the
    allowlist).
    """

    def __init__(
        self,
        *,
        fixture: Mapping[str, Any] | None = None,
        services: Mapping[str, str] | None = None,
        fault: Fault | None = None,
        drop: frozenset[str] = frozenset({"disk_used_percent"}),
        overshare: bool = False,
    ):
        self._fixture = dict(DEFAULT_FIXTURE if fixture is None else fixture)
        self._services = dict(DEFAULT_SERVICES if services is None else services)
        self.fault = fault
        self.drop = drop
        self.overshare = overshare
        self.requested: list[frozenset[str]] = []

    def describe(self) -> AdapterInfo:
        return AdapterInfo(kind="mock", read_only=True, supported_metrics=frozenset(METRIC_NAMES))

    def health(self, metrics: frozenset[str]) -> AdapterReading:
        self.requested.append(frozenset(metrics))
        if self.fault == "unreachable":
            raise HostUnreachable("mock: unreachable")
        if self.fault == "timeout":
            raise HostTimeout("mock: timeout")
        wanted = set(METRIC_NAMES) if self.overshare else set(metrics)
        if self.fault == "partial":
            wanted -= set(self.drop)
        values: dict[str, Any] = {k: v for k, v in self._fixture.items() if k in wanted}
        services = dict(self._services) if "services" in wanted else {}
        if self.overshare:
            values.update(OVERSHARE_EXTRA)
            services.update(OVERSHARE_SERVICES)
        return AdapterReading(metrics=values, services=services)


_SERVICE_NAME = re.compile(SERVICE_NAME_PATTERN)
_SYSTEM_DRIVE = re.compile(r"[A-Za-z]:")
# Windows service control manager states -> the API's four states. "failed" is never produced: the manager
# reports a stopped service as stopped, and the exit code is not read. Transitional states are unknown.
_WINDOWS_SERVICE_STATES: Mapping[str, str] = {
    "running": "active",
    "stopped": "inactive",
    "paused": "inactive",
}


class LocalHostAdapter:
    """Read-only health of the machine the backend runs on. Reads through psutil and the C runtime only.

    Every metric is one constant read from a fixed table; the only text that reaches the operating system is
    the name of an enrolled service (already matched against the service-name pattern at enrolment and
    again here) passed to the service API as a name, never to a shell. Nothing is executed or modified.

    - cpu_percent: one short blocking sample (`cpu_sample_s`, default 0.2 s). psutil's non-blocking mode keeps
      its reference per thread and the API reads on pool threads, so a first call there would be a
      meaningless 0.0.
    - mem_used_percent, disk_used_percent (system drive), uptime_s (time since boot).
    - load_1m only where the C runtime has a real load average (not on Windows): otherwise it is reported as
      missing, never emulated, and the API answers `partial`.
    - services: Windows services by name; on any other platform, or when a service cannot be queried, the
      state is `unknown`.
    A metric that cannot be read is simply absent from the reading; `health` does not raise for that.
    """

    def __init__(
        self,
        services: Sequence[str] = (),
        *,
        clock: Callable[[], float] = time.time,
        cpu_sample_s: float = 0.2,
    ):
        self._services = tuple(s for s in services if _SERVICE_NAME.fullmatch(s))[:MAX_SERVICES]
        self._clock = clock
        self._cpu_sample_s = max(0.0, float(cpu_sample_s))
        self._disk_path = self._system_disk()

    @staticmethod
    def _system_disk(platform: str = sys.platform, env: Mapping[str, str] = os.environ) -> str:
        if platform != "win32":
            return "/"
        drive = env.get("SystemDrive", "C:")
        return (drive if _SYSTEM_DRIVE.fullmatch(drive) else "C:") + "\\"

    def describe(self) -> AdapterInfo:
        return AdapterInfo(kind="local", read_only=True, supported_metrics=frozenset(METRIC_NAMES))

    def health(self, metrics: frozenset[str]) -> AdapterReading:
        wanted = set(metrics) & set(METRIC_NAMES)
        readers: dict[str, Callable[[], float | int | None]] = {
            "cpu_percent": self._cpu,
            "load_1m": self._load,
            "mem_used_percent": self._memory,
            "disk_used_percent": self._disk,
            "uptime_s": self._uptime,
        }
        values: dict[str, Any] = {}
        for name, read in readers.items():
            if name in wanted:
                value = self._guarded(name, read)
                if value is not None:
                    values[name] = value
        services = self._read_services() if "services" in wanted else {}
        return AdapterReading(metrics=values, services=services)

    @staticmethod
    def _guarded(name: str, read: Callable[[], float | int | None]) -> float | int | None:
        try:
            return read()
        except Exception as e:  # one failing read only drops that metric
            log.warning("local metric %s unreadable (%s)", name, type(e).__name__)
            return None

    def _cpu(self) -> float:
        return float(psutil.cpu_percent(interval=self._cpu_sample_s or None))

    def _memory(self) -> float:
        return float(psutil.virtual_memory().percent)

    def _disk(self) -> float:
        return float(psutil.disk_usage(self._disk_path).percent)

    def _uptime(self) -> int:
        return max(0, int(self._clock() - psutil.boot_time()))

    @staticmethod
    def _load() -> float | None:
        getter = getattr(os, "getloadavg", None)  # absent on Windows
        return float(getter()[0]) if getter is not None else None

    def _read_services(self) -> dict[str, str]:
        getter = getattr(psutil, "win_service_get", None)  # absent on other platforms
        states: dict[str, str] = {}
        for name in self._services:
            state = "unknown"
            if getter is not None:
                try:
                    state = _WINDOWS_SERVICE_STATES.get(str(getter(name).status()), "unknown")
                except Exception as e:  # missing service, access denied, ...
                    log.info("service %s unreadable (%s)", name, type(e).__name__)
            states[name] = state
        return states


AdapterFactory = Callable[[HostRecord], HostAdapter]


def build_adapter(host: HostRecord) -> HostAdapter:
    """Default factory: one adapter per enrolled host, chosen by its adapter kind."""
    if host.adapter == "mock":
        return MockHostAdapter()
    if host.adapter == "local":
        return LocalHostAdapter(host.services)
    raise HostUnreachable(f"no adapter for kind {host.adapter!r}")
