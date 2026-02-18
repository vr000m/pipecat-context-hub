"""GitHub repository ingester for the Pipecat Context Hub.

Clones/fetches pipecat-ai repos, discovers example directories, chunks code
files, and produces ChunkedRecord objects via an IndexWriter.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from git import Repo as GitRepo

from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IngestResult, TaxonomyEntry

if TYPE_CHECKING:
    from pipecat_context_hub.shared.interfaces import IndexWriter

logger = logging.getLogger(__name__)

# File extensions we consider "code" for ingestion.
_CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml",
})

# Directories to skip during traversal.
_SKIP_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".git", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".egg-info",
})

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


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


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


def _find_example_dirs(repo_root: Path) -> list[Path]:
    """Discover example directories within a cloned repo.

    Looks for an ``examples/`` top-level directory and returns each
    immediate subdirectory (or sub-subdirectory for pipecat's
    ``examples/foundational/`` pattern).
    """
    examples_dir = repo_root / "examples"
    if not examples_dir.is_dir():
        return []

    result: list[Path] = []
    for child in sorted(examples_dir.iterdir()):
        if child.name in _SKIP_DIRS or not child.is_dir():
            continue
        # Check if this is a category dir (contains further subdirs with code).
        sub_has_code = any(
            f.suffix in _CODE_EXTENSIONS for f in child.iterdir() if f.is_file()
        )
        if sub_has_code:
            result.append(child)
        else:
            # Category dir like ``examples/foundational/``: descend one level.
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and grandchild.name not in _SKIP_DIRS:
                    result.append(grandchild)
    return result


def _iter_code_files(directory: Path) -> list[Path]:
    """Return all code files under *directory*, respecting skip/size rules."""
    files: list[Path] = []
    for p in sorted(directory.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
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


def _build_chunk_metadata(
    *,
    repo_slug: str,
    commit_sha: str,
    chunk_index: int,
    language: str | None,
    line_start: int,
    line_end: int,
    taxonomy_entry: TaxonomyEntry | None,
) -> dict[str, object]:
    """Build enriched metadata dict for a ChunkedRecord.

    Merges basic provenance fields with taxonomy-derived fields
    (foundational_class, capability_tags, key_files).
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

    if taxonomy_entry is not None:
        if taxonomy_entry.foundational_class is not None:
            meta["foundational_class"] = taxonomy_entry.foundational_class
        cap_tag_names = [t.name for t in taxonomy_entry.capabilities]
        if cap_tag_names:
            meta["capability_tags"] = cap_tag_names
        if taxonomy_entry.key_files:
            meta["key_files"] = taxonomy_entry.key_files

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

    async def ingest(self) -> IngestResult:
        """Clone (or fetch) all configured repos and ingest their examples."""
        start = time.monotonic()
        all_errors: list[str] = []
        total_upserted = 0

        for repo_slug in self._config.sources.repos:
            result = await self._ingest_repo(repo_slug)
            total_upserted += result.records_upserted
            all_errors.extend(result.errors)

        return IngestResult(
            source="github",
            records_upserted=total_upserted,
            errors=all_errors,
            duration_seconds=round(time.monotonic() - start, 3),
        )

    async def refresh(self) -> IngestResult:
        """Incremental refresh (same as ingest in v0)."""
        return await self.ingest()

    # -- Internal helpers ----------------------------------------------------

    async def _ingest_repo(self, repo_slug: str) -> IngestResult:
        """Clone/fetch a single repo and ingest its example directories."""
        errors: list[str] = []
        records: list[ChunkedRecord] = []

        try:
            repo_path, commit_sha = await asyncio.to_thread(
                self._clone_or_fetch, repo_slug
            )
        except Exception as exc:
            msg = f"Failed to clone/fetch {repo_slug}: {exc}"
            logger.error(msg)
            return IngestResult(source=repo_slug, errors=[msg])

        # Build taxonomy for this repo to enrich chunk metadata.
        taxonomy_lookup = _build_taxonomy_lookup(repo_path, repo_slug, commit_sha)

        example_dirs = _find_example_dirs(repo_path)
        if not example_dirs:
            logger.warning("No example directories found in %s", repo_slug)

        now = datetime.now(tz=timezone.utc)
        chunking = self._config.chunking

        for ex_dir in example_dirs:
            # Look up taxonomy entry for this example directory.
            rel_ex_dir = str(ex_dir.relative_to(repo_path))
            taxonomy_entry = taxonomy_lookup.get(rel_ex_dir)

            code_files = _iter_code_files(ex_dir)
            for code_file in code_files:
                try:
                    content = code_file.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    errors.append(f"Error reading {code_file}: {exc}")
                    continue

                rel_path = str(code_file.relative_to(repo_path))
                source_url = (
                    f"https://github.com/{repo_slug}/blob/{commit_sha}/{rel_path}"
                )
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

    def _clone_or_fetch(self, repo_slug: str) -> tuple[Path, str]:
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
            origin = git_repo.remotes.origin
            origin.fetch()
            # Update working tree to match fetched remote HEAD
            remote_ref = origin.refs[0]
            git_repo.head.reset(remote_ref.commit, index=True, working_tree=True)
        else:
            repo_path.mkdir(parents=True, exist_ok=True)
            clone_url = f"https://github.com/{repo_slug}.git"
            git_repo = GitRepo.clone_from(clone_url, str(repo_path))

        commit_sha = git_repo.head.commit.hexsha
        return repo_path, commit_sha
