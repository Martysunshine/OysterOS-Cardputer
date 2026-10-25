"""Fixtures for the Claude Code link tests (the support code is in _claude_support.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _claude_support import ClaudeKit
from oyster.claude.service import ClaudeConfig


@pytest.fixture
def kit_factory(tmp_path: Path):
    made: list[ClaudeKit] = []

    def make(config: ClaudeConfig | None = None, real_time: bool = False) -> ClaudeKit:
        k = ClaudeKit(tmp_path / f"k{len(made)}", config, real_time)
        made.append(k)
        return k

    yield make
    for k in made:
        k.close()


@pytest.fixture
def kit(kit_factory) -> ClaudeKit:
    k = kit_factory()
    k.dev_a = k.enroll("dev-a")
    k.dev_b = k.enroll("dev-b")
    return k
