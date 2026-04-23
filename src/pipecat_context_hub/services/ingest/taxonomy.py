"""Taxonomy builder for Pipecat examples.

Scans local git clones of pipecat and pipecat-examples to automatically build
taxonomy manifests mapping each example to its foundational class and capability
tags. Fully automated — no manual curation file required.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pipecat_context_hub.shared.types import CapabilityTag, TaxonomyEntry

# ---------------------------------------------------------------------------
# Capability inference patterns
# ---------------------------------------------------------------------------

# Maps import substrings → tag name.  Checked against Python import lines.
_IMPORT_TAG_MAP: dict[str, str] = {
    "daily": "daily",
    "deepgram": "deepgram",
    "openai": "openai",
    "anthropic": "anthropic",
    "elevenlabs": "elevenlabs",
    "cartesia": "cartesia",
    "whisper": "whisper",
    "silero": "silero",
    "langchain": "langchain",
    "riva": "riva",
    "azure": "azure",
    "google": "google",
    "aws": "aws",
    "lmnt": "lmnt",
    "playht": "playht",
    "xtts": "xtts",
    "moondream": "moondream",
    "together": "together",
    "fal": "fal",
    "fastagent": "fastagent",
    "websocket": "websocket",
    "rtvi": "rtvi",
    "twilio": "twilio",
    "vonage": "vonage",
    "gstreamer": "gstreamer",
    "local": "local",
    "livekit": "livekit",
    "gemini": "gemini",
    "groq": "groq",
    "fireworks": "fireworks",
    "noisereduce": "noise-reduction",
    "krisp": "noise-reduction",
}

# Maps class-name substrings → tag name.  Checked against class Foo(...) lines.
_CLASS_TAG_MAP: dict[str, str] = {
    "Pipeline": "pipeline",
    "Transport": "transport",
    "STT": "stt",
    "TTS": "tts",
    "LLM": "llm",
    "ImageGen": "image-generation",
    "Vision": "vision",
    "VAD": "vad",
    "Bot": "bot",
    "Agent": "agent",
}

# Keyword patterns matched against README text (case-insensitive).
_README_TAG_PATTERNS: list[tuple[str, str]] = [
    (r"\binterrupt", "interruptible"),
    (r"\bfunction.?call", "function-calling"),
    (r"\btool.?use", "function-calling"),
    (r"\bvision", "vision"),
    (r"\bimage", "image-generation"),
    (r"\bvoice.?activity", "vad"),
    (r"\bwebsocket", "websocket"),
    (r"\brtvi", "rtvi"),
    (r"\btranscri", "stt"),
    (r"\bspeech.?to.?text", "stt"),
    (r"\btext.?to.?speech", "tts"),
    (r"\bllm\b", "llm"),
    (r"\bpipeline", "pipeline"),
    (r"\btransport", "transport"),
    (r"\bagent", "agent"),
    (r"\bwake.?word", "wake-word"),
    (r"\bdaily\b", "daily"),
    (r"\btwilio\b", "twilio"),
    (r"\blivekit\b", "livekit"),
]

# Directory-name fragments → tag name.
_DIR_TAG_MAP: dict[str, str] = {
    "interruptible": "interruptible",
    "stt": "stt",
    "tts": "tts",
    "llm": "llm",
    "vad": "vad",
    "vision": "vision",
    "websocket": "websocket",
    "rtvi": "rtvi",
    "image": "image-generation",
    "whisper": "whisper",
    "daily": "daily",
    "twilio": "twilio",
    "livekit": "livekit",
    "pipeline": "pipeline",
    "transport": "transport",
    "agent": "agent",
    "wake": "wake-word",
    "function": "function-calling",
    "tool": "function-calling",
    "noise": "noise-reduction",
    "chatbot": "chatbot",
    "storytelling": "storytelling",
}

# Regex to detect a numbered foundational directory like "07-interruptible".
_FOUNDATIONAL_DIR_RE = re.compile(r"^\d{2,}-")

# Override map for compound capability tags derived from topic-directory names.
# Topic layout (pipecat-ai/pipecat post-reorg) uses ``examples/<topic>/<example>/``;
# the topic dir name is a coarse capability label. Most topics pass through
# unchanged, but a handful need an extra synonym/related tag for better recall.
# Keep this map small and documented — do NOT enumerate every topic here.
_TOPIC_TAG_OVERRIDES: dict[str, list[str]] = {
    "function-calling": ["function-calling", "tools"],
    "realtime": ["realtime", "voice-ai"],
}

# File extensions that count as "code" for topic-tree scanning. Must stay in
# sync with ``github_ingest._CODE_EXTENSIONS`` so ``_scan_topic_tree`` emits a
# TaxonomyEntry for every dir that ``_discover_under_examples`` returns.
_TOPIC_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml"}
)

# Directories to ignore while walking topic trees (mirrors github_ingest skip list).
_TOPIC_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".git",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".egg-info",
    }
)


def _infer_tags_from_topic(topic_name: str) -> list[CapabilityTag]:
    """Derive capability tags from a topic directory name.

    New-layout pipecat examples group code under ``examples/<topic>/``, where
    the topic is a coarse capability label (e.g. ``function-calling``,
    ``realtime``, ``transports``). We expose the topic itself as a tag, and a
    small explicit override map adds related synonyms for a handful of
    compound topics. Unknown topics pass through as a single-tag entry.
    """
    tags_names = _TOPIC_TAG_OVERRIDES.get(topic_name, [topic_name])
    return [_make_tag(name, 1.0, "directory") for name in tags_names]


CapabilitySource = Literal["directory", "readme", "code", "manual"]


def _make_tag(name: str, confidence: float, source: CapabilitySource) -> CapabilityTag:
    return CapabilityTag(name=name, confidence=confidence, source=source)


def _dedup_tags(tags: list[CapabilityTag]) -> list[CapabilityTag]:
    """Keep highest-confidence tag per name, prefer code > readme > directory."""
    source_priority: dict[str, int] = {"manual": 4, "code": 3, "readme": 2, "directory": 1}
    best: dict[str, CapabilityTag] = {}
    for tag in tags:
        existing = best.get(tag.name)
        if existing is None:
            best[tag.name] = tag
        else:
            # Higher confidence wins; on tie, prefer higher-priority source.
            if tag.confidence > existing.confidence or (
                tag.confidence == existing.confidence
                and source_priority.get(tag.source, 0) > source_priority.get(existing.source, 0)
            ):
                best[tag.name] = tag
    return sorted(best.values(), key=lambda t: (-t.confidence, t.name))


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------


def _infer_tags_from_directory_name(dirname: str) -> list[CapabilityTag]:
    """Extract tags from a directory name like '07-interruptible' or 'chatbot'."""
    tags: list[CapabilityTag] = []
    lower = dirname.lower()
    for fragment, tag_name in _DIR_TAG_MAP.items():
        if fragment in lower:
            tags.append(_make_tag(tag_name, 1.0, "directory"))
    return tags


def _infer_tags_from_readme(readme_text: str) -> list[CapabilityTag]:
    """Extract tags from README content using keyword patterns."""
    tags: list[CapabilityTag] = []
    lower = readme_text.lower()
    for pattern, tag_name in _README_TAG_PATTERNS:
        if re.search(pattern, lower):
            tags.append(_make_tag(tag_name, 0.8, "readme"))
    return tags


def _infer_tags_from_code(code_text: str) -> list[CapabilityTag]:
    """Extract tags from Python source via imports and class names."""
    tags: list[CapabilityTag] = []
    seen: set[str] = set()
    for line in code_text.splitlines():
        stripped = line.strip()
        # Check imports
        if stripped.startswith(("import ", "from ")):
            for fragment, tag_name in _IMPORT_TAG_MAP.items():
                if fragment in stripped and tag_name not in seen:
                    tags.append(_make_tag(tag_name, 0.9, "code"))
                    seen.add(tag_name)
        # Check class references (both definitions and instantiations)
        for class_fragment, tag_name in _CLASS_TAG_MAP.items():
            if class_fragment in stripped and tag_name not in seen:
                tags.append(_make_tag(tag_name, 0.85, "code"))
                seen.add(tag_name)
    return tags


def _extract_summary_from_readme(readme_text: str) -> str:
    """Extract first non-heading, non-empty paragraph from README as summary."""
    lines = readme_text.strip().splitlines()
    summary_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Skip headings and empty lines at the beginning
        if not stripped:
            if summary_lines:
                break
            continue
        if stripped.startswith("#"):
            if summary_lines:
                break
            continue
        summary_lines.append(stripped)
    summary = " ".join(summary_lines)
    # Truncate to a reasonable length
    if len(summary) > 200:
        summary = summary[:197] + "..."
    return summary


def _find_key_files(example_dir: Path) -> list[str]:
    """Return list of notable files in an example directory."""
    key_files: list[str] = []
    for p in sorted(example_dir.iterdir()):
        if p.is_file():
            name = p.name
            if name.endswith(".py") or name == "README.md" or name == "requirements.txt":
                key_files.append(name)
    return key_files


# ---------------------------------------------------------------------------
# TaxonomyBuilder
# ---------------------------------------------------------------------------


class TaxonomyBuilder:
    """Builds taxonomy entries by scanning local directory structures.

    Operates on three layouts:
    - **pipecat main, legacy (pre-reorg)** (contains
      ``examples/foundational/NN-name/``): each numbered subdirectory
      becomes a foundational-class entry. ``foundational_class`` is now
      deprecated but still populated for these legacy entries.
    - **pipecat main, topic-based (current)** (contains
      ``examples/<topic>/<example>/``): each discovered example dir becomes
      an entry; ``foundational_class`` stays ``None``. Capability tags are
      derived from the topic dir name (see ``_TOPIC_TAG_OVERRIDES``).
    - **pipecat-examples** (contains project-level examples):
      each top-level subdirectory becomes an entry with capability tags.
    """

    def __init__(self) -> None:
        self._entries: list[TaxonomyEntry] = []

    # -- public API --------------------------------------------------------

    def build_from_foundational(
        self,
        root: Path,
        *,
        repo: str = "pipecat-ai/pipecat",
        commit_sha: str | None = None,
    ) -> list[TaxonomyEntry]:
        """Scan a ``examples/foundational/`` tree and produce entries.

        Handles two layouts:
        - **Subdirectory-per-example**: ``01-say-one-thing/bot.py``
        - **Flat file-per-example**: ``01-say-one-thing.py``

        Args:
            root: Path to the ``examples/foundational/`` directory.
            repo: GitHub repo slug.
            commit_sha: Optional commit SHA for provenance.

        Returns:
            List of TaxonomyEntry objects, one per subdirectory or flat file.
        """
        entries: list[TaxonomyEntry] = []
        if not root.is_dir():
            return entries
        for child in sorted(root.iterdir()):
            if child.is_dir():
                entry = self._build_entry_for_foundational(child, repo=repo, commit_sha=commit_sha)
                entries.append(entry)
            elif child.is_file() and child.suffix == ".py":
                entry = self._build_entry_for_foundational_file(
                    child, repo=repo, commit_sha=commit_sha
                )
                entries.append(entry)
        self._entries.extend(entries)
        return entries

    def build_entry_for_repo_root(
        self,
        root: Path,
        *,
        repo: str = "unknown",
        commit_sha: str | None = None,
    ) -> TaxonomyEntry:
        """Build a single TaxonomyEntry treating the repo root as the example.

        Used for single-project repos where the root IS the example (e.g.
        ``src/``-layout packages).  The returned entry uses ``path="."`` so the
        ingester's taxonomy lookup succeeds when
        ``_discover_root_level_examples`` falls back to repo root.
        """
        entry = self._build_entry_for_example(
            root, repo=repo, commit_sha=commit_sha,
        )
        entry = entry.model_copy(update={"path": "."})
        self._entries.append(entry)
        return entry

    def build_from_examples_repo(
        self,
        root: Path,
        *,
        repo: str = "pipecat-ai/pipecat-examples",
        commit_sha: str | None = None,
        require_example_markers: bool = False,
    ) -> list[TaxonomyEntry]:
        """Scan a pipecat-examples repo root and produce entries.

        Args:
            root: Path to the repo root directory.
            repo: GitHub repo slug.
            commit_sha: Optional commit SHA for provenance.
            require_example_markers: When ``True``, skip well-known
                non-example root dirs (``src``, ``tests``, ``docs``,
                ``scripts``, ``dashboard``, ``.github``, ``.claude``) in
                addition to the baseline ``.*``/``__pycache__``/
                ``node_modules`` filters. Used by ``build_from_directory``
                when falling back at the root of a packaged project
                (i.e. a repo that also contains ``src/`` or
                ``pyproject.toml``) to avoid emitting junk taxonomy
                entries for source/test/doc trees.

        Returns:
            List of TaxonomyEntry objects, one per subdirectory.
        """
        entries: list[TaxonomyEntry] = []
        if not root.is_dir():
            return entries
        packaged_project_skip = {
            "src",
            "tests",
            "docs",
            "scripts",
            "dashboard",
            ".github",
            ".claude",
        }
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            # Skip hidden dirs and common non-example dirs
            if child.name.startswith(".") or child.name in ("__pycache__", "node_modules"):
                continue
            if require_example_markers and child.name in packaged_project_skip:
                continue
            entry = self._build_entry_for_example(child, repo=repo, commit_sha=commit_sha)
            entries.append(entry)
        self._entries.extend(entries)
        return entries

    def build_from_topic_dirs(
        self,
        examples_dir: Path,
        *,
        repo: str = "pipecat-ai/pipecat",
        commit_sha: str | None = None,
    ) -> list[TaxonomyEntry]:
        """Scan a topic-based ``examples/<topic>/<example>/`` tree.

        Mirrors ``github_ingest._discover_under_examples`` exactly: if
        ``<topic>`` contains direct code files, emit one entry for
        ``<topic>``; otherwise emit one entry per subdirectory under
        ``<topic>``. Every emitted entry has
        ``path == str(ex_dir.relative_to(repo_root))`` where
        ``repo_root`` is the parent of ``examples_dir``.

        Args:
            examples_dir: Path to the ``examples/`` directory.
            repo: GitHub repo slug.
            commit_sha: Optional commit SHA for provenance.
        """
        repo_root = examples_dir.parent
        entries = self._scan_topic_tree(
            examples_dir, repo_root=repo_root, repo=repo, commit_sha=commit_sha
        )
        self._entries.extend(entries)
        return entries

    def build_from_directory(
        self,
        root: Path,
        *,
        repo: str = "unknown",
        commit_sha: str | None = None,
    ) -> list[TaxonomyEntry]:
        """Generic scan: auto-detects layout by sniffing the tree.

        Dispatch order:

        1. If ``examples/foundational/`` exists, scan the legacy foundational
           tree **and** walk any non-foundational sibling dirs under
           ``examples/`` as topic-layout entries (preserves the
           ``v0.0.96``-era mixed layout where ``examples/foundational/`` lives
           alongside ``examples/simple-chatbot/``).
        2. Else if ``examples/`` exists with any subdirs, treat as a pure
           topic-based layout (current pipecat main).
        3. Else fall back to ``build_from_examples_repo(root)`` for the
           ``pipecat-examples`` layout where top-level dirs are each an
           independent example.

        Args:
            root: Path to the repo root.
            repo: GitHub repo slug.
            commit_sha: Optional commit SHA for provenance.

        Returns:
            List of TaxonomyEntry objects.
        """
        examples_dir = root / "examples"
        foundational_dir = examples_dir / "foundational"
        if foundational_dir.is_dir():
            entries = self.build_from_foundational(
                foundational_dir, repo=repo, commit_sha=commit_sha
            )
            # Also scan non-foundational sibling dirs under examples/ using
            # the shared topic-tree helper so foundational + topic branches
            # cannot drift apart.
            sibling_entries = self._scan_topic_tree(
                examples_dir,
                repo_root=root,
                repo=repo,
                commit_sha=commit_sha,
                skip_names={"foundational"},
            )
            entries.extend(sibling_entries)
            self._entries.extend(sibling_entries)
            return entries
        if examples_dir.is_dir() and any(
            p.is_dir() for p in examples_dir.iterdir()
        ):
            return self.build_from_topic_dirs(
                examples_dir, repo=repo, commit_sha=commit_sha
            )
        # Root-level fallback (``pipecat-examples`` layout). When the repo
        # root also looks like a packaged project (contains ``src/`` or
        # ``pyproject.toml``), require_example_markers=True keeps junk
        # entries for ``src``/``tests``/``docs``/etc. out of the taxonomy.
        require_markers = (root / "src").is_dir() or (root / "pyproject.toml").is_file()
        return self.build_from_examples_repo(
            root,
            repo=repo,
            commit_sha=commit_sha,
            require_example_markers=require_markers,
        )

    @property
    def entries(self) -> list[TaxonomyEntry]:
        """All entries accumulated across build calls."""
        return list(self._entries)

    def query_by_class(self, foundational_class: str) -> list[TaxonomyEntry]:
        """Return entries matching a foundational class name."""
        return [e for e in self._entries if e.foundational_class == foundational_class]

    def query_by_tag(self, tag_name: str) -> list[TaxonomyEntry]:
        """Return entries that have a capability tag with the given name."""
        return [e for e in self._entries if any(t.name == tag_name for t in e.capabilities)]

    def query_by_example_id(self, example_id: str) -> TaxonomyEntry | None:
        """Return the entry with the given example_id, or None."""
        for e in self._entries:
            if e.example_id == example_id:
                return e
        return None

    def clear(self) -> None:
        """Remove all accumulated entries."""
        self._entries.clear()

    # -- private helpers ---------------------------------------------------

    def _build_entry_for_foundational(
        self,
        example_dir: Path,
        *,
        repo: str,
        commit_sha: str | None,
    ) -> TaxonomyEntry:
        """Build a TaxonomyEntry for a single foundational example directory."""
        dirname = example_dir.name
        example_id = f"foundational-{dirname}"
        foundational_class: str | None = dirname if _FOUNDATIONAL_DIR_RE.match(dirname) else None

        tags: list[CapabilityTag] = []
        tags.extend(_infer_tags_from_directory_name(dirname))

        readme_content: str | None = None
        summary = ""
        readme_path = example_dir / "README.md"
        if readme_path.is_file():
            readme_content = readme_path.read_text(encoding="utf-8", errors="replace")
            tags.extend(_infer_tags_from_readme(readme_content))
            summary = _extract_summary_from_readme(readme_content)

        # Scan Python files for code-level tags
        for py_file in sorted(example_dir.glob("*.py")):
            code = py_file.read_text(encoding="utf-8", errors="replace")
            tags.extend(_infer_tags_from_code(code))

        return TaxonomyEntry(
            example_id=example_id,
            repo=repo,
            path=f"examples/foundational/{dirname}",
            foundational_class=foundational_class,
            capabilities=_dedup_tags(tags),
            key_files=_find_key_files(example_dir),
            summary=summary,
            readme_content=readme_content,
            commit_sha=commit_sha,
            indexed_at=datetime.now(timezone.utc),
        )

    def _build_entry_for_foundational_file(
        self,
        py_file: Path,
        *,
        repo: str,
        commit_sha: str | None,
    ) -> TaxonomyEntry:
        """Build a TaxonomyEntry for a single flat foundational ``.py`` file.

        For repos where foundational examples are flat files rather than
        subdirectories (e.g. ``examples/foundational/01-say-one-thing.py``).
        """
        stem = py_file.stem  # e.g. "01-say-one-thing"
        example_id = f"foundational-{stem}"
        foundational_class: str | None = stem if _FOUNDATIONAL_DIR_RE.match(stem) else None

        tags: list[CapabilityTag] = []
        tags.extend(_infer_tags_from_directory_name(stem))

        code = py_file.read_text(encoding="utf-8", errors="replace")
        tags.extend(_infer_tags_from_code(code))

        return TaxonomyEntry(
            example_id=example_id,
            repo=repo,
            path=f"examples/foundational/{py_file.name}",
            foundational_class=foundational_class,
            capabilities=_dedup_tags(tags),
            key_files=[py_file.name],
            summary="",
            readme_content=None,
            commit_sha=commit_sha,
            indexed_at=datetime.now(timezone.utc),
        )

    def _scan_topic_tree(
        self,
        examples_dir: Path,
        *,
        repo_root: Path,
        repo: str,
        commit_sha: str | None,
        skip_names: frozenset[str] | set[str] | None = None,
    ) -> list[TaxonomyEntry]:
        """Walk an ``examples/`` dir in topic-layout style and emit entries.

        Shared helper used by both ``build_from_topic_dirs`` and the
        ``build_from_directory`` dispatch for legacy mixed layouts, so the
        two code paths cannot diverge.

        Mirrors ``github_ingest._discover_under_examples``: for each topic
        dir ``<topic>``, if it contains direct code files emit one entry for
        ``<topic>`` itself; otherwise emit one entry per subdirectory under
        ``<topic>``. Every emitted entry has
        ``path == str(ex_dir.relative_to(repo_root))``.

        ``skip_names`` lets callers exclude specific top-level topic names
        (used to skip ``foundational`` on mixed legacy layouts).
        """
        entries: list[TaxonomyEntry] = []
        if not examples_dir.is_dir():
            return entries
        skip = set(skip_names) if skip_names else set()
        for topic in sorted(examples_dir.iterdir()):
            if not topic.is_dir():
                continue
            if topic.name in skip:
                continue
            if topic.name.startswith(".") or topic.name in _TOPIC_SKIP_DIRS:
                continue
            sub_has_code = any(
                f.suffix in _TOPIC_CODE_EXTENSIONS
                for f in topic.iterdir()
                if f.is_file()
            )
            if sub_has_code:
                # Topic dir itself is the example (flat layout).
                entries.append(
                    self._build_entry_for_topic_example(
                        topic,
                        repo_root=repo_root,
                        repo=repo,
                        commit_sha=commit_sha,
                        topic_name=topic.name,
                    )
                )
            else:
                # Descend one level: each sub-dir is an example under ``topic``.
                for ex_dir in sorted(topic.iterdir()):
                    if not ex_dir.is_dir():
                        continue
                    if ex_dir.name.startswith(".") or ex_dir.name in _TOPIC_SKIP_DIRS:
                        continue
                    entries.append(
                        self._build_entry_for_topic_example(
                            ex_dir,
                            repo_root=repo_root,
                            repo=repo,
                            commit_sha=commit_sha,
                            topic_name=topic.name,
                        )
                    )
        return entries

    def _build_entry_for_topic_example(
        self,
        example_dir: Path,
        *,
        repo_root: Path,
        repo: str,
        commit_sha: str | None,
        topic_name: str,
    ) -> TaxonomyEntry:
        """Build a TaxonomyEntry for an example discovered under a topic dir.

        The ``path`` is ``str(example_dir.relative_to(repo_root))`` — this is
        the load-bearing invariant: ``github_ingest._build_taxonomy_lookup``
        keys entries by the same relative path that
        ``_discover_under_examples`` produces. ``foundational_class`` stays
        ``None`` for all topic-layout entries (deprecated legacy field).
        """
        dirname = example_dir.name
        example_id = f"example-{dirname}"
        rel_path = str(example_dir.relative_to(repo_root))

        tags: list[CapabilityTag] = []
        # Primary source: the topic dir name (with compound-topic overrides).
        tags.extend(_infer_tags_from_topic(topic_name))
        # Keep the existing dir-name heuristics as a secondary signal.
        tags.extend(_infer_tags_from_directory_name(dirname))

        readme_content: str | None = None
        summary = ""
        readme_path = example_dir / "README.md"
        if readme_path.is_file():
            readme_content = readme_path.read_text(encoding="utf-8", errors="replace")
            tags.extend(_infer_tags_from_readme(readme_content))
            summary = _extract_summary_from_readme(readme_content)

        # Scan Python files (including subdirectories) for code-level tags.
        for py_file in sorted(example_dir.rglob("*.py")):
            code = py_file.read_text(encoding="utf-8", errors="replace")
            tags.extend(_infer_tags_from_code(code))

        return TaxonomyEntry(
            example_id=example_id,
            repo=repo,
            path=rel_path,
            foundational_class=None,
            capabilities=_dedup_tags(tags),
            key_files=_find_key_files(example_dir),
            summary=summary,
            readme_content=readme_content,
            commit_sha=commit_sha,
            indexed_at=datetime.now(timezone.utc),
        )

    def _build_entry_for_example(
        self,
        example_dir: Path,
        *,
        repo: str,
        commit_sha: str | None,
        path_prefix: str = "",
    ) -> TaxonomyEntry:
        """Build a TaxonomyEntry for an example subdirectory.

        Args:
            example_dir: The example directory to scan.
            repo: GitHub repo slug.
            commit_sha: Optional commit SHA for provenance.
            path_prefix: Optional prefix for the entry path (e.g. ``"examples"``
                for dirs under ``examples/`` in mixed-layout repos).  When empty,
                the path is just the directory name.
        """
        dirname = example_dir.name
        example_id = f"example-{dirname}"
        rel_path = f"{path_prefix}/{dirname}" if path_prefix else dirname

        tags: list[CapabilityTag] = []
        tags.extend(_infer_tags_from_directory_name(dirname))

        readme_content: str | None = None
        summary = ""
        readme_path = example_dir / "README.md"
        if readme_path.is_file():
            readme_content = readme_path.read_text(encoding="utf-8", errors="replace")
            tags.extend(_infer_tags_from_readme(readme_content))
            summary = _extract_summary_from_readme(readme_content)

        # Scan Python files (including subdirectories) for code-level tags
        for py_file in sorted(example_dir.rglob("*.py")):
            code = py_file.read_text(encoding="utf-8", errors="replace")
            tags.extend(_infer_tags_from_code(code))

        return TaxonomyEntry(
            example_id=example_id,
            repo=repo,
            path=rel_path,
            foundational_class=None,
            capabilities=_dedup_tags(tags),
            key_files=_find_key_files(example_dir),
            summary=summary,
            readme_content=readme_content,
            commit_sha=commit_sha,
            indexed_at=datetime.now(timezone.utc),
        )
