"""Offline PR-gating smoke tests over vendored fixture trees.

These tests ALWAYS run on every ``pytest`` call — they must stay offline,
deterministic, and ≤ 15 s wall time (no embedding model, no Chroma, no
network). The ``smoke`` marker is reserved for the Phase 5 live drift check
and is intentionally NOT applied here.

Fixtures live under ``tests/fixtures/smoke/<repo>/`` and are regenerated
manually via ``tests/smoke/refresh_fixtures.py``; see ``tests/smoke/README.md``
for the refresh cadence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipecat_context_hub.services.ingest.taxonomy import TaxonomyBuilder

from tests.smoke.invariants import (
    assert_capability_tags_non_empty,
    assert_discovery_yields_code_files,
    assert_every_discovered_dir_has_taxonomy_entry,
    assert_no_junk_entries,
)


def _fixture_params(
    pipecat_fixture_root: Path, pipecat_examples_fixture_root: Path
) -> list[tuple[str, Path]]:
    return [
        ("pipecat", pipecat_fixture_root),
        ("pipecat-examples", pipecat_examples_fixture_root),
    ]


@pytest.fixture(
    params=["pipecat", "pipecat-examples"],
    ids=["pipecat", "pipecat-examples"],
)
def fixture_repo_root(
    request: pytest.FixtureRequest,
    pipecat_fixture_root: Path,
    pipecat_examples_fixture_root: Path,
) -> Path:
    mapping = {
        "pipecat": pipecat_fixture_root,
        "pipecat-examples": pipecat_examples_fixture_root,
    }
    return mapping[request.param]


def test_discovery_yields_code_files(fixture_repo_root: Path) -> None:
    assert_discovery_yields_code_files(fixture_repo_root)


def test_every_discovered_dir_has_taxonomy_entry(fixture_repo_root: Path) -> None:
    builder = TaxonomyBuilder()
    assert_every_discovered_dir_has_taxonomy_entry(fixture_repo_root, builder)


def test_no_junk_entries(fixture_repo_root: Path) -> None:
    builder = TaxonomyBuilder()
    assert_no_junk_entries(fixture_repo_root, builder)


def test_capability_tags_non_empty(fixture_repo_root: Path) -> None:
    builder = TaxonomyBuilder()
    builder.build_from_directory(
        fixture_repo_root, repo="fixture", commit_sha="SYNTHETIC"
    )
    assert_capability_tags_non_empty(builder)
