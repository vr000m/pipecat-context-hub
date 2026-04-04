"""Build and persist a deprecation map from pipecat framework source.

Parses ``DeprecatedModuleProxy`` usage in ``__init__.py`` and ``.py`` files
under ``src/pipecat/services/`` to discover old→new module redirects.
Optionally supplements with CHANGELOG ``### Deprecated`` / ``### Removed``
sections (best-effort).
"""

from __future__ import annotations

import json
import logging
import re
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
) -> DeprecationMap:
    """Supplement a deprecation map with CHANGELOG entries (best-effort).

    Parses ``### Deprecated`` and ``### Removed`` sections from CHANGELOG.md.
    Entries are keyed by a normalized description (not module paths) and serve
    as supplementary context — the DeprecatedModuleProxy entries are primary.

    Args:
        changelog_path: Path to CHANGELOG.md in the pipecat repo.
        existing_map: Map to supplement (entries are added, not replaced).

    Returns:
        The supplemented map (or a new one if existing_map is None).
    """
    result = existing_map or DeprecationMap()

    if not changelog_path.is_file():
        logger.debug("CHANGELOG not found at %s", changelog_path)
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
