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

    Operates on two kinds of repos:
    - **pipecat main** (contains ``examples/foundational/NN-name/``):
      each numbered subdirectory becomes a foundational-class entry.
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

        Args:
            root: Path to the ``examples/foundational/`` directory.
            repo: GitHub repo slug.
            commit_sha: Optional commit SHA for provenance.

        Returns:
            List of TaxonomyEntry objects, one per subdirectory.
        """
        entries: list[TaxonomyEntry] = []
        if not root.is_dir():
            return entries
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            entry = self._build_entry_for_foundational(child, repo=repo, commit_sha=commit_sha)
            entries.append(entry)
        self._entries.extend(entries)
        return entries

    def build_from_examples_repo(
        self,
        root: Path,
        *,
        repo: str = "pipecat-ai/pipecat-examples",
        commit_sha: str | None = None,
    ) -> list[TaxonomyEntry]:
        """Scan a pipecat-examples repo root and produce entries.

        Args:
            root: Path to the repo root directory.
            repo: GitHub repo slug.
            commit_sha: Optional commit SHA for provenance.

        Returns:
            List of TaxonomyEntry objects, one per subdirectory.
        """
        entries: list[TaxonomyEntry] = []
        if not root.is_dir():
            return entries
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            # Skip hidden dirs and common non-example dirs
            if child.name.startswith(".") or child.name in ("__pycache__", "node_modules"):
                continue
            entry = self._build_entry_for_example(child, repo=repo, commit_sha=commit_sha)
            entries.append(entry)
        self._entries.extend(entries)
        return entries

    def build_from_directory(
        self,
        root: Path,
        *,
        repo: str = "unknown",
        commit_sha: str | None = None,
    ) -> list[TaxonomyEntry]:
        """Generic scan: auto-detects foundational vs examples layout.

        Looks for ``examples/foundational/`` inside *root* — if present,
        treats it as a pipecat main repo.  Otherwise, scans top-level
        subdirectories as independent examples.

        Args:
            root: Path to the repo root or examples directory.
            repo: GitHub repo slug.
            commit_sha: Optional commit SHA for provenance.

        Returns:
            List of TaxonomyEntry objects.
        """
        foundational_dir = root / "examples" / "foundational"
        if foundational_dir.is_dir():
            return self.build_from_foundational(
                foundational_dir, repo=repo, commit_sha=commit_sha
            )
        return self.build_from_examples_repo(root, repo=repo, commit_sha=commit_sha)

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

    def _build_entry_for_example(
        self,
        example_dir: Path,
        *,
        repo: str,
        commit_sha: str | None,
    ) -> TaxonomyEntry:
        """Build a TaxonomyEntry for a pipecat-examples subdirectory."""
        dirname = example_dir.name
        example_id = f"example-{dirname}"

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
            path=dirname,
            foundational_class=None,
            capabilities=_dedup_tags(tags),
            key_files=_find_key_files(example_dir),
            summary=summary,
            readme_content=readme_content,
            commit_sha=commit_sha,
            indexed_at=datetime.now(timezone.utc),
        )
