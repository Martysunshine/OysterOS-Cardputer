"""Owner-enrolled hosts and the metric allowlist.

A host is enrolled with an explicit allowlist drawn from the fixed metric set: cpu_percent, load_1m,
mem_used_percent, disk_used_percent, uptime_s and services (only the enrolled service names). The adapter is
asked for the allowlisted names only, and `build_report` additionally drops anything else the adapter
returned and any value outside its documented range, so a host never exposes more than the owner allowed.
Hosts are visible to the devices named in `device_ids` only (empty = nobody but the owner).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from oyster.companion.adapters import AdapterReading, HostUnreachable
from oyster.companion.models import (
    HOST_ID_PATTERN,
    MAX_HOSTS,
    METRIC_BOUNDS,
    SERVICE_STATES,
    HealthMetrics,
    HealthReport,
    HostEnrol,
    HostRecord,
    ServiceStatus,
)
from oyster.companion.store import CompanionStore
from oyster.store import NotFound

_HOST_ID = re.compile(HOST_ID_PATTERN)


class HostRegistry:
    def __init__(self, store: CompanionStore, clock: Callable[[], float]):
        self._store = store
        self._clock = clock

    def enrol(self, req: HostEnrol) -> tuple[HostRecord, bool]:
        """Create or replace a host. Returns (stored record, created)."""
        now = self._clock()
        rec = HostRecord(**req.model_dump(), created_at=now, updated_at=now)
        created = self._store.put_host(rec, MAX_HOSTS)
        stored = self._store.get_host(rec.host_id)
        assert stored is not None  # just written
        return stored, created

    def get(self, host_id: str) -> HostRecord:
        host = self._store.get_host(host_id) if _HOST_ID.fullmatch(host_id) else None
        if host is None:
            raise NotFound(host_id)
        return host

    def get_for_device(self, host_id: str, device_id: str) -> HostRecord:
        """Unknown host and host not shared with this device are the same NotFound (404)."""
        host = self.get(host_id)
        if device_id not in host.device_ids:
            raise NotFound(host_id)
        return host

    def all_hosts(self) -> list[HostRecord]:
        return self._store.list_hosts()

    def visible_to(self, device_id: str) -> list[HostRecord]:
        return [h for h in self._store.list_hosts() if device_id in h.device_ids]

    def delete(self, host_id: str) -> None:
        if not _HOST_ID.fullmatch(host_id) or not self._store.delete_host(host_id):
            raise NotFound(host_id)


def _number(name: str, value: Any) -> float | int | None:
    """A finite number inside the metric's range, or None. Booleans and strings are not numbers."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    low, high = METRIC_BOUNDS[name]
    if not low <= value <= high:
        return None
    if name == "uptime_s":
        return int(value) if float(value).is_integer() else None
    return round(float(value), 2)


def build_report(host: HostRecord, reading: AdapterReading, now: float) -> HealthReport:
    """The response for one host: allowlisted, in-range values only. Raises HostUnreachable when the
    adapter delivered nothing usable at all."""
    values: dict[str, float | int] = {}
    missing: list[str] = []
    for name in host.metrics:
        if name == "services":
            continue
        value = _number(name, reading.metrics.get(name))
        if value is None:
            missing.append(name)
        else:
            values[name] = value
    services: list[ServiceStatus] | None = None
    if "services" in host.metrics:
        if reading.services:
            services = [
                ServiceStatus(
                    name=svc,
                    state=state if (state := reading.services.get(svc)) in SERVICE_STATES else "unknown",
                )
                for svc in host.services  # enrolled names only, in enrolment order
            ]
        else:
            missing.append("services")
    if not values and not services:
        raise HostUnreachable("adapter returned no usable data")
    return HealthReport(
        host_id=host.host_id,
        state="partial" if missing else "ok",
        collected_at=int(now),
        metrics=HealthMetrics(**values),
        services=services,
        missing=missing,
    )
