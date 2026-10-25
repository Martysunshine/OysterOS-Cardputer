"""Typed models of the companion API (Issue #3): hosts, health reports, pairing and audit entries.

Every request model forbids unknown fields and bounds every string and list. There is deliberately no field
anywhere that carries a command, a script or arguments: the API can only name hosts, fixed metric names and
enrolled service names.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# The complete, fixed set of things a host can be asked for. Anything else is never read or returned.
METRIC_NAMES = ("cpu_percent", "load_1m", "mem_used_percent", "disk_used_percent", "uptime_s", "services")
MetricName = Literal[
    "cpu_percent", "load_1m", "mem_used_percent", "disk_used_percent", "uptime_s", "services"
]
ServiceState = Literal["active", "inactive", "failed", "unknown"]
SERVICE_STATES = ("active", "inactive", "failed", "unknown")
AdapterKind = Literal["mock"]  # no real Linux adapter ships yet (docs/COMPANION_API.md)

# (min, max) per numeric metric; a value outside its range is treated as missing, never forwarded
METRIC_BOUNDS: dict[str, tuple[float, float]] = {
    "cpu_percent": (0.0, 100.0),
    "load_1m": (0.0, 10_000.0),
    "mem_used_percent": (0.0, 100.0),
    "disk_used_percent": (0.0, 100.0),
    "uptime_s": (0, 315_360_000),  # ten years
}

MAX_HOSTS = 16  # enrolled hosts; keeps the device host list under the 4 KiB response bound
MAX_SERVICES = 16
MAX_DEVICE_IDS = 16
HOST_ID_PATTERN = r"^[0-9a-zA-Z_-]{1,40}$"
DEVICE_ID_PATTERN = r"^[0-9a-zA-Z_-]{1,40}$"  # same alphabet as POST /v1/admin/devices
SERVICE_NAME_PATTERN = r"^[0-9a-zA-Z_.@-]{1,64}$"

HostId = Annotated[str, Field(pattern=HOST_ID_PATTERN)]
DeviceId = Annotated[str, Field(pattern=DEVICE_ID_PATTERN)]
ServiceName = Annotated[str, Field(pattern=SERVICE_NAME_PATTERN)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[^\x00-\x1f\x7f]{1,64}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- hosts -------------------------------------------------------------------------------------------------
class HostEnrol(_Strict):
    """Owner request: enrol (or replace) a host with an explicit allowlist."""

    host_id: HostId
    display_name: DisplayName
    adapter: AdapterKind = "mock"
    metrics: list[MetricName] = Field(min_length=1, max_length=len(METRIC_NAMES))
    services: list[ServiceName] = Field(default_factory=list, max_length=MAX_SERVICES)
    device_ids: list[DeviceId] = Field(default_factory=list, max_length=MAX_DEVICE_IDS)

    @model_validator(mode="after")
    def _consistent(self) -> HostEnrol:
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("metrics must not repeat")
        if len(set(self.services)) != len(self.services):
            raise ValueError("services must not repeat")
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError("device_ids must not repeat")
        if ("services" in self.metrics) != bool(self.services):
            raise ValueError("'services' must be allowlisted exactly when service names are enrolled")
        return self


class HostRecord(_Strict):
    """An enrolled host as stored. device_ids names the devices that may see it (empty = owner only)."""

    host_id: HostId
    display_name: DisplayName
    adapter: AdapterKind
    metrics: list[MetricName]
    services: list[ServiceName]
    device_ids: list[DeviceId]
    created_at: float
    updated_at: float


class HostEnrolled(_Strict):
    host: HostRecord
    created: bool


class HostRemoved(_Strict):
    deleted: str


class HostSummary(_Strict):
    """What a device sees in the host list: no allowlist details, no service names."""

    host_id: str
    display_name: str


class HostList(_Strict):
    hosts: list[HostSummary]


class HostAdminList(_Strict):
    hosts: list[HostRecord]


# --- health ------------------------------------------------------------------------------------------------
class HealthMetrics(_Strict):
    """Only allowlisted metrics are present; the others are omitted from the JSON."""

    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    load_1m: float | None = Field(default=None, ge=0, le=10_000)
    mem_used_percent: float | None = Field(default=None, ge=0, le=100)
    disk_used_percent: float | None = Field(default=None, ge=0, le=100)
    uptime_s: int | None = Field(default=None, ge=0, le=315_360_000)


class ServiceStatus(_Strict):
    name: ServiceName
    state: ServiceState


class HealthReport(_Strict):
    host_id: str
    state: Literal["ok", "partial"]
    collected_at: int
    metrics: HealthMetrics
    services: list[ServiceStatus] | None = Field(default=None, max_length=MAX_SERVICES)
    missing: list[MetricName] = Field(default_factory=list, max_length=len(METRIC_NAMES))


class HostUnavailable(_Strict):
    """Body of the 502 answer when the adapter cannot deliver a reading."""

    error: Literal["host_unavailable"] = "host_unavailable"
    detail: str
    state: Literal["unreachable", "timeout"]
    host_id: str


# --- pairing -----------------------------------------------------------------------------------------------
class PairingCreate(_Strict):
    """Owner request. With device_id the code only works for that id; without it the device chooses."""

    device_id: DeviceId | None = None
    simulated: bool = False


class PairingCreated(_Strict):
    pairing_id: str
    code: str  # returned exactly once; only a salted hash is stored
    ttl_s: int
    expires_at: float
    device_id: str | None


class PairingRedeem(_Strict):
    code: str = Field(pattern=r"^[0-9a-zA-Z]{8}$")
    device_id: DeviceId
    display_name: DisplayName | None = None


class PairingRedeemed(_Strict):
    device_id: str
    token: str  # the bearer token of the existing device auth; shown once


# --- audit -------------------------------------------------------------------------------------------------
class AuditEntry(_Strict):
    id: int
    ts: float
    actor: str
    action: str
    target: str
    outcome: str
    detail: str


class AuditPage(_Strict):
    entries: list[AuditEntry]
    next_before_id: int | None
