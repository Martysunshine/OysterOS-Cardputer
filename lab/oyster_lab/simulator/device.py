"""Virtual Cardputer: the device side of the HTTP job/result contract.

This is an INDEPENDENT implementation of the device side. It does not import backend code; it talks to
the backend over HTTP with httpx and validates its own events against `schemas/event.schema.json`. It
registers a simulated session (all 8 device operations as capabilities), polls jobs, validates each
locally (policy.py), gates confirmation by operation type (and assertion operation), executes the scenario
behaviour, uploads events (every one labelled `source_mode: "simulated"`), and submits an idempotent
JobResult.

Order for a confirmation-gated job: report awaiting_local_confirmation -> owner approves -> report running
(must be accepted) -> execute -> upload events -> result carrying the approval. A rejected status report
(the backend expired or cancelled the job, e.g. the AI run stopped waiting for the approval) means "do not
execute": nothing runs and no result is submitted. For other jobs the running report is advisory on
transport errors, but an explicit rejection still stops execution.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from . import events as ev
from . import policy
from ._synthetic import (
    DEFAULT_CHUNK_MAX,
    DEFAULT_OBJECT_ID,
    diagnostic_object,
    object_sha256,
)
from .policy import PolicyInput, Verdict, evaluate_job
from .scenarios import (
    Scenario,
    default_ble_observations,
    default_gatt_entries,
    default_wifi_observations,
)

CAPABILITIES = list(policy.DEVICE_OPERATIONS)  # all 8 operations

# The argument names each operation handler reads from Job.arguments. Every name here must exist in
# the backend operation catalogue for that operation (backend/oyster/policy/catalogue.py). A test
# asserts this to prevent the device from reading argument names the backend/firmware never send.
# `target_id` travels in the Job envelope, not in arguments, so it is not listed here.
ARGUMENT_NAMES_READ: dict[str, tuple[str, ...]] = {
    "wifi_survey": ("passive", "show_hidden", "ms_per_channel", "channel", "room_label"),
    "ble_discover": ("duration_ms", "active", "interval_ms", "window_ms"),
    "ble_inspect_gatt": ("page_index",),
    "ble_read_profile_value": ("characteristic_id",),
    "ble_observe_notifications": ("characteristic_id", "duration_s", "max_events"),
    "ble_check_access_policy": ("test_id",),
    "ble_write_lab_value": ("characteristic_id", "value", "restore"),
    "ble_collect_diagnostics": ("object_id", "resume"),
}

# ATT error codes surfaced as separate facts from application-level denials.
ATT_INSUFFICIENT_AUTHENTICATION = 0x05
ATT_INSUFFICIENT_AUTHORIZATION = 0x08  # virtual_led write before application authorization, every mode
ATT_INSUFFICIENT_ENCRYPTION = 0x0F
READ_REFUSAL = "characteristic not readable by remote job (policy is not public)"


def _sec(encrypted: bool | None, authenticated: bool | None, authorized: bool | None) -> dict[str, Any]:
    return {
        "encrypted": encrypted,
        "authenticated_pairing": authenticated,
        "application_authorized": authorized,
    }


def _public_value(characteristic_id: str) -> bytes:
    if characteristic_id == "public_sensor":
        return (2300).to_bytes(2, "little")  # 23.00 C in centi-degrees
    if characteristic_id == "device_info":
        return b"oyster-lab-v1;1"
    if characteristic_id == "diag_status":
        return bytes([0, 0, 0])
    if characteristic_id == "virtual_led":
        return bytes([0])  # off
    if characteristic_id == "app_auth":
        return bytes([0]) + bytes(range(16))  # [state=0][16-byte challenge]
    return b""


@dataclass
class ExecResult:
    events: list[dict[str, Any]]
    outcome: str
    error: str | None = None

    @property
    def event_statuses(self) -> list[str]:
        return [e["status"] for e in self.events]


@dataclass
class JobReport:
    job_id: str
    operation: str
    verdict: str
    outcome: str | None = None
    statuses: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    event_statuses: list[str] = field(default_factory=list)
    confirmation_approved: bool | None = None
    skipped_duplicate: bool = False
    executed: bool = False  # True only when the operation actually ran on the (virtual) device
    error: str | None = None


@dataclass
class RunReport:
    scenario: str
    session_id: str
    resolved_choice: str | None
    jobs: list[JobReport] = field(default_factory=list)
    polled_job_ids: list[str] = field(default_factory=list)


class VirtualCardputer:
    """Executes scenario operations and produces firmware-shaped evidence events."""

    def __init__(self, scenario: Scenario, device_id: str, boot_id: str):
        self.scenario = scenario
        self.device_id = device_id
        self.boot_id = boot_id
        self._sequence = 0
        self._uptime_ms = 1000

    # --- event helpers ---
    def _next_ids(self) -> tuple[str, int, int]:
        seq = self._sequence
        self._sequence += 1
        self._uptime_ms += 500
        event_id = f"evt-{seq:06d}"
        return event_id, seq, self._uptime_ms

    def session_id(self) -> str | None:
        return self._session_id

    def set_session(self, session_id: str) -> None:
        self._session_id = session_id

    def _event(
        self, *, operation: str, transport: str, status: str, details: dict, target_id: str | None
    ) -> dict:
        event_id, seq, uptime = self._next_ids()
        return ev.make_event(
            event_id=event_id,
            session_id=self._session_id,
            device_id=self.device_id,
            boot_id=self.boot_id,
            sequence=seq,
            operation=operation,
            transport=transport,
            status=status,
            details=details,
            target_id=target_id,
            uptime_ms=uptime,
        )

    # --- dispatch ---
    def execute(self, job: dict[str, Any]) -> ExecResult:
        op = job["operation"]
        handler = getattr(self, f"_exec_{op}", None)
        if handler is None:
            return ExecResult([], "unsupported", f"operation {op} not implemented")
        return handler(job)

    # --- operations ---
    def _exec_wifi_survey(self, job: dict) -> ExecResult:
        args = job.get("arguments", {})
        room_label = args.get("room_label")
        obs = default_wifi_observations(self.scenario.inject_prompt_strings)
        scan = {
            "mode": "passive" if args.get("passive", True) else "active",
            "show_hidden": bool(args.get("show_hidden", True)),
            "ms_per_channel": int(args.get("ms_per_channel", 300)),
            "channel": int(args.get("channel", 0)),  # 0 = all channels
            "observed_ap_country": None,
        }
        events = [
            self._event(
                operation="wifi_survey",
                transport="wifi",
                status="success",
                target_id=None,
                details=ev.wifi_summary_details(
                    room_label=room_label, scan=scan, observations=obs, complete=True, overflow_count=0
                ),
            )
        ]
        pages = ev.paginate(obs)
        for i, page in enumerate(pages):
            events.append(
                self._event(
                    operation="wifi_survey",
                    transport="wifi",
                    status="success",
                    target_id=None,
                    details=ev.wifi_page_details(
                        page_index=i, observations=page, more_pages=i < len(pages) - 1
                    ),
                )
            )
        return ExecResult(events, "succeeded")

    def _exec_ble_discover(self, job: dict) -> ExecResult:
        args = job.get("arguments", {})
        scan = {
            "duration_ms": int(args.get("duration_ms", 6000)),
            "active": bool(args.get("active", False)),
            "interval_ms": int(args.get("interval_ms", 100)),
            "window_ms": int(args.get("window_ms", 100)),
        }
        target = self.scenario.primary_target or {}
        obs = default_ble_observations(self.scenario.inject_prompt_strings, target)
        fault = self.scenario.faults.get("ble_discover")
        malformed_count = 0
        address_changed = False
        prior_address = None
        if fault == "malformed_advertisement":
            obs.append(
                {
                    "obs_id": 99,
                    "address": "00:00:00:00:00:00",
                    "address_type": "unknown",
                    "rssi_dbm": -90,
                    "connectable": None,
                    "name": None,
                    "service_uuids": [],
                    "mfg_data_hex": "deadbe",
                    "malformed": True,
                    "seen_count": 1,
                }
            )
            malformed_count = 1
        if fault == "address_change" and obs:
            prior_address = obs[0]["address"]
            obs[0] = {**obs[0], "address": "7c:df:a1:00:00:a9", "address_type": "random"}
            address_changed = True
        events = [
            self._event(
                operation="ble_discover",
                transport="ble",
                status="success",
                target_id=None,
                details=ev.discovery_summary_details(
                    observations=obs,
                    complete=True,
                    overflow_count=0,
                    malformed_count=malformed_count,
                    address_changed=address_changed,
                    prior_address=prior_address,
                    scan=scan,
                ),
            )
        ]
        pages = ev.paginate(obs)
        for i, page in enumerate(pages):
            events.append(
                self._event(
                    operation="ble_discover",
                    transport="ble",
                    status="success",
                    target_id=None,
                    details=ev.discovery_page_details(
                        page_index=i, observations=page, more_pages=i < len(pages) - 1
                    ),
                )
            )
        return ExecResult(events, "succeeded")

    def _exec_ble_inspect_gatt(self, job: dict) -> ExecResult:
        args = job.get("arguments", {})
        requested_page = args.get("page_index")  # optional; this build returns the full hierarchy
        target = self.scenario.primary_target or {}
        entries = default_gatt_entries(target)
        pages = ev.paginate(entries)
        summary = ev.gatt_summary_details(
            service_uuid=ev.load_profile()["service_uuid"],
            total_entries=len(entries),
            complete=True,
            pages=len(pages),
        )
        if requested_page is not None:
            summary["requested_page_index"] = int(requested_page)
        events = [
            self._event(
                operation="ble_inspect_gatt",
                transport="ble",
                status="success",
                target_id=job.get("target_id"),
                details=summary,
            )
        ]
        for i, page in enumerate(pages):
            events.append(
                self._event(
                    operation="ble_inspect_gatt",
                    transport="ble",
                    status="success",
                    target_id=job.get("target_id"),
                    details=ev.gatt_page_details(page_index=i, entries=page, more_pages=i < len(pages) - 1),
                )
            )
        return ExecResult(events, "succeeded")

    def _exec_ble_read_profile_value(self, job: dict) -> ExecResult:
        args = job.get("arguments", {})
        char_id = args.get("characteristic_id", "")
        target_id = job.get("target_id")
        fault = self.scenario.faults.get("ble_read_profile_value")
        if not policy.remote_read_allowed(ev.load_profile(), char_id):
            # Same as the firmware: a remote job reads only public, side-effect-free characteristics.
            # Protected ones are checked by the access-policy assertions; nothing is sent to the target.
            refusal = {
                "profile_id": ev.load_profile().get("profile_id"),
                "characteristic_id": char_id,
                "read_policy": ev.read_policy(char_id),
                "error": READ_REFUSAL,
                "attempted": False,
            }
            return ExecResult(
                [
                    self._event(
                        operation="ble_read_profile_value",
                        transport="ble",
                        status="denied",
                        details=refusal,
                        target_id=target_id,
                    )
                ],
                "failed",
                READ_REFUSAL,
            )
        if fault == "timeout":
            details = ev.read_value_details(
                characteristic_id=char_id,
                value=None,
                security_state=_sec(False, False, None),
                raw_att_status=None,
            )
            return ExecResult(
                [
                    self._event(
                        operation="ble_read_profile_value",
                        transport="ble",
                        status="timeout",
                        details=details,
                        target_id=target_id,
                    )
                ],
                "failed",
                "read timed out",
            )
        value = _public_value(char_id)
        details = ev.read_value_details(
            characteristic_id=char_id,
            value=value,
            security_state=_sec(False, False, None),
            raw_att_status=None,
        )
        status = "success"
        return ExecResult(
            [
                self._event(
                    operation="ble_read_profile_value",
                    transport="ble",
                    status=status,
                    details=details,
                    target_id=target_id,
                )
            ],
            "succeeded",
        )

    def _exec_ble_observe_notifications(self, job: dict) -> ExecResult:
        args = job.get("arguments", {})
        char_id = args.get("characteristic_id", "public_sensor")
        duration_s = float(args.get("duration_s", 2.0))
        max_events = int(args.get("max_events", 3))
        observed = min(3, max_events)
        details = ev.notifications_details(
            characteristic_id=char_id,
            subscribed=True,
            notifications_observed=observed,
            duration_ms=int(duration_s * 1000),
            value_lengths=[2] * observed,
            security_state=_sec(False, False, None),
        )
        return ExecResult(
            [
                self._event(
                    operation="ble_observe_notifications",
                    transport="ble",
                    status="success",
                    details=details,
                    target_id=job.get("target_id"),
                )
            ],
            "succeeded",
        )

    def _assertion_for(self, test_id: str) -> dict[str, Any] | None:
        for a in ev.load_profile().get("assertions", []):
            if a.get("test_id") == test_id:
                return a
        return None

    def _exec_ble_check_access_policy(self, job: dict) -> ExecResult:
        args = job.get("arguments", {})
        test_id = args.get("test_id", "")
        target_id = job.get("target_id")
        assertion = self._assertion_for(test_id)
        if assertion is None:
            details = ev.access_check_details(
                test_id=test_id,
                characteristic_id=args.get("characteristic_id", ""),
                precondition="unknown",
                precondition_established=False,
                access="not_attempted",
                app_status=None,
                raw_att_status=None,
                security_before=_sec(None, None, None),
                security_after=_sec(None, None, None),
                security_transition=False,
                value_length=None,
                value_sha256=None,
            )
            return ExecResult(
                [
                    self._event(
                        operation="ble_check_access_policy",
                        transport="ble",
                        status="unsupported",
                        details=details,
                        target_id=target_id,
                    )
                ],
                "unsupported",
                f"unknown assertion {test_id!r}",
            )
        char_id = assertion["characteristic"]
        precondition = assertion["precondition"]
        mode = self.scenario.fixture_mode
        fault = self.scenario.faults.get("ble_check_access_policy")

        declared = len(diagnostic_object())
        obj_sha = object_sha256()

        # defaults
        established = True
        access = "not_attempted"
        app_status: str | None = None
        raw_att: int | None = None
        before = _sec(False, False, None)
        after = _sec(False, False, None)
        transition = False
        value_length: int | None = None
        value_sha256: str | None = None
        hash_verified: bool | None = None

        if fault == "timeout":
            access = "timeout"
        elif test_id == "public_sensor_is_public":
            access = "returned"
            value_length = 2
            value_sha256 = ev.sha256_hex(_public_value("public_sensor"))
        elif test_id == "restricted_value_requires_link_encryption":
            if fault == "unexpected_pairing_transition":
                before = _sec(False, False, None)
                after = _sec(True, False, None)
                transition = True
                access = "returned"  # stack auto-escalated to encryption before returning
                value_length = 16
                value_sha256 = ev.sha256_hex(b"LAB-ENC-OK")
            else:
                access = "denied_att"
                raw_att = ATT_INSUFFICIENT_ENCRYPTION
        elif test_id == "restricted_value_requires_authenticated_pairing":
            before = after = _sec(True, False, None)
            access = "denied_att"
            raw_att = ATT_INSUFFICIENT_AUTHENTICATION
        elif test_id == "restricted_diagnostics_requires_app_authorization":
            before = after = _sec(False, False, False)
            if mode == "vulnerable":
                access = "returned"
                app_status = "not_authorized"  # returned despite being unauthorized -> violation
                value_length = declared
                value_sha256 = obj_sha
                hash_verified = True
            else:
                access = "denied_app"
                app_status = "not_authorized"
                value_length = 1
        elif test_id == "diagnostics_available_after_app_authorization":
            # device performs the enrolled exchange to establish the precondition
            before = after = _sec(False, False, True)
            access = "returned"
            app_status = "authorized"
            value_length = declared
            value_sha256 = obj_sha
            hash_verified = True
        elif test_id == "virtual_led_write_requires_app_authorization":
            # The real fixture rejects this write in EVERY mode with ATT 0x08 (lab-profile.json
            # write_values.virtual_led.denial_att_status); there is no application-level status byte.
            before = after = _sec(False, False, False)
            access = "denied_att"
            raw_att = ATT_INSUFFICIENT_AUTHORIZATION
        else:
            access = "not_attempted"
            established = False

        details = ev.access_check_details(
            test_id=test_id,
            characteristic_id=char_id,
            precondition=precondition,
            precondition_established=established,
            access=access,
            app_status=app_status,
            raw_att_status=raw_att,
            security_before=before,
            security_after=after,
            security_transition=transition,
            value_length=value_length,
            value_sha256=value_sha256,
            hash_verified=hash_verified,
        )
        status_map = {
            "returned": "success",
            "denied_app": "denied",
            "denied_att": "denied",
            "timeout": "timeout",
            "error": "error",
            "not_attempted": "inconclusive",
        }
        status = status_map.get(access, "error")
        outcome = {"timeout": "failed", "error": "failed"}.get(access, "succeeded")
        if access == "timeout" and assertion.get("operation") in ("write", "transfer"):
            outcome = "unknown_outcome"  # the write/transfer may have reached the target
        error = None
        if access == "timeout":
            error = (
                "access check timed out; the write/transfer may have executed"
                if outcome == "unknown_outcome"
                else "access check timed out"
            )
        return ExecResult(
            [
                self._event(
                    operation="ble_check_access_policy",
                    transport="ble",
                    status=status,
                    details=details,
                    target_id=target_id,
                )
            ],
            outcome,
            error,
        )

    def _exec_ble_write_lab_value(self, job: dict) -> ExecResult:
        args = job.get("arguments", {})
        char_id = args.get("characteristic_id", "virtual_led")
        requested = args.get("value")
        target_id = job.get("target_id")
        if self.scenario.faults.get("ble_write_lab_value") == "timeout":
            details = ev.write_value_details(
                characteristic_id=char_id,
                requested_value=requested,
                starting_value=None,
                observed_result="timeout",
                restored=None,
                security_state=_sec(False, False, False),
                app_status=None,
            )
            return ExecResult(
                [
                    self._event(
                        operation="ble_write_lab_value",
                        transport="ble",
                        status="timeout",
                        details=details,
                        target_id=target_id,
                    )
                ],
                "unknown_outcome",
                "write timed out; the target state is unknown",
            )
        # The real fixture rejects an unauthorized virtual_led write in EVERY fixture mode with ATT 0x08
        # (fixture/protocol.py _write_led; lab-profile.json write_values.virtual_led.denial_att_status).
        details = ev.write_value_details(
            characteristic_id=char_id,
            requested_value=requested,
            starting_value=None,
            observed_result="denied",
            restored=None,
            security_state=_sec(False, False, False),
            app_status=None,
            raw_att_status=ATT_INSUFFICIENT_AUTHORIZATION,
        )
        return ExecResult(
            [
                self._event(
                    operation="ble_write_lab_value",
                    transport="ble",
                    status="denied",
                    details=details,
                    target_id=target_id,
                )
            ],
            "succeeded",
        )

    def _exec_ble_collect_diagnostics(self, job: dict) -> ExecResult:
        args = job.get("arguments", {})
        object_id = args.get("object_id") or DEFAULT_OBJECT_ID
        resume = bool(args.get("resume", False))
        target_id = job.get("target_id")
        fault = self.scenario.faults.get("ble_collect_diagnostics")
        mode = self.scenario.fixture_mode
        obj = diagnostic_object(object_id=object_id)
        declared = len(obj)
        full_sha = object_sha256(object_id)

        # There is no `authorize` job argument (catalogue): diagnostics require application
        # authorization, so the device performs its own enrolled app-auth exchange. A corrected
        # fixture grants access after it; a vulnerable fixture returns the object regardless.
        app_authorized = mode != "vulnerable"

        if fault == "interrupted":
            received = declared // 2
            details = ev.diagnostics_details(
                object_id=object_id,
                declared_length=declared,
                received_length=received,
                value_sha256=ev.sha256_hex(obj[:received]),
                hash_verified=False,
                app_status="authorized" if app_authorized else "not_authorized",
                security_state=_sec(False, False, app_authorized),
                chunks_received=max(1, received // DEFAULT_CHUNK_MAX),
            )
            details["resumed"] = resume
            return ExecResult(
                [
                    self._event(
                        operation="ble_collect_diagnostics",
                        transport="ble",
                        status="partial",
                        details=details,
                        target_id=target_id,
                    )
                ],
                "unknown_outcome",
                "transport lost mid-transfer; state not reconciled",
            )

        # Returned: legitimately after authorization (corrected), or as a vulnerability while
        # application_authorized is false (vulnerable). Either way the bytes hash-verify.
        details = ev.diagnostics_details(
            object_id=object_id,
            declared_length=declared,
            received_length=declared,
            value_sha256=full_sha,
            hash_verified=True,
            app_status="authorized" if app_authorized else "not_authorized",
            security_state=_sec(False, False, app_authorized),
            chunks_received=(declared + DEFAULT_CHUNK_MAX - 1) // DEFAULT_CHUNK_MAX,
        )
        details["resumed"] = resume
        return ExecResult(
            [
                self._event(
                    operation="ble_collect_diagnostics",
                    transport="ble",
                    status="success",
                    details=details,
                    target_id=target_id,
                )
            ],
            "succeeded",
        )

    # set after construction
    _session_id: str = ""


# --- HTTP orchestration ---


class Simulator:
    """Drives one scenario against a backend over HTTP (httpx)."""

    def __init__(
        self,
        scenario: Scenario,
        client: httpx.Client,
        *,
        device_id: str = "sim-cardputer-01",
        boot_id: str | None = None,
        auto_confirm: bool = False,
        confirm_wait_s: float = 0.2,
        result_retries: int = 3,
        confirm: Callable[[dict[str, Any]], bool] | None = None,
    ):
        self.scenario = scenario
        self.client = client
        self.device_id = device_id
        self.boot_id = boot_id or f"boot-{secrets.token_hex(4)}"
        self.auto_confirm = auto_confirm
        self.confirm_wait_s = confirm_wait_s
        self.result_retries = result_retries
        self._confirm = confirm  # the owner's decision on the device (tests); default: auto_confirm
        self.device = VirtualCardputer(scenario, device_id, self.boot_id)
        profile = ev.load_profile()  # the enrolled targets' lab profile, read locally
        self._profiles = {profile["profile_id"]: profile}
        self._validator = Draft202012Validator(ev.load_event_schema())
        self._executed: set[str] = set()
        self._submitted: set[str] = set()  # job ids whose result the backend accepted
        self._session_id = ""

    # --- session ---
    def register_session(self, session_file: str | Path | None = None) -> str:
        session_id = f"sess-{secrets.token_hex(4)}-001"
        body = {
            "session_id": session_id,
            "source_mode": "simulated",
            "capabilities": CAPABILITIES,
            "room_label": None,
        }
        resp = self.client.post("/v1/sessions", json=body)
        resp.raise_for_status()
        data = resp.json()
        self._session_id = data.get("session_id", session_id)
        self.device.set_session(self._session_id)
        if session_file is not None:
            self._write_session_file(Path(session_file))
        return self._session_id

    def _write_session_file(self, session_file: Path) -> None:
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text(self._session_id + "\n", encoding="utf-8")
        # The withheld choice goes only to a sibling `.withheld.sealed` file and is never printed, so the
        # false-positive evaluation stays blind for the operator. "Sealed" is NOT secrecy: it is plain
        # text next to the session file with no encryption or access control, and holds no credential.
        if self.scenario.resolved_choice:
            sealed = session_file.with_suffix(session_file.suffix + ".withheld.sealed")
            sealed.write_text(self.scenario.resolved_choice + "\n", encoding="utf-8")

    def sealed_file_for(self, session_file: str | Path) -> Path:
        p = Path(session_file)
        return p.with_suffix(p.suffix + ".withheld.sealed")

    # --- loop ---
    def run(
        self,
        *,
        max_jobs: int | None = None,
        once: bool = False,
        serve: bool = False,
        poll_interval_s: float = 1.0,
        idle_exit_s: float | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> RunReport:
        """Poll and execute jobs. Without `serve` it stops when no job is queued; with `serve` it keeps
        polling (like the firmware) until max_jobs, idle_exit_s without a job, or Ctrl+C."""
        if not self._session_id:
            self.register_session()
        report = RunReport(scenario=self.scenario.name, session_id=self._session_id, resolved_choice=None)
        n = 0
        last_activity = clock()
        try:
            while True:
                if max_jobs is not None and n >= max_jobs:
                    break
                try:
                    job = self._poll_job()
                except httpx.TransportError:
                    if not serve:
                        raise
                    job = None  # backend restarting or unreachable: keep polling
                if job is None:
                    if not serve:
                        break
                    if idle_exit_s is not None and clock() - last_activity >= idle_exit_s:
                        break
                    sleep(poll_interval_s)
                    continue
                report.polled_job_ids.append(job.get("job_id", "?"))
                report.jobs.append(self._process_job_safely(job))
                n += 1
                last_activity = clock()
                if once:
                    break
        except KeyboardInterrupt:
            pass
        return report

    def _poll_job(self) -> dict[str, Any] | None:
        resp = self.client.get("/v1/device/jobs")
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        return jobs[0] if jobs else None

    def _policy_input(self) -> PolicyInput:
        return PolicyInput(
            device_id=self.device_id,
            active_session_id=self._session_id,
            targets=self.scenario.targets,
            executed_job_ids=frozenset(self._executed),
            profiles=self._profiles,
        )

    def _process_job_safely(self, job: Any) -> JobReport:
        """One job never aborts the loop: an unexpected exception becomes a `failed` result (best effort)."""
        try:
            return self._process_job(job)
        except Exception as exc:  # noqa: BLE001 - the device loop must survive any single job
            job_id = job.get("job_id", "?") if isinstance(job, dict) else "?"
            operation = job.get("operation", "?") if isinstance(job, dict) else "?"
            reason = f"internal simulator error ({type(exc).__name__}); job not completed"
            jr = JobReport(
                job_id=job_id, operation=operation, verdict="error", outcome="failed", error=reason
            )
            if isinstance(job_id, str) and job_id != "?" and job_id not in self._submitted:
                self._executed.add(job_id)
                try:
                    self._submit_result(job_id, "failed", [], error=reason, confirmation=None)
                except Exception:  # noqa: BLE001 - backend unreachable: the deadline sweep ends the job
                    jr.error = reason + "; the failed result could not be submitted"
            return jr

    def _process_job(self, job: dict[str, Any]) -> JobReport:
        job_id = job.get("job_id", "?")
        jr = JobReport(job_id=job_id, operation=job.get("operation", "?"), verdict="")
        decision = evaluate_job(job, self._policy_input())
        jr.verdict = decision.verdict.value

        if decision.verdict is Verdict.reject_duplicate or job_id in self._executed:
            # Never re-execute a job id (local_policy.h).
            jr.skipped_duplicate = True
            jr.outcome = "cancelled"
            jr.error = "duplicate job id; not re-executed"
            return jr

        if not decision.accepted:
            outcome = policy.reject_outcome(decision.verdict)
            self._submit_result(job_id, outcome, [], error=decision.reason, confirmation=None)
            jr.outcome = outcome
            jr.error = decision.reason
            return jr

        # Mark executed up-front so a redelivery of the same id is not run again.
        self._executed.add(job_id)

        confirmation: dict[str, Any] | None = None
        if decision.requires_local_confirmation:
            # The confirmation gate is derived locally and cannot be lowered by the job's
            # requires_local_confirmation flag.
            if self._report_status(job_id, "awaiting_local_confirmation") is not True:
                return self._not_executed(jr, "awaiting_local_confirmation")
            jr.statuses.append("awaiting_local_confirmation")
            nonce = job.get("nonce")
            if not self._owner_approves(job):
                confirmation = {"job_id": job_id, "nonce": nonce, "approved": False}
                jr.confirmation_approved = False
                self._submit_result(
                    job_id, "cancelled", [], error="local confirmation not granted", confirmation=confirmation
                )
                jr.outcome = "cancelled"
                return jr
            confirmation = {"job_id": job_id, "nonce": nonce, "approved": True}
            jr.confirmation_approved = True
            if not policy.confirmation_valid(job, confirmation):
                self._submit_result(
                    job_id, "cancelled", [], error="confirmation binding invalid", confirmation=confirmation
                )
                jr.outcome = "cancelled"
                return jr
            # A gated job runs only after the backend accepted `running` (it may have expired the job
            # while the owner was deciding).
            if self._report_status(job_id, "running") is not True:
                return self._not_executed(jr, "running")
        elif self._report_status(job_id, "running") is False:
            return self._not_executed(jr, "running")  # explicit rejection; transport errors are advisory
        jr.statuses.append("running")

        jr.executed = True
        result = self.device.execute(job)
        self._validate_events(result.events)
        event_ids = self._upload_events(result.events)
        jr.event_ids = event_ids
        jr.event_statuses = result.event_statuses
        self._submit_result(job_id, result.outcome, event_ids, error=result.error, confirmation=confirmation)
        jr.outcome = result.outcome
        jr.error = result.error
        return jr

    def _owner_approves(self, job: dict[str, Any]) -> bool:
        if self._confirm is not None:
            return bool(self._confirm(job))
        if self.auto_confirm:
            return True
        time.sleep(self.confirm_wait_s)
        return False

    @staticmethod
    def _not_executed(jr: JobReport, status: str) -> JobReport:
        """The backend refused a lifecycle report: execute nothing and submit no result."""
        jr.statuses.append(f"{status}:rejected")
        jr.outcome = None
        jr.error = f"backend rejected the {status} report; job not executed"
        return jr

    def _validate_events(self, events: list[dict]) -> None:
        for e in events:
            errors = sorted(self._validator.iter_errors(e), key=str)
            if errors:
                raise ValueError(f"event {e.get('event_id')} failed schema: {errors[0].message}")
            if e["source_mode"] != "simulated":
                raise ValueError(f"event {e.get('event_id')} is not labelled simulated")

    def _report_status(self, job_id: str, status: str) -> bool | None:
        """True when accepted, False when the backend rejected it (do not execute), None on a transport
        error (unknown: a gated job does not run; for other jobs the report is advisory)."""
        try:
            resp = self.client.post(f"/v1/jobs/{job_id}/status", json={"status": status})
        except httpx.TransportError:
            return None
        return resp.is_success

    def _upload_events(self, events: list[dict]) -> list[str]:
        if not events:
            return []
        resp = self.client.post(f"/v1/sessions/{self._session_id}/events", json={"events": events})
        resp.raise_for_status()
        return [e["event_id"] for e in events]

    def _submit_result(
        self,
        job_id: str,
        outcome: str,
        event_ids: list[str],
        *,
        error: str | None,
        confirmation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": job_id,
            "idempotency_key": f"res-{job_id}-{self.boot_id}",
            "outcome": outcome,
            "event_ids": event_ids,
            "artifact_ids": [],
            "error": error,
            "confirmation": confirmation,
        }
        # Submission is idempotent (keyed by idempotency_key); only transport errors are retried.
        last_exc: Exception | None = None
        for _attempt in range(self.result_retries):
            try:
                resp = self.client.post(f"/v1/jobs/{job_id}/result", json=body)
                if resp.status_code == 409 and resp.json().get("error") != "events_missing":
                    self._submitted.add(job_id)
                    return resp.json()  # e.g. the job already ended backend-side; never re-executed
                resp.raise_for_status()
                self._submitted.add(job_id)
                return resp.json()
            except httpx.TransportError as exc:  # retry transient transport failures only
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        return {}
