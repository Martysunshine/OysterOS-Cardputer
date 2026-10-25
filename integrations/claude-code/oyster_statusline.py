#!/usr/bin/env python3
"""Claude Code status line command that also reports the usage line to the Oyster device.

Claude Code runs this command whenever its status line refreshes and writes a JSON object to stdin (fields
used: model.display_name or model.id, cost.total_cost_usd, context_window.used_percentage, session_id and,
for claude.ai subscribers only, rate_limits.five_hour.used_percentage and
rate_limits.seven_day.used_percentage). The script

  1. prints one short ASCII line for the terminal, for example `Opus | ctx 8% | $0.01 | 5h 24% 7d 41%`, and
  2. posts the same numbers to the Oyster backend on this PC (loopback only, 1 s timeout, best effort) so
     the Cardputer can show them.

Nothing is invented: a value Claude Code did not send is left out of the line and sent as absent. The cost
is Claude Code's own estimate. The plan limits appear only when Claude Code sends them. Any error is
swallowed: the status line must never break. Standard library only. Settings: see oyster_hook.py
(OYSTER_BACKEND, OYSTER_ADMIN_TOKEN, OYSTER_ENV_FILE).
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oyster_hook as hook  # noqa: E402 - the sibling script holds the shared settings and HTTP code

POST_TIMEOUT_S = 1.0
STDIN_MAX = 1024 * 1024
MODEL_MAX = 40


def _get(data: object, *path: str) -> object:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _number(value: object, lo: float, hi: float) -> float | None:
    """`value` as a finite float inside [lo, hi], else None (bool and strings are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and lo <= number <= hi else None


def model_name(data: object) -> str | None:
    for path in (("model", "display_name"), ("model", "id")):
        value = _get(data, *path)
        if isinstance(value, str):
            cleaned = re.sub(r"[^\x20-\x7e]", "?", value).strip()[:MODEL_MAX]
            if cleaned:
                return cleaned
    return None


def read_values(data: object) -> dict[str, object]:
    """The numbers Claude Code sent, each None when absent or out of range."""
    return {
        "model": model_name(data),
        "cost_usd": _number(_get(data, "cost", "total_cost_usd"), 0, 1_000_000),
        "context_pct": _number(_get(data, "context_window", "used_percentage"), 0, 100),
        "five_hour_pct": _number(_get(data, "rate_limits", "five_hour", "used_percentage"), 0, 100),
        "seven_day_pct": _number(_get(data, "rate_limits", "seven_day", "used_percentage"), 0, 100),
    }


def status_text(values: Mapping[str, object]) -> str:
    parts = [str(values["model"] or "claude")]
    if values["context_pct"] is not None:
        parts.append(f"ctx {values['context_pct']:.0f}%")
    if values["cost_usd"] is not None:
        parts.append(f"${values['cost_usd']:.2f}")
    limits = []
    if values["five_hour_pct"] is not None:
        limits.append(f"5h {values['five_hour_pct']:.0f}%")
    if values["seven_day_pct"] is not None:
        limits.append(f"7d {values['seven_day_pct']:.0f}%")
    if limits:
        parts.append(" ".join(limits))
    return " | ".join(parts)


def report_payload(data: object, values: Mapping[str, object]) -> dict | None:
    """The body for POST /v1/claude/status, or None when the essentials (session, model, cost) are missing."""
    session = _get(data, "session_id")
    if not isinstance(session, str) or values["model"] is None or values["cost_usd"] is None:
        return None
    payload: dict = {
        "session": re.sub(r"[^A-Za-z0-9_.:-]", "_", session)[:64] or "unknown",
        "model": values["model"],
        "cost_usd": values["cost_usd"],
    }
    for key in ("context_pct", "five_hour_pct", "seven_day_pct"):
        if values[key] is not None:
            payload[key] = values[key]
    return payload


def handle(raw: bytes, env: Mapping[str, str], poster=hook.post_json) -> str:
    """stdin bytes in, the status line text out; the report is posted on the way. Never raises."""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    values = read_values(data)
    text = status_text(values) if isinstance(data, dict) else ""
    with contextlib.suppress(Exception):  # best effort: a missing or slow backend must not slow the line
        payload = report_payload(data, values)
        settings = hook.load_settings(env) if payload is not None else None
        if payload is not None and settings is not None:
            poster(settings[0], "/v1/claude/status", settings[1], payload, POST_TIMEOUT_S)
    return text


def main() -> int:
    with contextlib.suppress(Exception):
        raw = sys.stdin.buffer.read(STDIN_MAX)
        text = handle(raw, os.environ)
        if text:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
