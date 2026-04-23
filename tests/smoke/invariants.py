"""Reusable layout invariants for offline and drift smoke checks.

Each helper is pure (no side effects, no pytest fixtures): it takes a ``Path``
to a repo root — optionally plus a fresh :class:`ExampleTaxonomyBuilder` — and
raises :class:`AssertionError` on violation.

Single source of truth for both the Phase 4 PR-gate tests in
``tests/smoke/test_pipecat_layout.py`` and the Phase 5 live-network drift
script in ``scripts/check_pipecat_drift.py``.

Design notes
------------
- Invariants parameterise over ``_discover_under_examples`` output, not over
  hard-coded topic lists. Adding or renaming a topic upstream MUST NOT break
  the assertions; removing *all* topics (the original regression) MUST.
- Helpers never import from private test modules so the drift script can reuse
  them directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from pipecat_context_hub.services.ingest.github_ingest import (
    _CODE_EXTENSIONS,
    _discover_under_examples,
    _find_example_dirs,
)
from pipecat_context_hub.services.ingest.taxonomy import TaxonomyBuilder


def _iter_discovered_dirs(repo_root: Path) -> list[Path]:
    """Return the directories a real refresh would discover in ``repo_root``.

    Uses ``_discover_under_examples`` when an ``examples/`` subtree exists
    (the pipecat-ai/pipecat layout), otherwise falls back to
    ``_find_example_dirs`` (which matches the pipecat-examples layout where
    example dirs live at the repo root).
    """
    examples_dir = repo_root / "examples"
    if examples_dir.is_dir():
        return list(_discover_under_examples(examples_dir))
    return list(_find_example_dirs(repo_root))


def _dir_contains_code_file(directory: Path) -> bool:
    for entry in directory.rglob("*"):
        if entry.is_file() and entry.suffix in _CODE_EXTENSIONS:
            return True
    return False


def assert_discovery_yields_code_files(repo_root: Path) -> None:
    """At least one discovered example dir must contain an ingestible code file.

    Catches the original regression where the discovery layer silently stopped
    returning anything (e.g. because ``examples/foundational/`` was removed and
    no fallback fired).
    """
    discovered = _iter_discovered_dirs(repo_root)
    assert discovered, f"No example dirs discovered under {repo_root}"
    dirs_with_code = [d for d in discovered if _dir_contains_code_file(d)]
    assert dirs_with_code, (
        f"No discovered example dir under {repo_root} contains code files "
        f"with extensions {sorted(_CODE_EXTENSIONS)}; discovered={discovered!r}"
    )


def assert_every_discovered_dir_has_taxonomy_entry(
    repo_root: Path, builder: TaxonomyBuilder
) -> None:
    """Each dir returned by discovery must have a matching taxonomy entry.

    This is the load-bearing lookup-key invariant: the ingester keys the
    taxonomy lookup on ``str(ex_dir.relative_to(repo_root))`` for every
    ``ex_dir`` discovery returns. If the builder emits a different path shape,
    the ingester silently loses metadata.
    """
    entries = builder.build_from_directory(repo_root, repo="fixture", commit_sha="SYNTHETIC")
    lookup = {entry.path: entry for entry in entries}
    discovered = _iter_discovered_dirs(repo_root)
    missing: list[str] = []
    for ex_dir in discovered:
        try:
            rel = str(ex_dir.relative_to(repo_root))
        except ValueError:
            rel = str(ex_dir)
        # Root fallback keys under "." — synthesised by the ingester, not the
        # builder. Skip that case here.
        if rel in (".", ""):
            continue
        if rel not in lookup:
            missing.append(rel)
    assert not missing, (
        f"Taxonomy lookup missing entries for discovered dirs under {repo_root}: "
        f"missing={missing!r}; available_paths={sorted(lookup)!r}"
    )


def assert_no_junk_entries(
    repo_root: Path,
    builder: TaxonomyBuilder,
    forbidden: Iterable[str] = ("src", "tests", "docs", "scripts"),
) -> None:
    """No taxonomy entry may have a top-level path component in ``forbidden``.

    Guards against the root-level fallback emitting entries for ``src/``,
    ``tests/``, ``docs/``, etc. when a packaged project falls through the
    dispatch.
    """
    entries = builder.build_from_directory(repo_root, repo="fixture", commit_sha="SYNTHETIC")
    forbidden_set = set(forbidden)
    junk: list[str] = []
    for entry in entries:
        first_component = entry.path.split("/", 1)[0]
        if first_component in forbidden_set:
            junk.append(entry.path)
    assert not junk, (
        f"Found junk taxonomy entries under {repo_root} whose top-level path "
        f"component is in {sorted(forbidden_set)!r}: {junk!r}"
    )


def assert_capability_tags_non_empty(builder: TaxonomyBuilder) -> None:
    """Every taxonomy entry must carry at least one capability tag.

    Entries without tags degrade search filter/rank quality; if the whole
    builder output is tagless, capability-tag derivation is broken.
    """
    entries = builder.entries
    assert entries, "Builder has no accumulated entries to check for capability tags"
    tagless = [entry.path for entry in entries if not entry.capabilities]
    assert not tagless, (
        f"Taxonomy entries with empty capability_tags: {tagless!r}"
    )
