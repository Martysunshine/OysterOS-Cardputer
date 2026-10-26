"""Fixtures for the voice-question tests (support code in _voice_support.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _voice_support import VoiceKit


@pytest.fixture
def kit_factory(tmp_path: Path):
    made: list[VoiceKit] = []

    def make(**kw) -> VoiceKit:
        k = VoiceKit(tmp_path / f"k{len(made)}", **kw)
        made.append(k)
        return k

    yield make
    for k in made:
        k.close()


@pytest.fixture
def kit(kit_factory) -> VoiceKit:
    return kit_factory()
