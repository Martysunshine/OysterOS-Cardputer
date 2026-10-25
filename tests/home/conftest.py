"""Fixtures for the Smart Home tests. The environment is cleared of any real Home Assistant settings so that
no test depends on the machine it runs on."""

from __future__ import annotations

from pathlib import Path

import pytest
from _home_support import HomeKit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("HOME_ASSISTANT_URL", "HOME_ASSISTANT_TOKEN", "HOME_ASSISTANT_CA", "OYSTER_HOME_ALLOWLIST"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def kit_factory(tmp_path: Path):
    made: list[HomeKit] = []

    def make(**kw) -> HomeKit:
        k = HomeKit(tmp_path / f"k{len(made)}", **kw)
        made.append(k)
        return k

    yield make
    for k in made:
        k.close()


@pytest.fixture
def kit(kit_factory) -> HomeKit:
    return kit_factory()
