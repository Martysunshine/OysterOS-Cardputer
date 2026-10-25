"""Owner-only secret files (CA/server private keys, token files).

POSIX: created with mode 0600. Windows: mode bits mean nothing, so the file is created empty, its ACL is
reduced to the current user with `icacls <file> /inheritance:r /grant:r "<user>:(R,W)"`, and only then is the
secret written; the resulting ACL is read back and checked. When protection cannot be applied or verified the
caller gets a warning text and must not claim the file is owner-only.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# principals that must not remain on a secret file's ACL (matched against `icacls <file>` output)
_BROAD_PRINCIPALS = ("everyone", "builtin\\users", "authenticated users", "nt authority\\interactive")
_ICACLS_TIMEOUT_S = 15


def _user_name() -> str:
    return os.environ.get("USERNAME") or getpass.getuser()


def icacls_grant_args(path: Path, user: str) -> list[str]:
    """Command that drops inherited ACEs and leaves read/write for `user` only."""
    return ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"]


def restrict_to_current_user(
    path: Path,
    *,
    platform: str = sys.platform,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    user: str | None = None,
) -> str | None:
    """Apply owner-only access to `path`. Returns None on success, else a warning (never raises)."""
    if platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            return f"could not set mode 600 on {path}: {e}"
        return None
    user = user or _user_name()
    try:
        done = run(icacls_grant_args(path, user), capture_output=True, text=True, timeout=_ICACLS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not restrict {path} to {user} (icacls failed to run: {type(e).__name__})"
    if done.returncode != 0:
        return f"could not restrict {path} to {user} (icacls exit {done.returncode})"
    try:
        shown = run(["icacls", str(path)], capture_output=True, text=True, timeout=_ICACLS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not verify the ACL of {path} ({type(e).__name__})"
    listing = (shown.stdout or "").lower()
    if shown.returncode != 0 or user.lower() not in listing:
        return f"could not verify that {path} is restricted to {user}"
    if any(p in listing for p in _BROAD_PRINCIPALS):
        return f"{path} is still readable by a broad group after icacls; restrict it manually"
    return None


def write_private_file(path: Path, data: bytes, **kw) -> str | None:
    """Write a secret file; returns None when owner-only access was applied and verified, else a warning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)  # empty file first: the ACL is tightened before any secret byte is written
    warning = restrict_to_current_user(path, **kw)
    with open(path, "wb") as f:
        f.write(data)
    return warning
