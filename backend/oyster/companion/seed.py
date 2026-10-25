"""Seeding helpers for the owner's own machine (wired into `oyster seed-demo` by the CLI).

`ensure_local_host` enrols the PC the backend runs on as a read-only `local` host, so a paired device sees
real CPU, memory, disk and uptime without any manual API call. It writes through the same models and
registry as `POST /v1/admin/hosts`, so every limit of the API applies (id and name patterns, caps, allowlist
rules) and it checks the same adapter guarantees (read-only, supports the requested metrics).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from oyster.companion.adapters import LocalHostAdapter
from oyster.companion.audit import AuditLog
from oyster.companion.models import HostEnrol, HostRecord
from oyster.companion.registry import HostRegistry
from oyster.companion.store import CompanionStore
from oyster.store import NotFound

# What a local host reports by default. load_1m is left out: Windows has no load average.
LOCAL_HOST_METRICS = ("cpu_percent", "mem_used_percent", "disk_used_percent", "uptime_s")


@dataclass(frozen=True)
class SeedResult:
    host: HostRecord
    created: bool  # the host did not exist before
    changed: bool  # something was written (created, or an existing record differed)


def ensure_local_host(
    store_or_registry: CompanionStore | HostRegistry,
    device_ids: Iterable[str],
    host_id: str = "this-pc",
    display_name: str = "This PC",
    services: Sequence[str] = (),
    *,
    clock: Callable[[], float] = time.time,
    audit: AuditLog | None = None,
) -> SeedResult:
    """Make sure `host_id` is enrolled as a read-only local host visible to exactly `device_ids`.

    Idempotent: when the stored record already has this display name, adapter, metrics, services and device
    set, nothing is written. Otherwise the record is replaced (created_at is kept). `services` is the
    allowlist of Windows service names (up to 16); when non-empty the `services` metric is allowlisted too.
    `clock` is only used when a bare store is given; `audit` (optional) records the enrolment like the
    owner route does. Raises ValueError (pydantic validation) for ids or names the API would refuse, and
    oyster.store.Conflict when the 16-host cap is reached.
    """
    registry = (
        store_or_registry
        if isinstance(store_or_registry, HostRegistry)
        else HostRegistry(store_or_registry, clock)
    )
    wanted = HostEnrol(
        host_id=host_id,
        display_name=display_name,
        adapter="local",
        metrics=[*LOCAL_HOST_METRICS, *(["services"] if services else [])],
        services=list(services),
        device_ids=list(device_ids),
    )
    info = LocalHostAdapter(wanted.services).describe()
    if not info.read_only or not set(wanted.metrics) <= set(info.supported_metrics):
        raise ValueError("the local adapter cannot provide the requested metrics read-only")

    try:
        current: HostRecord | None = registry.get(wanted.host_id)
    except NotFound:
        current = None
    if current is not None and _same(current, wanted):
        return SeedResult(host=current, created=False, changed=False)

    record, created = registry.enrol(wanted)
    if audit is not None:
        what = "created" if created else "replaced"
        audit.record(
            "owner",
            "host_enrolled",
            record.host_id,
            "ok",
            f"{what} by seed metrics={','.join(record.metrics)} devices={len(record.device_ids)}",
        )
    return SeedResult(host=record, created=created, changed=True)


def _same(current: HostRecord, wanted: HostEnrol) -> bool:
    return (
        current.display_name == wanted.display_name
        and current.adapter == wanted.adapter
        and current.metrics == wanted.metrics
        and current.services == wanted.services
        and set(current.device_ids) == set(wanted.device_ids)
    )
