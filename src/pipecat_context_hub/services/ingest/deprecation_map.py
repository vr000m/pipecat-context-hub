"""Build and persist a deprecation map from pipecat framework source.

Parses deprecation information from three sources (in priority order):

1. ``DeprecatedModuleProxy`` usage in ``__init__.py`` and ``.py`` files
   under ``src/pipecat/services/`` (structured, reliable — but removed
   in latest pipecat HEAD as of PR #4240)
2. GitHub release notes ``### Deprecated`` / ``### Removed`` sections
   (structured, versioned, rich — primary source for current pipecat)
3. CHANGELOG ``### Deprecated`` / ``### Removed`` sections (best-effort
   supplement, stored as ``changelog_notes`` only)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches: DeprecatedModuleProxy(globals(), "old", "new")
# Captures the old and new string arguments (handles multi-line, trailing comma).
_PROXY_RE = re.compile(
    r'DeprecatedModuleProxy\s*\(\s*globals\(\)\s*,\s*'
    r'"([^"]+)"\s*,\s*"([^"]+)"\s*,?\s*\)',
    re.DOTALL,
)

# Matches bracket-expansion in module paths: "cartesia.[stt,tts]" or "[a,b,c]"
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")


@dataclass
class DeprecationEntry:
    """A single deprecated module path mapping."""

    old_path: str
    new_path: str | None = None
    deprecated_in: str | None = None
    removed_in: str | None = None
    note: str = ""


@dataclass
class DeprecationMap:
    """Map of deprecated module paths with fuzzy lookup.

    Entries are keyed by the full old module path
    (e.g., ``pipecat.services.grok``).
    """

    entries: dict[str, DeprecationEntry] = field(default_factory=dict)
    changelog_notes: list[DeprecationEntry] = field(default_factory=list)
    pipecat_commit_sha: str = ""

    def check(self, symbol: str) -> DeprecationEntry | None:
        """Fuzzy match a symbol against the deprecation map.

        Matches:
        - Exact: ``pipecat.services.grok`` → entry for that key
        - Prefix: ``pipecat.services.grok.llm`` → entry for ``pipecat.services.grok``
        - Reverse prefix: ``pipecat.services`` when key is ``pipecat.services.grok``
          (returns first match — useful for broad queries)
        """
        if symbol in self.entries:
            return self.entries[symbol]
        for key, entry in self.entries.items():
            if symbol.startswith(key + ".") or key.startswith(symbol + "."):
                return entry
        return None

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict."""
        return {
            "pipecat_commit_sha": self.pipecat_commit_sha,
            "entries": {
                k: {
                    "old_path": e.old_path,
                    "new_path": e.new_path,
                    "deprecated_in": e.deprecated_in,
                    "removed_in": e.removed_in,
                    "note": e.note,
                }
                for k, e in self.entries.items()
            },
            "changelog_notes": [
                {
                    "old_path": e.old_path,
                    "new_path": e.new_path,
                    "deprecated_in": e.deprecated_in,
                    "removed_in": e.removed_in,
                    "note": e.note,
                }
                for e in self.changelog_notes
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DeprecationMap:
        """Deserialize from a JSON-compatible dict."""
        entries: dict[str, DeprecationEntry] = {}
        raw_entries = data.get("entries", {})
        if isinstance(raw_entries, dict):
            for key, val in raw_entries.items():
                if isinstance(val, dict):
                    entries[key] = DeprecationEntry(
                        old_path=str(val.get("old_path", key)),
                        new_path=val.get("new_path"),
                        deprecated_in=val.get("deprecated_in"),
                        removed_in=val.get("removed_in"),
                        note=val.get("note", ""),
                    )
        changelog_notes: list[DeprecationEntry] = []
        raw_notes = data.get("changelog_notes", [])
        if isinstance(raw_notes, list):
            for val in raw_notes:
                if isinstance(val, dict):
                    changelog_notes.append(DeprecationEntry(
                        old_path=str(val.get("old_path", "")),
                        new_path=val.get("new_path"),
                        deprecated_in=val.get("deprecated_in"),
                        removed_in=val.get("removed_in"),
                        note=val.get("note", ""),
                    ))
        commit_sha = data.get("pipecat_commit_sha", "")
        return cls(
            entries=entries,
            changelog_notes=changelog_notes,
            pipecat_commit_sha=str(commit_sha) if commit_sha else "",
        )

    def save(self, path: Path) -> None:
        """Persist the deprecation map to a JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        logger.info("Deprecation map saved to %s (%d entries)", path, len(self.entries))

    @classmethod
    def load(cls, path: Path) -> DeprecationMap:
        """Load a deprecation map from a JSON file. Returns empty map on failure."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except Exception:
            logger.debug("Could not load deprecation map from %s", path)
            return cls()


def _expand_bracket_module(module_str: str) -> list[str]:
    """Expand bracket syntax in module path strings.

    ``"cartesia.[stt,tts]"`` → ``["cartesia.stt", "cartesia.tts"]``
    ``"[ai_service,image_service]"`` → ``["ai_service", "image_service"]``
    ``"lmnt.tts"`` → ``["lmnt.tts"]`` (no brackets, pass-through)
    """
    match = _BRACKET_RE.search(module_str)
    if not match:
        return [module_str]
    parts = [p.strip() for p in match.group(1).split(",")]
    prefix = module_str[: match.start()]
    suffix = module_str[match.end() :]
    return [prefix + p + suffix for p in parts]


def _infer_module_path(file_path: Path, pipecat_src_root: Path) -> str:
    """Infer the Python module path from a file's location under src/pipecat/.

    E.g., ``src/pipecat/services/grok/__init__.py`` → ``pipecat.services.grok``
    """
    try:
        rel = file_path.relative_to(pipecat_src_root)
    except ValueError:
        return ""
    parts = list(rel.parts)
    # Remove __init__.py or .py suffix
    if parts and parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def build_deprecation_map_from_source(
    pipecat_repo_path: Path,
    commit_sha: str = "",
) -> DeprecationMap:
    """Parse DeprecatedModuleProxy usage from the pipecat framework source.

    Scans all Python files under ``src/pipecat/`` for
    ``DeprecatedModuleProxy(globals(), "old", "new")`` calls and builds
    the deprecation map.

    Args:
        pipecat_repo_path: Path to the cloned pipecat framework repo.
        commit_sha: Current commit SHA for staleness detection.

    Returns:
        A DeprecationMap with all discovered deprecations.
    """
    entries: dict[str, DeprecationEntry] = {}
    src_root = pipecat_repo_path / "src" / "pipecat"

    if not src_root.is_dir():
        logger.warning("pipecat source root not found at %s", src_root)
        return DeprecationMap(entries=entries, pipecat_commit_sha=commit_sha)

    # Scan all Python files for DeprecatedModuleProxy usage
    resolved_root = pipecat_repo_path.resolve()
    for py_file in src_root.rglob("*.py"):
        # Security: reject symlinks and verify file stays within repo
        if py_file.is_symlink():
            continue
        try:
            py_file.resolve().relative_to(resolved_root)
            content = py_file.read_text(encoding="utf-8", errors="replace")
        except (ValueError, Exception):
            continue

        for match in _PROXY_RE.finditer(content):
            old_arg = match.group(1)
            new_arg = match.group(2)

            # Infer the parent module from file path.
            # Both __init__.py and .py files use the PARENT package as context.
            # E.g., grok/__init__.py → parent is "pipecat.services"
            #       ai_services.py   → parent is "pipecat.services"
            file_module = _infer_module_path(py_file, pipecat_repo_path / "src")
            if "." in file_module:
                parent_module = file_module.rsplit(".", 1)[0]
            else:
                parent_module = ""

            # Build the full old path
            old_full = f"{parent_module}.{old_arg}" if parent_module else old_arg

            # Expand bracket syntax in new_arg
            new_expanded = _expand_bracket_module(new_arg)

            # Build full new paths
            new_full_list = []
            for new_part in new_expanded:
                if parent_module:
                    new_full_list.append(f"{parent_module}.{new_part}")
                else:
                    new_full_list.append(new_part)

            # Create entry: old_path → new_path (join multiple with ", ")
            new_path_str = ", ".join(new_full_list) if new_full_list else None
            entries[old_full] = DeprecationEntry(
                old_path=old_full,
                new_path=new_path_str,
                note=f"Use {new_path_str} instead" if new_path_str else "",
            )

            logger.debug("Deprecation: %s → %s", old_full, new_path_str)

    logger.info(
        "Built deprecation map: %d entries from DeprecatedModuleProxy",
        len(entries),
    )

    return DeprecationMap(entries=entries, pipecat_commit_sha=commit_sha)


def build_deprecation_map_from_changelog(
    changelog_path: Path,
    existing_map: DeprecationMap | None = None,
    *,
    repo_root: Path | None = None,
) -> DeprecationMap:
    """Supplement a deprecation map with CHANGELOG entries (best-effort).

    Parses ``### Deprecated`` and ``### Removed`` sections from CHANGELOG.md.
    Notes are stored in ``changelog_notes`` (not ``entries``) since they are
    keyed by description, not module paths, and cannot be matched by ``check()``.

    Args:
        changelog_path: Path to CHANGELOG.md in the pipecat repo.
        existing_map: Map to supplement (notes are added, not replaced).
        repo_root: If provided, reject symlinks and paths that resolve
            outside this root (same containment guard as source scanner).

    Returns:
        The supplemented map (or a new one if existing_map is None).
    """
    result = existing_map or DeprecationMap()

    if not changelog_path.is_file():
        logger.debug("CHANGELOG not found at %s", changelog_path)
        return result

    # Security: reject symlinks and verify path stays within repo root
    if changelog_path.is_symlink():
        logger.warning("CHANGELOG is a symlink, skipping: %s", changelog_path)
        return result
    if repo_root is not None:
        try:
            changelog_path.resolve().relative_to(repo_root.resolve())
        except ValueError:
            logger.warning("CHANGELOG resolves outside repo root, skipping: %s", changelog_path)
            return result

    try:
        content = changelog_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return result

    # Parse version sections: ## [0.0.100] - 2024-xx-xx
    version_re = re.compile(r"^##\s+\[([^\]]+)\]", re.MULTILINE)
    section_re = re.compile(r"^###\s+(Deprecated|Removed)", re.MULTILINE)

    current_version: str | None = None
    current_section: str | None = None
    changelog_entries: list[tuple[str, str, str]] = []  # (version, section, line)

    for line in content.splitlines():
        version_match = version_re.match(line)
        if version_match:
            current_version = version_match.group(1)
            current_section = None
            continue
        section_match = section_re.match(line)
        if section_match:
            current_section = section_match.group(1)
            continue
        if line.startswith("### "):
            current_section = None
            continue
        if current_version and current_section and line.strip().startswith("- "):
            changelog_entries.append((current_version, current_section, line.strip()[2:]))

    for version, section, description in changelog_entries:
        result.changelog_notes.append(DeprecationEntry(
            old_path=description[:80],
            new_path=None,
            deprecated_in=version if section == "Deprecated" else None,
            removed_in=version if section == "Removed" else None,
            note=description,
        ))

    logger.info("Added %d CHANGELOG deprecation/removal notes", len(changelog_entries))
    return result


# Matches backtick-wrapped pipecat module paths like `pipecat.services.grok.llm`
_MODULE_PATH_RE = re.compile(r"`(pipecat\.[a-zA-Z0-9_.]+)`")

# Matches backtick-wrapped class/function names like `GrokLLMService`
_SYMBOL_NAME_RE = re.compile(r"`([A-Z][a-zA-Z0-9]+(?:Service|Processor|Transport|Filter|Strategy|Analyzer|Observer|Frame|Params|Settings))`")

# Matches backtick-wrapped dotted identifiers like `SimliVideoService.InputParams`
_DOTTED_SYMBOL_RE = re.compile(r"`([A-Z][a-zA-Z0-9]+(?:\.[A-Z][a-zA-Z0-9]+)+)`")


def _extract_module_paths(text: str) -> list[str]:
    """Extract pipecat module paths from backtick-wrapped text.

    Returns deduplicated list of ``pipecat.*`` module paths found in the text.
    """
    paths = _MODULE_PATH_RE.findall(text)
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


_REPLACEMENT_RE = re.compile(
    r"[Uu]se\s+`(pipecat\.[a-zA-Z0-9_.]+)`",
)


def _extract_replacement(text: str, deprecated_paths: list[str]) -> str | None:
    """Try to extract the replacement path from deprecation text.

    Finds the "use X instead" boundary in the text, then extracts ALL
    pipecat module paths after that point as replacements.
    """
    # Find where "use" appears (case-insensitive)
    use_pos = -1
    for m in re.finditer(r"\b[Uu]se\b", text):
        use_pos = m.start()
        break
    if use_pos < 0:
        return None

    # Extract all pipecat paths after "use"
    after_use = text[use_pos:]
    paths = _MODULE_PATH_RE.findall(after_use)
    replacements = [p for p in paths if p not in deprecated_paths]
    if replacements:
        seen: set[str] = set()
        unique: list[str] = []
        for r in replacements:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        return ", ".join(unique)
    return None


def _fetch_release_notes(
    repo_slug: str,
    limit: int = 100,
) -> list[tuple[str, str]]:
    """Fetch release notes from GitHub via gh CLI.

    Uses ``gh release list`` for tags then ``gh release view`` for each
    body (``gh release list --json`` does not support the ``body`` field).

    Returns a list of ``(version, body)`` tuples.
    Falls back gracefully if ``gh`` is unavailable or unauthenticated.
    """
    try:
        # Step 1: get tag names
        list_result = subprocess.run(
            ["gh", "release", "list", "--repo", repo_slug, "--limit", str(limit),
             "--json", "tagName"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if list_result.returncode != 0:
            logger.warning(
                "gh release list failed (auth issue?) — release-note deprecation data "
                "will be incomplete: %s", list_result.stderr.strip(),
            )
            return []

        tags = [r["tagName"] for r in json.loads(list_result.stdout) if r.get("tagName")]

        # Step 2: fetch body for each tag
        notes: list[tuple[str, str]] = []
        for tag in tags:
            try:
                view_result = subprocess.run(
                    ["gh", "release", "view", tag, "--repo", repo_slug,
                     "--json", "body", "-q", ".body"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if view_result.returncode == 0 and view_result.stdout.strip():
                    version = tag.lstrip("v")
                    notes.append((version, view_result.stdout))
            except subprocess.TimeoutExpired:
                logger.debug("gh release view %s timed out", tag)
                continue

        logger.info("Fetched %d release notes from %s", len(notes), repo_slug)
        return notes
    except FileNotFoundError:
        logger.warning("gh CLI not found — release-note deprecation data will be incomplete")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("gh release list timed out")
        return []
    except Exception as exc:
        logger.debug("Failed to fetch release notes: %s", exc)
        return []


def _parse_release_body(
    version: str,
    body: str,
) -> list[DeprecationEntry]:
    """Parse ### Deprecated and ### Removed sections from a release body.

    Extracts module paths from backtick-wrapped text and creates
    DeprecationEntry objects keyed by module path when possible.
    """
    section_re = re.compile(r"^###\s+(Deprecated|Removed)", re.MULTILINE)
    entries: list[DeprecationEntry] = []

    current_section: str | None = None
    current_item_lines: list[str] = []

    def _flush_item() -> None:
        if not current_item_lines or not current_section:
            return
        text = " ".join(current_item_lines)
        all_paths = _extract_module_paths(text)
        # Split paths into deprecated vs replacement by finding the "use"
        # boundary. All pipecat paths after "use" are replacements.
        use_match = re.search(r"\b[Uu]se\b", text)
        if use_match:
            after_use = text[use_match.start():]
            replacement_paths = set(_MODULE_PATH_RE.findall(after_use))
        else:
            replacement_paths = set()
        deprecated_paths = [p for p in all_paths if p not in replacement_paths]
        replacement = ", ".join(sorted(replacement_paths)) if replacement_paths else None

        if deprecated_paths:
            # Create one entry per deprecated module path
            for path in deprecated_paths:
                entries.append(DeprecationEntry(
                    old_path=path,
                    new_path=replacement,
                    deprecated_in=version if current_section == "Deprecated" else None,
                    removed_in=version if current_section == "Removed" else None,
                    note=text[:500],
                ))
        else:
            # No module paths found — extract class/symbol names
            # Try dotted identifiers first (e.g. SimliVideoService.InputParams)
            dotted = _DOTTED_SYMBOL_RE.findall(text)
            symbols = _SYMBOL_NAME_RE.findall(text)
            all_symbols = list(dict.fromkeys(dotted + symbols))  # dedup, order preserved
            if all_symbols:
                for sym in all_symbols:
                    entries.append(DeprecationEntry(
                        old_path=sym,
                        new_path=replacement,
                        deprecated_in=version if current_section == "Deprecated" else None,
                        removed_in=version if current_section == "Removed" else None,
                        note=text[:500],
                    ))
            else:
                # Fall back to storing with a description key
                entries.append(DeprecationEntry(
                    old_path=f"release:{version}:{text[:60]}",
                    new_path=replacement,
                    deprecated_in=version if current_section == "Deprecated" else None,
                    removed_in=version if current_section == "Removed" else None,
                    note=text[:500],
                ))

    for line in body.splitlines():
        section_match = section_re.match(line)
        if section_match:
            _flush_item()
            current_item_lines = []
            current_section = section_match.group(1)
            continue
        if line.startswith("### "):
            _flush_item()
            current_item_lines = []
            current_section = None
            continue
        if current_section is None:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            _flush_item()
            current_item_lines = [stripped[2:]]
        elif stripped and current_item_lines:
            # Continuation line for the current item
            current_item_lines.append(stripped)

    _flush_item()
    return entries


def build_deprecation_map_from_releases(
    repo_slug: str = "pipecat-ai/pipecat",
    existing_map: DeprecationMap | None = None,
    *,
    limit: int = 100,
) -> DeprecationMap:
    """Build deprecation map from GitHub release notes.

    Fetches release notes via ``gh`` CLI, parses ``### Deprecated`` and
    ``### Removed`` sections, extracts module paths from backtick-wrapped
    text, and creates entries keyed by module path for ``check()`` matching.

    Entries from release notes do NOT overwrite existing entries (e.g.,
    from ``DeprecatedModuleProxy`` source parsing), but missing lifecycle
    fields (``deprecated_in``, ``removed_in``, ``new_path``) are merged
    into existing entries when the release notes provide them.

    Args:
        repo_slug: GitHub repo slug (e.g., ``pipecat-ai/pipecat``).
        existing_map: Map to supplement. New keys are added; existing keys
            get missing lifecycle fields merged from release data.
        limit: Maximum number of releases to fetch (default 100 to cover
            the full deprecation history).

    Returns:
        The supplemented map.
    """
    result = existing_map or DeprecationMap()

    releases = _fetch_release_notes(repo_slug, limit=limit)
    if not releases:
        logger.info("No release notes fetched — deprecation map unchanged")
        return result

    added = 0
    merged = 0
    for version, body in releases:
        entries = _parse_release_body(version, body)
        for entry in entries:
            existing = result.entries.get(entry.old_path)
            if existing is None:
                result.entries[entry.old_path] = entry
                added += 1
            else:
                # Merge missing lifecycle fields into the existing entry
                changed = False
                if not existing.deprecated_in and entry.deprecated_in:
                    existing.deprecated_in = entry.deprecated_in
                    changed = True
                if not existing.removed_in and entry.removed_in:
                    existing.removed_in = entry.removed_in
                    changed = True
                if not existing.new_path and entry.new_path:
                    existing.new_path = entry.new_path
                    changed = True
                if changed:
                    merged += 1

    logger.info(
        "Added %d deprecation entries from %d release notes (%d existing entries merged)",
        added,
        len(releases),
        merged,
    )
    return result
