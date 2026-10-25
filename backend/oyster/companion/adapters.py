"""Host adapters: the only way the companion reads a host. Typed reads only, by construction.

`HostAdapter` has exactly two methods, `describe()` and `health(metrics)`. There is deliberately no method
that takes a command, a script or a free-form argument, and `health` receives a frozenset of names from
the fixed metric set (models.METRIC_NAMES), so nothing a client sends can reach a host as input.

STATUS: only `MockHostAdapter` ships (deterministic fixtures and injectable faults, for tests and demos).
There is NO real Linux adapter yet and nothing here has touched a real host. Whoever adds one must
guarantee, and document in docs/COMPANION_API.md:
  - strictly read-only access (a dedicated unprivileged account, no sudo, no write-capable credentials);
  - an authenticated, encrypted transport with verified host identity (for SSH: pinned host key);
  - no value that came from a client or from a model is ever interpolated into a command line, a path or a
    query: metric names are looked up in a fixed table of constant read operations;
  - every call has a hard timeout and reports failure with the exceptions below, never with raw error text.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from oyster.companion.models import METRIC_NAMES, HostRecord


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


AdapterFactory = Callable[[HostRecord], HostAdapter]


def build_adapter(host: HostRecord) -> HostAdapter:
    """Default factory: one adapter per enrolled host, chosen by its adapter kind."""
    if host.adapter == "mock":
        return MockHostAdapter()
    raise HostUnreachable(f"no adapter for kind {host.adapter!r}")
