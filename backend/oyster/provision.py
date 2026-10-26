"""`oyster provision-device`: host side of the serial provisioning protocol (docs/PROVISIONING.md, v1).

ASCII lines at 115200 baud; replies are `OK <field>`, `ERR <code> <text>` or `STATUS <json>`. Other lines
(device logs) are ignored while waiting for a reply. `CA-BEGIN` and `CA-LINE` have no success reply (only
`ERR ...` on failure); `CA-END` answers `OK ca <bytes>`. Secrets (Wi-Fi password, device token) are sent once,
never echoed: the transcript shows `***` in their place. Changes are staged on the device and committed
by DONE; any failure sends ABORT so nothing half-provisioned is persisted.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

PROTOCOL = "oyster-prov/1"
BAUD = 115200
MAX_LINE = 512  # bytes per line, newline excluded (docs/PROVISIONING.md); reference TARGET-ADD is 437
CA_MAX_BYTES = 4096
CA_LINE_GAP_S = 0.02  # pause between CA-LINE sends (see send loop)
REPLY_TIMEOUT_S = 5.0
DONE_TIMEOUT_S = 15.0


class ProvisioningError(RuntimeError):
    pass


def _ca_bytes(reply: str) -> int:
    """The byte count of an `OK ca <bytes>` reply (-1 when the reply has no count)."""
    parts = reply.split()
    try:
        return int(parts[2]) if len(parts) >= 3 else -1
    except ValueError:
        return -1


def masked(line: str) -> str:
    """Transcript form of a command: secret arguments replaced by ***."""
    parts = line.split(" ")
    if parts[0] == "SET-TOKEN":
        return "SET-TOKEN ***"
    if parts[0] == "SET-AI-KEY":
        return "SET-AI-KEY ***"
    if parts[0] == "SET-WIFI" and len(parts) >= 2:
        return f"SET-WIFI {parts[1]} ***"
    return line


def compact_target(target: dict) -> str:
    """Compact TargetProfile JSON for TARGET-ADD: untrusted notes and null fields are left out."""
    t = {k: v for k, v in target.items() if k != "notes" and v is not None}
    if isinstance(t.get("identity"), dict):
        t["identity"] = {k: v for k, v in t["identity"].items() if v is not None}
    return json.dumps(t, separators=(",", ":"), ensure_ascii=True)


class SerialLink:
    """Line-oriented request/reply over a pyserial port (or any object with write/readline/close)."""

    def __init__(
        self,
        port_or_serial,
        *,
        echo: Callable[[str], None] | None = None,
        reply_timeout_s: float = REPLY_TIMEOUT_S,
    ):
        if isinstance(port_or_serial, str):
            import serial  # pyserial

            # DTR and RTS are set before the port is opened: on the Cardputer's native USB-Serial/JTAG port
            # an asserted DTR at open time can reset the chip into its ROM bootloader.
            ser = serial.Serial()
            ser.port = port_or_serial
            ser.baudrate = BAUD
            ser.timeout = 0.2
            ser.write_timeout = 2
            ser.dtr = False
            ser.rts = False
            ser.open()
            self._ser = ser
        else:
            self._ser = port_or_serial
        self._echo = echo or (lambda _s: None)
        self._timeout = reply_timeout_s

    def close(self) -> None:
        self._ser.close()

    def pause(self, seconds: float) -> None:
        """Short gap between unacknowledged lines (overridable by tests)."""
        time.sleep(seconds)

    def _write(self, line: str, max_line: int) -> None:
        data = line.encode("ascii")
        if len(data) > max_line:
            raise ProvisioningError(
                f"{masked(line).split(' ')[0]}: line is {len(data)} bytes; the protocol allows {max_line}"
            )
        self._echo(f"> {masked(line)}")
        self._ser.write(data + b"\n")

    def send(self, line: str, *, max_line: int = MAX_LINE) -> None:
        """Write a command that has no success reply (CA-BEGIN, CA-LINE). Does not wait; an `ERR` line
        already waiting in the input fails fast, other waiting lines (device logs) are skipped."""
        self._write(line, max_line)
        while getattr(self._ser, "in_waiting", 0):
            raw = self._ser.readline()
            if not raw:
                break
            reply = raw.decode("ascii", errors="replace").strip()
            if reply.startswith("ERR"):
                self._echo(f"< {reply}")
                raise ProvisioningError(f"{masked(line).split(' ')[0]} rejected: {reply}")

    def command(
        self,
        line: str,
        *,
        timeout_s: float | None = None,
        max_line: int = MAX_LINE,
        expect: str | None = None,
    ) -> str:
        """Write a command and return its reply. With `expect`, only an OK reply starting with it counts;
        any `ERR` (also one answering an earlier reply-less command) fails."""
        self._write(line, max_line)
        deadline = time.monotonic() + (timeout_s or self._timeout)
        while time.monotonic() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            reply = raw.decode("ascii", errors="replace").strip()
            if reply.startswith(("OK", "ERR", "STATUS")):
                self._echo(f"< {reply}")
                if reply.startswith("ERR"):
                    raise ProvisioningError(f"{masked(line).split(' ')[0]} rejected: {reply}")
                if expect is not None and not reply.startswith(expect):
                    continue
                return reply
        raise ProvisioningError(
            f"no reply to {masked(line).split(' ')[0]} within {timeout_s or self._timeout:g} s"
        )


@dataclass
class ProvisionPlan:
    device_id: str
    backend_host: str
    backend_port: int
    wifi_ssid: str | None = None
    wifi_password: str | None = None
    ca_pem: str | None = None
    ai_key: str | None = None     # direct-AI provider key (secret); device answers without the backend
    ai_model: str | None = None   # direct-AI model id (plain setting)
    targets: list[dict] = field(default_factory=list)
    set_time: bool = True
    clear_targets: bool = True
    max_line: int = MAX_LINE


def _check_plan(plan: ProvisionPlan) -> None:
    if not plan.backend_host or " " in plan.backend_host:
        raise ProvisioningError("backend host must be a single token (LAN IP or certificate host name)")
    if not (0 < plan.backend_port < 65536):
        raise ProvisioningError("backend port out of range")
    if plan.ca_pem is not None and len(plan.ca_pem.encode()) > CA_MAX_BYTES:
        raise ProvisioningError(f"CA PEM exceeds {CA_MAX_BYTES} bytes")
    if (plan.wifi_ssid is None) != (plan.wifi_password is None):
        raise ProvisioningError("Wi-Fi SSID and password must be given together")
    if plan.wifi_ssid is not None and not (1 <= len(plan.wifi_ssid.encode("utf-8")) <= 32):
        raise ProvisioningError("Wi-Fi SSID must be 1-32 bytes")
    if plan.ai_key is not None:
        if not (8 <= len(plan.ai_key) <= 200) or any(ord(c) <= 0x20 or ord(c) > 0x7E for c in plan.ai_key):
            raise ProvisioningError("AI key must be 8-200 printable ASCII characters")
    if plan.ai_model is not None:
        if not (1 <= len(plan.ai_model) <= 64) or not all(c.isalnum() or c in "/-._" for c in plan.ai_model):
            raise ProvisioningError("AI model id must be 1-64 chars of letters, digits or / - . _")


def provision(link: SerialLink, plan: ProvisionPlan, token_source: Callable[[], str]) -> dict:
    """Run one provisioning session. token_source is called only after HELLO succeeds (enrolment rotates the
    device token, so it happens only when the device is actually there). Returns the device STATUS json."""
    _check_plan(plan)
    link.command(f"HELLO {PROTOCOL}")
    try:
        link.command(f"SET-ID {plan.device_id}")
        link.command(f"SET-BACKEND {plan.backend_host} {plan.backend_port}")
        if plan.wifi_ssid is not None:
            link.command(
                f"SET-WIFI {plan.wifi_ssid.encode('utf-8').hex()} {plan.wifi_password.encode('utf-8').hex()}"
            )
        if plan.ca_pem is not None:
            # CA-BEGIN/CA-LINE are acknowledged only by the CA-END reply (ERR earlier on failure)
            link.send("CA-BEGIN")
            expected = 0
            for pem_line in plan.ca_pem.strip().splitlines():
                text = pem_line.strip()
                link.send(f"CA-LINE {text}")
                expected += len(text) + 1  # the device stores each line plus one newline
                # USB CDC delivers a burst far faster than the firmware drains its serial buffer; a burst of
                # 20+ lines lost bytes mid-certificate on a real device, so the lines are paced.
                link.pause(CA_LINE_GAP_S)
            reply = link.command("CA-END", expect="OK ca")
            got = _ca_bytes(reply)
            if got != expected:
                raise ProvisioningError(
                    f"CA-END: the device stored {got} bytes, {expected} were sent (serial data lost; retry)"
                )
        if plan.set_time:
            link.command(f"SET-TIME {int(time.time())}")
        if plan.clear_targets:
            link.command("TARGETS-CLEAR")
        for t in plan.targets:
            link.command(f"TARGET-ADD {compact_target(t)}", max_line=plan.max_line)
        # Direct-AI settings: the model id is a plain setting, the key is a secret (masked in the transcript).
        if plan.ai_model is not None:
            link.command(f"SET-AI-MODEL {plan.ai_model}")
        if plan.ai_key is not None:
            link.command(f"SET-AI-KEY {plan.ai_key}")
        token = token_source()
        link.command(f"SET-TOKEN {token}")
        status = link.command("STATUS")
        link.command("DONE", timeout_s=DONE_TIMEOUT_S)
    except BaseException:
        try:
            link.command("ABORT")
        except ProvisioningError:
            pass
        raise
    try:
        return json.loads(status.partition(" ")[2] or "{}")
    except ValueError:
        return {"raw": status[:200]}
