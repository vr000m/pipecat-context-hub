#!/usr/bin/env python3
"""Live-network drift check for upstream Pipecat example layouts.

For each repo recorded in ``tests/fixtures/smoke/FIXTURE_PINS.json`` (or the
single ``--repo`` override), clone the repo at ``--ref`` (default ``main``)
into a temporary directory and run the reusable invariant helpers from
``tests/smoke/invariants.py`` against the clone root.

Exits non-zero on any assertion failure. Prints a structured Markdown-ish
report to stdout so the scheduled GitHub Action in
``.github/workflows/smoke-drift.yml`` can post or update a tracking issue.

Usage
-----

    uv run python scripts/check_pipecat_drift.py                       # all repos
    uv run python scripts/check_pipecat_drift.py --repo pipecat-ai/pipecat
    uv run python scripts/check_pipecat_drift.py --repo pipecat-ai/pipecat --ref main
    uv run python scripts/check_pipecat_drift.py --dry-run             # no clone

Design notes
------------
- Stdlib only (``subprocess``, ``tempfile``, ``pathlib``, ``json``,
  ``argparse``). No new runtime deps.
- Full ``git clone`` (``--depth 1``) — partial clones are unreliable across
  older gits on CI runners. The examples subtree is small, so a shallow clone
  is fast enough.
- Must be runnable as ``uv run python scripts/check_pipecat_drift.py`` from
  the repo root. ``src/`` is installed as a package via ``pyproject.toml``, so
  ``pipecat_context_hub`` imports resolve cleanly. We also prepend the repo
  root to ``sys.path`` so ``tests.smoke.invariants`` resolves without a test
  runner.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 - we construct the args ourselves
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --- Path bootstrap ---------------------------------------------------------
# Allow ``from tests.smoke.invariants import ...`` when invoked as a standalone
# script. ``pipecat_context_hub`` is already importable because ``src/`` is
# installed via pyproject, but ``tests/`` is not a package on sys.path by
# default outside pytest.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.smoke.invariants import (  # noqa: E402
    assert_capability_tags_non_empty,
    assert_discovery_yields_code_files,
    assert_every_discovered_dir_has_taxonomy_entry,
    assert_no_junk_entries,
)

from pipecat_context_hub.services.ingest.taxonomy import (  # noqa: E402
    TaxonomyBuilder,
)

_FIXTURE_PINS = (
    _REPO_ROOT / "tests" / "fixtures" / "smoke" / "FIXTURE_PINS.json"
)


@dataclass
class RepoResult:
    """Accumulates the outcome of a single-repo drift check."""

    repo: str
    ref: str
    clone_path: str | None = None
    checks: list[tuple[str, bool, str | None]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)

    def record(self, name: str, passed: bool, message: str | None = None) -> None:
        self.checks.append((name, passed, message))


def _load_pins(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        raise SystemExit(f"FIXTURE_PINS.json not found at {path}")
    with path.open("r", encoding="utf-8") as fh:
        data: dict[str, dict[str, str]] = json.load(fh)
    return data


def _resolve_repos(
    pins: dict[str, dict[str, str]], repo_override: str | None
) -> list[tuple[str, str]]:
    """Return a list of ``(slug, default_ref)`` tuples to check.

    ``repo_override`` takes precedence over the pins file. If the override is
    not in the pins file we still honour it (useful for checking a new repo
    before it is added to the pins).
    """
    if repo_override:
        default_ref = "main"
        for entry in pins.values():
            if entry.get("repo") == repo_override:
                default_ref = entry.get("ref", "main")
                break
        return [(repo_override, default_ref)]

    resolved: list[tuple[str, str]] = []
    for entry in pins.values():
        slug = entry.get("repo")
        if not slug:
            continue
        resolved.append((slug, entry.get("ref", "main")))
    return resolved


def _clone_repo(slug: str, ref: str, dest: Path) -> None:
    """Shallow-clone ``slug`` at ``ref`` into ``dest``.

    Uses ``--depth 1`` for speed; we only need the tree, not history.
    """
    url = f"https://github.com/{slug}.git"
    cmd = ["git", "clone", "--depth", "1", "--branch", ref, url, str(dest)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)  # nosec B603


def _check_capability_tags(repo_root: Path) -> None:
    """Populate a fresh builder then assert tags are non-empty."""
    builder = TaxonomyBuilder()
    builder.build_from_directory(repo_root, repo="drift-check", commit_sha=None)
    assert_capability_tags_non_empty(builder)


def _run_checks(repo_root: Path, result: RepoResult) -> None:
    """Run every invariant helper, collecting pass/fail per helper.

    Each helper that consumes a ``TaxonomyBuilder`` gets a fresh instance —
    ``TaxonomyBuilder.build_from_directory`` accumulates entries across calls,
    so sharing the same builder between helpers double-counts results and
    garbles error messages.
    """
    checks: list[tuple[str, Callable[[], None]]] = [
        (
            "assert_discovery_yields_code_files",
            lambda: assert_discovery_yields_code_files(repo_root),
        ),
        (
            "assert_every_discovered_dir_has_taxonomy_entry",
            lambda: assert_every_discovered_dir_has_taxonomy_entry(
                repo_root, TaxonomyBuilder()
            ),
        ),
        (
            "assert_no_junk_entries",
            lambda: assert_no_junk_entries(repo_root, TaxonomyBuilder()),
        ),
        (
            "assert_capability_tags_non_empty",
            lambda: _check_capability_tags(repo_root),
        ),
    ]

    for name, fn in checks:
        try:
            fn()
        except AssertionError as err:
            result.record(name, False, str(err))
        except Exception as err:  # noqa: BLE001 — surface unexpected failures
            result.record(
                name, False, f"unexpected {type(err).__name__}: {err}"
            )
        else:
            result.record(name, True)


def _check_repo(slug: str, ref: str, *, dry_run: bool) -> RepoResult:
    result = RepoResult(repo=slug, ref=ref)
    if dry_run:
        result.record("dry_run", True, "skipped clone and invariants (dry-run)")
        return result

    with tempfile.TemporaryDirectory(prefix="pipecat-drift-") as tmp:
        dest = Path(tmp) / slug.replace("/", "__")
        try:
            _clone_repo(slug, ref, dest)
        except subprocess.CalledProcessError as err:
            stderr = (err.stderr or "").strip()
            result.record(
                "git_clone",
                False,
                f"git clone {slug}@{ref} failed: {stderr or err}",
            )
            return result
        except FileNotFoundError:
            result.record(
                "git_clone", False, "git executable not found on PATH"
            )
            return result
        result.clone_path = str(dest)
        _run_checks(dest, result)
    return result


def _render_report(results: list[RepoResult]) -> str:
    """Return a Markdown-ish report suitable for an issue body."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    overall_ok = all(r.ok for r in results)
    status = "OK" if overall_ok else "DRIFT"

    lines: list[str] = []
    lines.append(f"# Upstream drift report ({status})")
    lines.append("")
    lines.append(f"- Generated: `{ts}`")
    lines.append(f"- Repos checked: {len(results)}")
    lines.append("")
    for result in results:
        header = f"## `{result.repo}` @ `{result.ref}`"
        lines.append(header)
        repo_status = "OK" if result.ok else "FAIL"
        lines.append(f"- Status: **{repo_status}**")
        lines.append("")
        lines.append("| Check | Result | Details |")
        lines.append("| --- | --- | --- |")
        for name, passed, message in result.checks:
            mark = "pass" if passed else "fail"
            detail = (message or "").replace("|", "\\|").replace("\n", " ")
            if len(detail) > 400:
                detail = detail[:397] + "..."
            lines.append(f"| `{name}` | {mark} | {detail} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clone upstream Pipecat repos at a ref and assert the example "
            "layout invariants hold."
        )
    )
    parser.add_argument(
        "--repo",
        help=(
            "Single upstream slug to check (e.g. 'pipecat-ai/pipecat'). "
            "Default: every repo in FIXTURE_PINS.json."
        ),
    )
    parser.add_argument(
        "--ref",
        default=None,
        help="Git ref to clone (sha, branch, or tag). Default: per-pin 'ref' or 'main'.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip clone + invariant execution; just print the repo list.",
    )
    parser.add_argument(
        "--pins",
        default=str(_FIXTURE_PINS),
        help=f"Path to FIXTURE_PINS.json (default: {_FIXTURE_PINS}).",
    )
    args = parser.parse_args(argv)

    pins = _load_pins(Path(args.pins))
    repos = _resolve_repos(pins, args.repo)
    if not repos:
        print("No repos to check (empty FIXTURE_PINS.json and no --repo).", file=sys.stderr)
        return 2

    results: list[RepoResult] = []
    for slug, default_ref in repos:
        ref = args.ref or default_ref
        results.append(_check_repo(slug, ref, dry_run=args.dry_run))

    report = _render_report(results)
    print(report)

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
