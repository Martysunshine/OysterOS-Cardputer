"""Home Assistant connection settings, read from the environment (backend/.env is loaded by oyster.settings).

    HOME_ASSISTANT_URL        base URL of the instance, e.g. http://192.168.1.50:8123 (http or https)
    HOME_ASSISTANT_TOKEN      long-lived access token created in the Home Assistant profile page
    HOME_ASSISTANT_CA         optional: path of a PEM CA/certificate file to trust for an https URL
    OYSTER_HOME_ALLOWLIST     path of the allowlist JSON file; default <data_dir>/home_allowlist.json

The URL and the token are secrets as far as this code is concerned: they are held in fields that are left out
of repr(), never logged and never put into an error text or a response. `problem()` returns a short machine
code (no_url, bad_url, no_token, bad_token) naming what is missing, never the value.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_ALLOWLIST_NAME = "home_allowlist.json"
MAX_TOKEN_LEN = 2048


@dataclass(frozen=True)
class HomeConfig:
    url: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    ca_file: str = field(default="", repr=False)
    allowlist_path: Path = Path(DEFAULT_ALLOWLIST_NAME)

    @classmethod
    def from_env(cls, data_dir: Path, getenv: Callable[[str], str | None] = os.getenv) -> HomeConfig:
        def value(name: str) -> str:
            return (getenv(name) or "").strip()

        raw_path = value("OYSTER_HOME_ALLOWLIST")
        path = Path(raw_path) if raw_path else Path(DEFAULT_ALLOWLIST_NAME)
        if not path.is_absolute():
            path = Path(data_dir) / path  # relative paths are relative to the data directory
        return cls(
            url=value("HOME_ASSISTANT_URL").rstrip("/"),
            token=value("HOME_ASSISTANT_TOKEN"),
            ca_file=value("HOME_ASSISTANT_CA"),
            allowlist_path=path,
        )

    def problem(self) -> str | None:
        """Machine code for what stops the connection settings from being usable, or None."""
        if not self.url:
            return "no_url"
        parts = urlsplit(self.url)
        try:
            host = parts.hostname
            parts.port  # noqa: B018 - raises ValueError for an invalid port
        except ValueError:
            return "bad_url"
        if parts.scheme not in ("http", "https") or not host:
            return "bad_url"
        if parts.username or parts.password or parts.query or parts.fragment:
            return "bad_url"  # credentials or parameters in the URL would end up in logs and proxies
        if not self.token:
            return "no_token"
        if (
            len(self.token) > MAX_TOKEN_LEN
            or not self.token.isascii()
            or not self.token.isprintable()
            or " " in self.token
        ):
            return "bad_token"
        return None

    @property
    def tls(self) -> bool:
        return self.url.lower().startswith("https://")
