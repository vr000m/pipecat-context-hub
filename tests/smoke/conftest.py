"""Fixture path helpers for offline smoke tests.

No git, no network, no embedding model — these fixtures only resolve paths
to the vendored snapshot trees under ``tests/fixtures/smoke/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "smoke"


@pytest.fixture(scope="session")
def smoke_fixtures_root() -> Path:
    """Absolute path to ``tests/fixtures/smoke/``."""
    return _FIXTURES_ROOT


@pytest.fixture(scope="session")
def pipecat_fixture_root(smoke_fixtures_root: Path) -> Path:
    """Absolute path to the vendored ``pipecat-ai/pipecat`` fixture root."""
    path = smoke_fixtures_root / "pipecat"
    assert path.is_dir(), f"Missing fixture tree: {path}"
    return path


@pytest.fixture(scope="session")
def pipecat_examples_fixture_root(smoke_fixtures_root: Path) -> Path:
    """Absolute path to the vendored ``pipecat-ai/pipecat-examples`` fixture root."""
    path = smoke_fixtures_root / "pipecat-examples"
    assert path.is_dir(), f"Missing fixture tree: {path}"
    return path
