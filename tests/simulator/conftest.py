"""Fixtures for the OysterOS UI simulator tests: headless, deterministic (the simulation clock is
advanced by the tests), no device, no backend, no network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SIM_DIR = REPO / "simulator"
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

try:  # the simulator renders with Pillow (simulator/requirements.txt); without it the tests are not collected
    import PIL  # noqa: F401
except ImportError:
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture
def sim():
    """A fresh simulator running the firmware profile (unfinished modules are greyed out)."""
    from oyster_ui_sim.engine import Simulator

    return Simulator()


@pytest.fixture
def preview():
    """A fresh simulator with the mock preview of the modules the firmware does not have."""
    from oyster_ui_sim.engine import Simulator

    return Simulator(preview=True)
