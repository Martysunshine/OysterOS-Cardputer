"""Local file-based control channel for `oyster-fixture reset-state` (same machine only).

The running fixture polls <control-dir>/reset.request about twice a second. When it appears, the
fixture resets FixtureState (authorization, challenge, transfer, virtual LED), deletes the request
and writes reset.ack containing the same request id. `reset-state` waits for that ack; without one it
withdraws its request so it cannot fire later. Default directory: %LOCALAPPDATA%\\oyster-lab\\fixture.
The channel carries no secrets; writing it can only return the fixture to its initial state.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

REQUEST_FILE = "reset.request"
ACK_FILE = "reset.ack"
CONTROL_POLL_S = 0.5
_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def default_control_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "oyster-lab" / "fixture"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, UnicodeDecodeError):
        return None
    except OSError:
        return None  # e.g. the writer still holds the file; retry on the next poll


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="ascii")
    os.replace(tmp, path)


class ControlChannel:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_control_dir()
        self.request_path = self.directory / REQUEST_FILE
        self.ack_path = self.directory / ACK_FILE

    def prepare(self) -> None:
        """Fixture start-up: create the directory and drop stale request/ack files."""
        self.directory.mkdir(parents=True, exist_ok=True)
        for p in (self.request_path, self.ack_path):
            p.unlink(missing_ok=True)

    def poll_reset(self, reset: Callable[[], None]) -> bool:
        """Fixture side: handle one pending reset request. True when a reset was performed."""
        req_id = _read(self.request_path)
        if req_id is None:
            return False
        if not _ID_RE.fullmatch(req_id):
            self.request_path.unlink(missing_ok=True)
            return False
        reset()
        _atomic_write(self.ack_path, req_id)
        self.request_path.unlink(missing_ok=True)
        return True

    def request_reset(
        self,
        timeout_s: float = 5.0,
        *,
        poll_s: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """CLI side: ask a running fixture to reset. True when it acknowledged within timeout_s."""
        self.directory.mkdir(parents=True, exist_ok=True)
        req_id = secrets.token_hex(8)
        self.ack_path.unlink(missing_ok=True)
        _atomic_write(self.request_path, req_id)
        deadline = clock() + timeout_s
        while True:
            if _read(self.ack_path) == req_id:
                self.ack_path.unlink(missing_ok=True)
                return True
            if clock() >= deadline:
                break
            sleep(poll_s)
        if _read(self.request_path) == req_id:
            self.request_path.unlink(missing_ok=True)
        if _read(self.ack_path) == req_id:  # acknowledged just at the deadline
            self.ack_path.unlink(missing_ok=True)
            return True
        return False
