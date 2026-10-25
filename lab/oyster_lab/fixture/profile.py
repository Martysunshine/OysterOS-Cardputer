"""Lab GATT profile loader. schemas/lab-profile.json is the single source of truth (ADR-006)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_ENV = "OYSTER_LAB_PROFILE"
REQUIRED_CHARACTERISTICS = (
    "public_sensor",
    "restricted_enc",
    "restricted_mitm",
    "app_auth",
    "diag_manifest",
    "chunk_ctrl",
    "chunk_data",
    "diag_status",
    "virtual_led",
    "device_info",
)
_STATUS_NAMES = (
    "ok", "not_authorized", "bad_request", "stale_object", "out_of_range", "cancelled", "no_request"
)
_OPS = ("request", "cancel", "reset")


@dataclass(frozen=True)
class CharSpec:
    name: str
    uuid: str
    properties: tuple[str, ...]
    policy: dict[str, str]
    max_length: int

    def policy_for(self, operation: str) -> str | None:
        return self.policy.get(operation)


@dataclass(frozen=True)
class LabProfile:
    profile_id: str
    profile_version: int
    service_uuid: str
    characteristics: dict[str, CharSpec]
    object_id: str
    max_object_bytes: int
    ctrl_ops: dict[str, int]
    status_codes: dict[str, int]
    chunk_header_bytes: int
    auth_ttl_s: int
    challenge_bytes: int
    response_bytes: int
    led_values: dict[str, int]
    challenge_ttl_s: float = 60.0  # app_auth.challenge_ttl_s

    @property
    def device_info(self) -> bytes:
        return f"{self.profile_id};{self.profile_version}".encode("ascii")


def default_profile_path() -> Path:
    env = os.environ.get(PROFILE_ENV)
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "schemas" / "lab-profile.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"schemas/lab-profile.json not found; set {PROFILE_ENV}")


def _need(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ValueError(f"lab profile: missing {where}.{key}")
    return mapping[key]


def parse_profile(data: dict[str, Any]) -> LabProfile:
    chars_raw = _need(data, "characteristics", "profile")
    chars: dict[str, CharSpec] = {}
    for name in REQUIRED_CHARACTERISTICS:
        c = _need(chars_raw, name, "characteristics")
        chars[name] = CharSpec(
            name=name,
            uuid=str(_need(c, "uuid", name)).lower(),
            properties=tuple(_need(c, "properties", name)),
            policy=dict(_need(c, "policy", name)),
            max_length=int(_need(c, "max_length", name)),
        )
    transfer = _need(data, "transfer", "profile")
    app_auth = _need(data, "app_auth", "profile")
    if app_auth.get("algorithm") != "HMAC-SHA256":
        raise ValueError("lab profile: app_auth.algorithm must be HMAC-SHA256")
    ops = {k: int(v) for k, v in _need(transfer, "ctrl_ops", "transfer").items()}
    codes = {k: int(v) for k, v in _need(transfer, "status_codes", "transfer").items()}
    for k in _OPS:
        _need(ops, k, "transfer.ctrl_ops")
    for k in _STATUS_NAMES:
        _need(codes, k, "transfer.status_codes")
    led = data.get("write_values", {}).get("virtual_led", {}).get("allowed")
    if not led:
        raise ValueError("lab profile: missing write_values.virtual_led.allowed")
    return LabProfile(
        profile_id=str(_need(data, "profile_id", "profile")),
        profile_version=int(_need(data, "profile_version", "profile")),
        service_uuid=str(_need(data, "service_uuid", "profile")).lower(),
        characteristics=chars,
        object_id=str(_need(transfer, "object_id", "transfer")),
        max_object_bytes=int(_need(transfer, "max_object_bytes", "transfer")),
        ctrl_ops=ops,
        status_codes=codes,
        chunk_header_bytes=int(_need(transfer, "chunk_header_bytes", "transfer")),
        auth_ttl_s=int(_need(app_auth, "authorization_ttl_s", "app_auth")),
        challenge_bytes=int(_need(app_auth, "challenge_bytes", "app_auth")),
        response_bytes=int(_need(app_auth, "response_bytes", "app_auth")),
        led_values={k: int(v) for k, v in led.items()},
        challenge_ttl_s=float(_need(app_auth, "challenge_ttl_s", "app_auth")),
    )


def load_profile(path: Path | None = None) -> LabProfile:
    p = path or default_profile_path()
    return parse_profile(json.loads(Path(p).read_text(encoding="utf-8")))
