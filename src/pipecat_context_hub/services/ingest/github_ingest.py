"""GitHub repository ingester for the Pipecat Context Hub.

Clones/fetches pipecat-ai repos, discovers example directories, chunks code
files, and produces ChunkedRecord objects via an IndexWriter.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from git import Repo as GitRepo
from git.exc import BadObject, GitCommandError

from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IngestResult, TaxonomyEntry

if TYPE_CHECKING:
    from pipecat_context_hub.shared.interfaces import IndexWriter

logger = logging.getLogger(__name__)

# File extensions we consider "code" for ingestion.
_CODE_EXTENSIONS: frozenset[str] = frozenset(
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
    }
)

# Directories to skip during traversal.
_SKIP_DIRS: frozenset[str] = frozenset(
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

# Max file size (bytes) we'll attempt to chunk.
_MAX_FILE_BYTES: int = 512_000  # 500 KB

# File extension → language name for metadata.
_EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
}


# ---------------------------------------------------------------------------
# Code chunker
# ---------------------------------------------------------------------------

# Patterns that mark a logical boundary in Python code.
_BOUNDARY_RE = re.compile(
    r"^(?:def |class |async def |@)",
    re.MULTILINE,
)

_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def repo_ref_is_tainted(repo_path: Path, commit_sha: str, tainted_refs: set[str]) -> bool:
    """Return True when *commit_sha* matches a tainted commit-ish or tag name.

    Hex refs are treated as commit SHA prefixes. Non-hex refs are matched
    against local tag names fetched for the repository.
    """
    if not tainted_refs:
        return False

    sha = commit_sha.lower()
    for ref in tainted_refs:
        normalized = ref.strip()
        if normalized and _HEX_SHA_RE.fullmatch(normalized) and sha.startswith(normalized.lower()):
            return True

    named_refs = [ref.strip() for ref in tainted_refs if ref.strip() and not _HEX_SHA_RE.fullmatch(ref.strip())]
    if not named_refs:
        return False

    try:
        git_repo = GitRepo(str(repo_path))
    except Exception:
        logger.warning("Failed to open repo for tainted-ref check: %s", repo_path)
        return False

    tag_targets: dict[str, str] = {}
    for tag in git_repo.tags:
        with suppress(AttributeError, BadObject, GitCommandError, ValueError):
            tag_targets[tag.name] = tag.commit.hexsha.lower()

    return any(tag_targets.get(ref) == sha for ref in named_refs)


def _resolve_origin_head_commit(git_repo: GitRepo) -> str:
    """Resolve the current commit for the remote default branch."""
    try:
        return git_repo.commit("origin/HEAD").hexsha
    except Exception:
        origin = git_repo.remotes.origin
        return origin.refs[0].commit.hexsha


def _chunk_code(
    source: str,
    *,
    max_tokens: int = 256,
    overlap_tokens: int = 25,
    prefer_boundaries: bool = True,
) -> list[str]:
    """Split source code into chunks respecting function/class boundaries.

    Falls back to line-based splitting when boundary-aware splitting isn't
    feasible.
    """
    if _estimate_tokens(source) <= max_tokens:
        return [source]

    lines = source.splitlines(keepends=True)

    if prefer_boundaries:
        chunks = _chunk_by_boundaries(lines, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
        if chunks:
            return chunks

    # Fallback: simple line-based chunking.
    return _chunk_by_lines(lines, max_tokens=max_tokens, overlap_tokens=overlap_tokens)


def _chunk_by_boundaries(
    lines: list[str],
    *,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Split at function/class boundaries.

    Returns empty list if no boundaries found (caller should fall back).
    """
    boundary_indices: list[int] = []
    for i, line in enumerate(lines):
        if _BOUNDARY_RE.match(line):
            boundary_indices.append(i)

    if not boundary_indices:
        return []

    # Build segments between boundaries.
    segments: list[list[str]] = []
    for idx, start in enumerate(boundary_indices):
        end = boundary_indices[idx + 1] if idx + 1 < len(boundary_indices) else len(lines)
        segments.append(lines[start:end])

    # Prepend any leading lines (imports, module docstring) to first segment.
    if boundary_indices[0] > 0:
        preamble = lines[: boundary_indices[0]]
        segments[0] = preamble + segments[0]

    # Merge small adjacent segments so we don't create tiny chunks.
    merged: list[str] = []
    buf: list[str] = []
    for seg in segments:
        candidate = buf + seg
        if _estimate_tokens("".join(candidate)) > max_tokens and buf:
            merged.append("".join(buf))
            buf = seg[:]
        else:
            buf = candidate
    if buf:
        merged.append("".join(buf))

    # Apply overlap: prepend last N tokens worth of the previous chunk.
    if overlap_tokens > 0 and len(merged) > 1:
        overlap_chars = overlap_tokens * 4
        result: list[str] = [merged[0]]
        for i in range(1, len(merged)):
            prev = merged[i - 1]
            overlap_text = prev[-overlap_chars:] if len(prev) > overlap_chars else prev
            result.append(overlap_text + merged[i])
        return result

    return merged


def _chunk_by_lines(
    lines: list[str],
    *,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Simple line-based chunking with overlap."""
    max_chars = max_tokens * 4
    overlap_chars = overlap_tokens * 4

    chunks: list[str] = []
    buf: list[str] = []
    buf_chars = 0

    for line in lines:
        line_chars = len(line)
        if buf_chars + line_chars > max_chars and buf:
            chunks.append("".join(buf))
            # Keep overlap from end of buffer.
            overlap_buf: list[str] = []
            overlap_count = 0
            for prev_line in reversed(buf):
                if overlap_count + len(prev_line) > overlap_chars:
                    break
                overlap_buf.insert(0, prev_line)
                overlap_count += len(prev_line)
            buf = overlap_buf + [line]
            buf_chars = overlap_count + line_chars
        else:
            buf.append(line)
            buf_chars += line_chars

    if buf:
        chunks.append("".join(buf))

    return chunks


def _make_chunk_id(repo: str, path: str, commit_sha: str, chunk_index: int) -> str:
    """Deterministic chunk ID derived from repo + path + commit + index."""
    key = f"{repo}:{path}:{commit_sha}:{chunk_index}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Example directory discovery
# ---------------------------------------------------------------------------


# Root-level directories to skip when scanning repos without an ``examples/`` dir.
_NON_EXAMPLE_ROOT_DIRS: frozenset[str] = frozenset(
    {
        *_SKIP_DIRS,
        "src",
        "lib",
        "docs",
        "doc",
        "tests",
        "test",
        "scripts",
        "tools",
        "ci",
        ".github",
        ".tox",
        ".nox",
        "assets",
        "static",
        "bin",
        "include",
        "man",
        "config",
        "deploy",
    }
)

# Top-level directories to skip when the repo root IS the single example.
# Only checked against the FIRST path component relative to the scan root —
# nested dirs with the same name (e.g. ``src/pkg/config/``) are NOT excluded.
# Keeps ``src/`` and ``lib/`` since those contain actual source code.
_ROOT_FALLBACK_SKIP_ROOT_DIRS: frozenset[str] = frozenset(
    {
        "docs",
        "doc",
        "tests",
        "test",
        "scripts",
        "tools",
        "ci",
        ".github",
        ".tox",
        ".nox",
        "assets",
        "static",
        "bin",
        "include",
        "man",
        "config",
        "deploy",
    }
)


def _find_example_dirs(repo_root: Path) -> list[Path]:
    """Discover example directories within a cloned repo.

    Handles two repo layouts:
    - **examples/ dir present** (e.g. ``pipecat-ai/pipecat``): returns each
      immediate subdirectory of ``examples/`` (or sub-subdirectory for the
      ``examples/foundational/`` category pattern). When a category dir
      contains code files directly (flat file layout), it's returned as-is.
    - **No examples/ dir** (e.g. ``pipecat-ai/pipecat-examples``): falls back
      to scanning root-level directories that contain code files.
    """
    examples_dir = repo_root / "examples"
    if examples_dir.is_dir():
        return _discover_under_examples(examples_dir)

    # Fall back: root-level directories (pipecat-examples pattern).
    return _discover_root_level_examples(repo_root)


def _discover_under_examples(examples_dir: Path) -> list[Path]:
    """Discover example dirs under an ``examples/`` directory.

    Handles three layouts:
    1. Subdirectories with code files (e.g. ``examples/my-bot/bot.py``)
    2. Category dirs (e.g. ``examples/foundational/07-interruptible/``)
    3. Flat code files directly in ``examples/`` (e.g. ``examples/single_agent.py``)
       — returns ``examples/`` itself so the files are indexed.
    """
    result: list[Path] = []
    has_flat_code = False

    for child in sorted(examples_dir.iterdir()):
        if child.is_file() and child.suffix in _CODE_EXTENSIONS:
            has_flat_code = True
            continue
        if child.name in _SKIP_DIRS or not child.is_dir():
            continue
        # Check if this dir directly contains code files.
        sub_has_code = any(f.suffix in _CODE_EXTENSIONS for f in child.iterdir() if f.is_file())
        if sub_has_code:
            result.append(child)
        else:
            # Category dir like ``examples/foundational/``: descend one level.
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and grandchild.name not in _SKIP_DIRS:
                    result.append(grandchild)

    # Flat code files directly in examples/ — treat the dir itself as an example.
    if has_flat_code:
        result.append(examples_dir)

    return result


def _discover_root_level_examples(repo_root: Path) -> list[Path]:
    """Discover example dirs at the repo root (no ``examples/`` dir).

    When no qualifying subdirectories are found (e.g. single-project repos
    where all code lives under ``src/``), falls back to returning the repo
    root itself so that ``_iter_code_files`` can recurse into it.
    """
    result: list[Path] = []
    for child in sorted(repo_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name in _NON_EXAMPLE_ROOT_DIRS:
            continue
        # Only include dirs that directly contain code files.
        has_code = any(f.suffix in _CODE_EXTENSIONS for f in child.iterdir() if f.is_file())
        if has_code:
            result.append(child)

    # Fallback: treat the whole repo as a single example when no qualifying
    # subdirectories are found (e.g. src/-layout packages).
    if not result:
        result.append(repo_root)

    return result


def _iter_code_files(
    directory: Path,
    *,
    skip_root_dirs: frozenset[str] = frozenset(),
) -> list[Path]:
    """Return all code files under *directory*, respecting skip/size rules.

    Args:
        directory: Root directory to scan recursively.
        skip_root_dirs: Extra directory names to exclude, checked only against
            the **first** path component relative to *directory*.  This avoids
            excluding nested modules that share a name with a top-level
            non-source directory (e.g. ``src/pkg/config/`` is kept even when
            top-level ``config/`` is excluded).  ``_SKIP_DIRS`` is always
            checked at all depths.
    """
    files: list[Path] = []
    for p in sorted(directory.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if skip_root_dirs:
            first_component = p.relative_to(directory).parts[0]
            if first_component in skip_root_dirs:
                continue
        if p.suffix not in _CODE_EXTENSIONS:
            continue
        if p.stat().st_size > _MAX_FILE_BYTES:
            continue
        files.append(p)
    return files


def _iter_root_level_code_files(directory: Path) -> list[Path]:
    """Return code files directly in *directory* (non-recursive).

    Captures entry-point files (e.g. ``sidekick.py``, config YAMLs) that
    sit at the repo root alongside subdirectory examples.
    """
    files: list[Path] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        if p.suffix not in _CODE_EXTENSIONS:
            continue
        if p.stat().st_size > _MAX_FILE_BYTES:
            continue
        files.append(p)
    return files


# ---------------------------------------------------------------------------
# Taxonomy + metadata enrichment
# ---------------------------------------------------------------------------


def _build_taxonomy_lookup(
    repo_path: Path,
    repo_slug: str,
    commit_sha: str,
) -> dict[str, TaxonomyEntry]:
    """Run TaxonomyBuilder on a cloned repo and return a path→entry lookup.

    Keys are relative directory paths (e.g. ``examples/foundational/07-interruptible``).
    """
    from pipecat_context_hub.services.ingest.taxonomy import TaxonomyBuilder

    builder = TaxonomyBuilder()
    entries = builder.build_from_directory(repo_path, repo=repo_slug, commit_sha=commit_sha)
    lookup: dict[str, TaxonomyEntry] = {}
    for entry in entries:
        lookup[entry.path] = entry
    logger.debug(
        "Taxonomy built for %s: %d entries",
        repo_slug,
        len(lookup),
    )
    return lookup


# Tags whose presence implies the example requires a cloud service.
_CLOUD_TAGS: frozenset[str] = frozenset(
    {
        "daily",
        "twilio",
        "vonage",
        "livekit",
        "azure",
        "aws",
        "google",
    }
)


def _infer_execution_mode(capability_tags: list[str]) -> str:
    """Infer execution_mode from capability tags.

    If any tag implies a hosted transport or cloud service, the example
    is classified as ``"cloud"``; otherwise ``"local"``.
    """
    for tag in capability_tags:
        if tag in _CLOUD_TAGS:
            return "cloud"
    return "local"


def _infer_domain(rel_path: str, language: str | None) -> str:
    """Infer a domain tag from file path and language.

    Categories:
    - ``backend`` — Python files (bot code, pipeline logic, server code)
    - ``frontend`` — JavaScript/TypeScript files in client-like paths
    - ``config`` — YAML, TOML, JSON config files, docker-compose
    - ``infra`` — CI/deploy YAML files in .github/, ci/, or deploy/ directories

    Note: only files with extensions in ``_CODE_EXTENSIONS`` reach this
    function.  Dockerfiles, Makefiles, etc. are not ingested and cannot
    be classified here.
    """
    path_lower = rel_path.lower()
    name = path_lower.rsplit("/", 1)[-1] if "/" in path_lower else path_lower

    # Infra: CI/deploy configs in .github/, ci/, or deploy/ directories
    if "ci/" in path_lower or ".github/" in path_lower or "deploy/" in path_lower:
        return "infra"

    # Config files by name or extension
    if language in ("yaml", "toml", "json") or name in (
        "docker-compose.yml", "docker-compose.yaml",
        "pcc-deploy.toml", ".env.example", "config.yaml",
        "config.example.yaml", "requirements.txt", "pyproject.toml",
        "package.json", "tsconfig.json",
    ):
        return "config"

    # Frontend: JS/TS files, especially in client-like directories
    if language in ("javascript", "typescript"):
        return "frontend"

    # Backend: Python files (default for pipeline/bot code)
    if language == "python":
        return "backend"

    # Fallback: anything not matched above defaults to backend
    return "backend"


def _build_chunk_metadata(
    *,
    repo_slug: str,
    commit_sha: str,
    chunk_index: int,
    language: str | None,
    line_start: int,
    line_end: int,
    rel_path: str = "",
    taxonomy_entry: TaxonomyEntry | None,
) -> dict[str, object]:
    """Build enriched metadata dict for a ChunkedRecord.

    Merges basic provenance fields with taxonomy-derived fields
    (foundational_class, capability_tags, key_files, execution_mode)
    and inferred domain tag (backend/frontend/config/infra).
    """
    meta: dict[str, object] = {
        "repo": repo_slug,
        "commit_sha": commit_sha,
        "chunk_index": chunk_index,
        "line_start": line_start,
        "line_end": line_end,
    }
    if language is not None:
        meta["language"] = language
    meta["domain"] = _infer_domain(rel_path, language)

    if taxonomy_entry is not None:
        if taxonomy_entry.foundational_class is not None:
            meta["foundational_class"] = taxonomy_entry.foundational_class
        cap_tag_names = [t.name for t in taxonomy_entry.capabilities]
        if cap_tag_names:
            meta["capability_tags"] = cap_tag_names
        if taxonomy_entry.key_files:
            meta["key_files"] = taxonomy_entry.key_files
        if taxonomy_entry.readme_content is not None:
            meta["readme_content"] = taxonomy_entry.readme_content[:65536]
        meta["execution_mode"] = _infer_execution_mode(cap_tag_names)

    return meta


def _compute_chunk_line_ranges(
    source: str,
    chunks: list[str],
) -> list[tuple[int, int]]:
    """Compute approximate (line_start, line_end) 1-indexed for each chunk.

    Uses sequential line counting. With overlap, ranges may slightly
    overlap between adjacent chunks — acceptable for v0.
    """
    if not chunks:
        return []

    total_lines = len(source.splitlines())
    ranges: list[tuple[int, int]] = []
    line_cursor = 1

    for chunk in chunks:
        num_lines = len(chunk.splitlines()) if chunk else 1
        line_start = max(1, line_cursor)
        line_end = min(line_start + num_lines - 1, total_lines)
        ranges.append((line_start, line_end))
        # Advance cursor to next unique content start.
        # With overlap, some lines are shared with the next chunk,
        # but for line tracking we simply advance past this chunk.
        line_cursor = line_end + 1

    return ranges


# ---------------------------------------------------------------------------
# GitHubRepoIngester
# ---------------------------------------------------------------------------


class GitHubRepoIngester:
    """Ingests pipecat-ai GitHub repos into the index.

    Implements the ``Ingester`` protocol.
    """

    def __init__(self, config: HubConfig, writer: IndexWriter) -> None:
        self._config = config
        self._writer = writer
        self._repos_dir = config.storage.data_dir / "repos"

    # -- Ingester protocol ---------------------------------------------------

    async def ingest(
        self,
        repos: list[str] | None = None,
        prefetched: dict[str, tuple[Path, str]] | None = None,
    ) -> IngestResult:
        """Clone (or fetch) repos and ingest their examples.

        Args:
            repos: Specific repo slugs to ingest. If None, uses all
                configured repos from ``effective_repos``.
            prefetched: Optional mapping of ``{repo_slug: (repo_path, commit_sha)}``
                from prior ``clone_or_fetch`` calls, avoiding redundant fetches.
        """
        start = time.monotonic()
        all_errors: list[str] = []
        total_upserted = 0
        prefetched = prefetched or {}

        target_repos = repos if repos is not None else self._config.sources.effective_repos
        for repo_slug in target_repos:
            result = await self._ingest_repo(repo_slug, prefetched=prefetched.get(repo_slug))
            total_upserted += result.records_upserted
            all_errors.extend(result.errors)

        return IngestResult(
            source="github",
            records_upserted=total_upserted,
            errors=all_errors,
            duration_seconds=round(time.monotonic() - start, 3),
        )

    # -- Internal helpers ----------------------------------------------------

    async def _ingest_repo(
        self,
        repo_slug: str,
        prefetched: tuple[Path, str] | None = None,
    ) -> IngestResult:
        """Clone/fetch a single repo and ingest its example directories.

        Args:
            repo_slug: GitHub repo slug (e.g. ``pipecat-ai/pipecat``).
            prefetched: Optional ``(repo_path, commit_sha)`` from a prior
                ``clone_or_fetch`` call, avoiding a redundant fetch.
        """
        errors: list[str] = []
        records: list[ChunkedRecord] = []

        if prefetched is not None:
            repo_path, commit_sha = prefetched
        else:
            try:
                repo_path, commit_sha = await asyncio.to_thread(self.clone_or_fetch, repo_slug)
            except Exception as exc:
                msg = f"Failed to clone/fetch {repo_slug}: {exc}"
                logger.error(msg)
                return IngestResult(source=repo_slug, errors=[msg])

        # Build taxonomy for this repo to enrich chunk metadata.
        taxonomy_lookup = _build_taxonomy_lookup(repo_path, repo_slug, commit_sha)

        example_dirs = _find_example_dirs(repo_path)
        if not example_dirs:
            logger.warning("No example directories found in %s", repo_slug)

        # When the root fallback is active (repo_path IS an example dir),
        # the taxonomy lookup key is "." but build_from_directory never
        # produces that key.  Synthesize it so chunks get full enrichment
        # (execution_mode, capability_tags, key_files).
        if repo_path in example_dirs and "." not in taxonomy_lookup:
            from pipecat_context_hub.services.ingest.taxonomy import TaxonomyBuilder

            root_builder = TaxonomyBuilder()
            taxonomy_lookup["."] = root_builder.build_entry_for_repo_root(
                repo_path,
                repo=repo_slug,
                commit_sha=commit_sha,
            )

        now = datetime.now(tz=timezone.utc)
        chunking = self._config.chunking

        is_root_fallback = repo_path in example_dirs

        for ex_dir in example_dirs:
            # Look up taxonomy entry at the directory level.
            rel_ex_dir = str(ex_dir.relative_to(repo_path))
            dir_taxonomy_entry = taxonomy_lookup.get(rel_ex_dir)
            if dir_taxonomy_entry is None:
                logger.warning(
                    "No taxonomy entry for example dir %s in %s — "
                    "chunks will lack capability_tags, execution_mode, key_files",
                    rel_ex_dir,
                    repo_slug,
                )

            # When the repo root IS the example, skip top-level non-source
            # dirs (tests/, docs/, .github/, …) to avoid polluting example
            # search.  Only the first path component is checked so nested
            # modules like src/pkg/config/ are still indexed.
            #
            # When examples/ itself is in the list (flat-file layout),
            # only index direct children to avoid re-processing subdirectory
            # examples that are already handled as separate entries.
            examples_subdir = repo_path / "examples"
            if ex_dir == examples_subdir:
                code_files = _iter_root_level_code_files(ex_dir)
            elif is_root_fallback and ex_dir == repo_path:
                code_files = _iter_code_files(ex_dir, skip_root_dirs=_ROOT_FALLBACK_SKIP_ROOT_DIRS)
            else:
                code_files = _iter_code_files(ex_dir)
            for code_file in code_files:
                # Skip symlinks to prevent reading files outside the repo
                if code_file.is_symlink():
                    continue
                try:
                    # Verify resolved path stays within the repo root
                    code_file.resolve().relative_to(repo_path.resolve())
                    content = code_file.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    errors.append(f"Error reading {code_file.relative_to(repo_path)}: {exc}")
                    continue

                rel_path = str(code_file.relative_to(repo_path))
                # Try per-file taxonomy lookup first (flat files like
                # examples/foundational/01-say-one-thing.py), then
                # fall back to directory-level lookup (subdirectory examples).
                taxonomy_entry = taxonomy_lookup.get(rel_path) or dir_taxonomy_entry

                source_url = f"https://github.com/{repo_slug}/blob/{commit_sha}/{rel_path}"
                language = _EXTENSION_TO_LANGUAGE.get(code_file.suffix)

                chunks = _chunk_code(
                    content,
                    max_tokens=chunking.code_max_tokens,
                    overlap_tokens=chunking.code_overlap_tokens,
                    prefer_boundaries=chunking.code_prefer_function_boundaries,
                )
                line_ranges = _compute_chunk_line_ranges(content, chunks)

                for idx, chunk_text in enumerate(chunks):
                    chunk_id = _make_chunk_id(repo_slug, rel_path, commit_sha, idx)
                    line_start, line_end = line_ranges[idx]

                    meta = _build_chunk_metadata(
                        repo_slug=repo_slug,
                        commit_sha=commit_sha,
                        chunk_index=idx,
                        language=language,
                        line_start=line_start,
                        line_end=line_end,
                        rel_path=rel_path,
                        taxonomy_entry=taxonomy_entry,
                    )

                    records.append(
                        ChunkedRecord(
                            chunk_id=chunk_id,
                            content=chunk_text,
                            content_type="code",
                            source_url=source_url,
                            repo=repo_slug,
                            path=rel_path,
                            commit_sha=commit_sha,
                            indexed_at=now,
                            metadata=meta,
                        )
                    )

        # For Layout B repos (no examples/ dir) where subdirectory examples
        # were found, also capture root-level code files (e.g. entry-point
        # scripts like sidekick.py) that would otherwise be missed.
        examples_dir = repo_path / "examples"
        is_layout_b = not examples_dir.is_dir()
        has_subdir_examples = any(d != repo_path for d in example_dirs)
        if is_layout_b and has_subdir_examples:
            # Ensure a repo-root taxonomy entry exists so root-level files
            # inherit execution_mode / capability_tags (file-level keys like
            # "sidekick.py" almost never appear in the taxonomy).
            if "." not in taxonomy_lookup:
                from pipecat_context_hub.services.ingest.taxonomy import TaxonomyBuilder

                root_builder = TaxonomyBuilder()
                taxonomy_lookup["."] = root_builder.build_entry_for_repo_root(
                    repo_path,
                    repo=repo_slug,
                    commit_sha=commit_sha,
                )
            root_taxonomy = taxonomy_lookup.get(".")

            root_files = _iter_root_level_code_files(repo_path)
            for code_file in root_files:
                if code_file.is_symlink():
                    continue
                try:
                    # Verify resolved path stays within the repo root
                    code_file.resolve().relative_to(repo_path.resolve())
                    content = code_file.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    errors.append(f"Error reading {code_file.relative_to(repo_path)}: {exc}")
                    continue

                rel_path = str(code_file.relative_to(repo_path))
                taxonomy_entry = taxonomy_lookup.get(rel_path) or root_taxonomy
                source_url = f"https://github.com/{repo_slug}/blob/{commit_sha}/{rel_path}"
                language = _EXTENSION_TO_LANGUAGE.get(code_file.suffix)
                chunks = _chunk_code(
                    content,
                    max_tokens=chunking.code_max_tokens,
                    overlap_tokens=chunking.code_overlap_tokens,
                    prefer_boundaries=chunking.code_prefer_function_boundaries,
                )
                line_ranges = _compute_chunk_line_ranges(content, chunks)

                for idx, chunk_text in enumerate(chunks):
                    chunk_id = _make_chunk_id(repo_slug, rel_path, commit_sha, idx)
                    line_start, line_end = line_ranges[idx]
                    meta = _build_chunk_metadata(
                        repo_slug=repo_slug,
                        commit_sha=commit_sha,
                        chunk_index=idx,
                        language=language,
                        line_start=line_start,
                        line_end=line_end,
                        rel_path=rel_path,
                        taxonomy_entry=taxonomy_entry,
                    )
                    records.append(
                        ChunkedRecord(
                            chunk_id=chunk_id,
                            content=chunk_text,
                            content_type="code",
                            source_url=source_url,
                            repo=repo_slug,
                            path=rel_path,
                            commit_sha=commit_sha,
                            indexed_at=now,
                            metadata=meta,
                        )
                    )

        upserted = 0
        if records:
            try:
                upserted = await self._writer.upsert(records)
            except Exception as exc:
                errors.append(f"IndexWriter.upsert failed for {repo_slug}: {exc}")

        return IngestResult(
            source=repo_slug,
            records_upserted=upserted,
            errors=errors,
        )

    def clone_or_fetch(self, repo_slug: str, checkout: bool = True) -> tuple[Path, str]:
        """Clone repo if not present, otherwise fetch latest.

        Returns (repo_path, HEAD commit SHA).
        """
        # Sanitize slug to prevent path traversal
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", repo_slug)
        repo_path = self._repos_dir / safe_name
        # Verify resolved path stays under repos dir
        self._repos_dir.mkdir(parents=True, exist_ok=True)
        repo_path.resolve().relative_to(self._repos_dir.resolve())

        if (repo_path / ".git").is_dir():
            git_repo = GitRepo(str(repo_path))
            git_repo.git.fetch("origin", "--tags")
            commit_sha = _resolve_origin_head_commit(git_repo)
            if checkout:
                self.checkout_commit(repo_path, commit_sha)
        else:
            clone_url = f"https://github.com/{repo_slug}.git"
            git_repo = GitRepo.clone_from(
                clone_url,
                str(repo_path),
                no_checkout=not checkout,
            )
            commit_sha = git_repo.head.commit.hexsha

        return repo_path, commit_sha

    def checkout_commit(self, repo_path: Path, commit_sha: str) -> None:
        """Reset the local working tree to a specific commit."""
        git_repo = GitRepo(str(repo_path))
        git_repo.head.reset(git_repo.commit(commit_sha), index=True, working_tree=True)
