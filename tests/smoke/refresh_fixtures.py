#!/usr/bin/env python3
"""Regenerate vendored smoke-test fixtures from live upstream clones.

Manual-use script — NOT invoked by CI. Refresh the snapshots whenever the
upstream ``examples/`` layout changes (every release at minimum; on every
drift-job failure). See ``tests/smoke/README.md`` for policy.

Usage
-----

    uv run python tests/smoke/refresh_fixtures.py

Behaviour
---------

For each repo in the vendored ``FIXTURE_PINS.json``:

1. Shallow-clone the repo at ``main`` into a scratch directory.
2. Copy over ``examples/`` (if present), ``pyproject.toml``, and ``README.md``
   into the vendored fixture root.
3. Strip ``.git/``, binaries (anything not matching the allowed suffix list),
   and any stray build artefacts.
4. Update ``FIXTURE_PINS.json`` with the new SHA + capture date.

The vendored fixture tree is tree-only: no ``.git`` directory and no history.
Target size ≤ 2 MB per repo.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURES_ROOT = _REPO_ROOT / "tests" / "fixtures" / "smoke"
_PINS_PATH = _FIXTURES_ROOT / "FIXTURE_PINS.json"

# File suffixes considered "text-ish" and safe to vendor. Everything else is
# stripped (binaries, images, archives).
_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".md",
        ".rst",
        ".txt",
        ".cfg",
        ".ini",
    }
)

# Repo slug → vendored fixture dir name.
_REPOS: dict[str, str] = {
    "pipecat-ai/pipecat": "pipecat",
    "pipecat-ai/pipecat-examples": "pipecat-examples",
}


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd, cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _shallow_clone(repo_slug: str, dest: Path, ref: str = "main") -> str:
    url = f"https://github.com/{repo_slug}.git"
    _run(["git", "clone", "--depth", "1", "--branch", ref, url, str(dest)])
    return _run(["git", "rev-parse", "HEAD"], cwd=dest)


def _copy_filtered(src: Path, dst: Path) -> None:
    """Copy ``src`` into ``dst``, stripping ``.git`` and non-text files."""
    if not src.exists():
        return
    if src.is_file():
        if src.suffix in _TEXT_SUFFIXES or src.name in {"README", "LICENSE"}:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return
    for entry in src.iterdir():
        if entry.name == ".git":
            continue
        _copy_filtered(entry, dst / entry.name)


def _rebuild_fixture(repo_slug: str, fixture_name: str, clone_root: Path) -> None:
    fixture_dir = _FIXTURES_ROOT / fixture_name
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture_dir.mkdir(parents=True, exist_ok=True)

    for top_level in ("examples", "pyproject.toml", "README.md"):
        _copy_filtered(clone_root / top_level, fixture_dir / top_level)


def refresh(ref: str = "main", dry_run: bool = False) -> None:
    pins: dict[str, dict[str, str]] = {}
    if _PINS_PATH.is_file():
        pins = json.loads(_PINS_PATH.read_text(encoding="utf-8"))

    today = date.today().isoformat()
    with tempfile.TemporaryDirectory() as scratch:
        scratch_root = Path(scratch)
        for repo_slug, fixture_name in _REPOS.items():
            clone_path = scratch_root / fixture_name
            sha = _shallow_clone(repo_slug, clone_path, ref=ref)
            print(f"Cloned {repo_slug} @ {sha[:8]}")
            if dry_run:
                continue
            _rebuild_fixture(repo_slug, fixture_name, clone_path)
            pins[fixture_name] = {
                "repo": repo_slug,
                "sha": sha,
                "date": today,
                "ref": ref,
                "note": f"vendored snapshot of {repo_slug}@{ref}",
            }

    if not dry_run:
        _PINS_PATH.write_text(
            json.dumps(pins, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {_PINS_PATH}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main", help="Git ref to clone (default: main)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Clone but do not overwrite fixtures."
    )
    args = parser.parse_args(argv)
    refresh(ref=args.ref, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
