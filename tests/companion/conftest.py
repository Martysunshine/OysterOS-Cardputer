"""Fixtures for the companion API tests (in-process app, temporary data directory, injected clock/RNG)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _companion_support import Clock, Rig


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def rig(tmp_path: Path, clock: Clock):
    r = Rig(tmp_path, clock)
    yield r
    r.close()


@pytest.fixture
def device_rig(tmp_path: Path, clock: Clock):
    """The LAN-facing app (audience "device"): owner routes must not exist there."""
    r = Rig(tmp_path / "dev", clock, audience="device")
    yield r
    r.close()
