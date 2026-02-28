"""Pipecat framework source ingester using Python AST extraction.

Walks the pipecat framework source tree (from the GitHubRepoIngester's
clone), extracts API metadata via AST, and produces ChunkedRecord objects
with content_type="source".
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pipecat_context_hub.services.ingest.ast_extractor import (
    ClassInfo,
    FunctionInfo,
    MethodInfo,
    ModuleInfo,
    build_signature,
    extract_module_info,
)
from pipecat_context_hub.shared.types import ChunkedRecord, IngestResult

if TYPE_CHECKING:
    from pipecat_context_hub.shared.config import HubConfig
    from pipecat_context_hub.shared.interfaces import IndexWriter

logger = logging.getLogger(__name__)

# Directories to skip when walking the pipecat source tree.
_SKIP_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".git", "tests", "test", ".mypy_cache",
    ".pytest_cache", ".ruff_cache",
})

# Minimum body lines for a method/function to get its own chunk.
_MIN_METHOD_LINES = 3

_REPO_SLUG = "pipecat-ai/pipecat"


class SourceIngester:
    """Ingests pipecat framework source as API reference chunks."""

    def __init__(self, config: HubConfig, writer: IndexWriter) -> None:
        self._repos_dir = config.storage.data_dir / "repos"
        self._writer = writer

    async def ingest(self) -> IngestResult:
        """Extract API metadata from pipecat source and index it."""
        start = time.monotonic()
        errors: list[str] = []
        records: list[ChunkedRecord] = []

        # 1. Locate the pipecat clone
        clone_dir = self._repos_dir / "pipecat-ai_pipecat"
        src_dir = clone_dir / "src" / "pipecat"
        if not src_dir.is_dir():
            msg = f"Pipecat source not found at {src_dir}"
            logger.error(msg)
            return IngestResult(source="pipecat-source", errors=[msg])

        # 2. Get commit SHA
        commit_sha = _get_commit_sha(clone_dir)

        # 3. Walk src/pipecat/ recursively
        py_files = _find_python_files(src_dir)
        logger.info("Found %d Python files in %s", len(py_files), src_dir)

        now = datetime.now(tz=timezone.utc)

        for py_file in py_files:
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                errors.append(f"Error reading {py_file}: {exc}")
                continue

            rel_path = py_file.relative_to(clone_dir / "src").as_posix()
            module_path = rel_path.replace("/", ".").removesuffix(".py")
            # Handle __init__.py: module path is the parent package
            if module_path.endswith(".__init__"):
                module_path = module_path.removesuffix(".__init__")

            try:
                module_info = extract_module_info(source, module_path)
            except SyntaxError as exc:
                errors.append(f"SyntaxError in {rel_path}: {exc}")
                continue
            except Exception as exc:
                errors.append(f"AST error in {rel_path}: {exc}")
                continue

            file_records = _build_chunks(
                module_info=module_info,
                source=source,
                rel_path=rel_path,
                commit_sha=commit_sha,
                now=now,
            )
            records.extend(file_records)

        # 4. Batch upsert
        upserted = 0
        if records:
            try:
                upserted = await self._writer.upsert(records)
            except Exception as exc:
                errors.append(f"IndexWriter.upsert failed: {exc}")

        duration = round(time.monotonic() - start, 3)
        logger.info(
            "Source ingest: files=%d chunks=%d upserted=%d errors=%d duration=%.1fs",
            len(py_files), len(records), upserted, len(errors), duration,
        )
        return IngestResult(
            source="pipecat-source",
            records_upserted=upserted,
            errors=errors,
            duration_seconds=duration,
        )


def _get_commit_sha(clone_dir: Path) -> str:
    """Get HEAD commit SHA from git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(clone_dir),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _find_python_files(src_dir: Path) -> list[Path]:
    """Find all .py files under src_dir, skipping test dirs."""
    files: list[Path] = []
    for p in sorted(src_dir.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.relative_to(src_dir).parts):
            continue
        files.append(p)
    return files


def _make_chunk_id(
    module_path: str, chunk_type: str, class_name: str, method_name: str,
    commit_sha: str, line_start: int = 0,
) -> str:
    """Deterministic chunk ID."""
    key = f"source:{module_path}:{chunk_type}:{class_name}:{method_name}:{commit_sha}:{line_start}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _make_source_url(rel_path: str, commit_sha: str, line_start: int, line_end: int) -> str:
    """Build GitHub source URL with line range."""
    base = f"https://github.com/{_REPO_SLUG}/blob/{commit_sha}/src/{rel_path}"
    if line_start and line_end:
        return f"{base}#L{line_start}-L{line_end}"
    return base


def _build_chunks(
    *,
    module_info: ModuleInfo,
    source: str,
    rel_path: str,
    commit_sha: str,
    now: datetime,
) -> list[ChunkedRecord]:
    """Build ChunkedRecord list from extracted module info."""
    records: list[ChunkedRecord] = []
    mp = module_info.module_path

    # --- Module overview chunk ---
    module_content = _build_module_overview(module_info)
    records.append(ChunkedRecord(
        chunk_id=_make_chunk_id(mp, "module_overview", "", "", commit_sha, line_start=1),
        content=module_content,
        content_type="source",
        source_url=_make_source_url(rel_path, commit_sha, 1, len(source.splitlines())),
        repo=_REPO_SLUG,
        path=rel_path,
        commit_sha=commit_sha,
        indexed_at=now,
        metadata={
            "module_path": mp,
            "chunk_type": "module_overview",
            "class_name": "",
            "method_name": "",
            "base_classes": [],
            "method_signature": "",
            "is_dataclass": False,
            "is_abstract": False,
            "language": "python",
            "line_start": 1,
            "line_end": len(source.splitlines()),
            "imports": [i for i in module_info.imports if "pipecat" in i],
        },
    ))

    # --- Class chunks ---
    for cls in module_info.classes:
        # Class overview
        class_content = _build_class_overview(cls, mp)
        records.append(ChunkedRecord(
            chunk_id=_make_chunk_id(mp, "class_overview", cls.name, "", commit_sha, line_start=cls.line_start),
            content=class_content,
            content_type="source",
            source_url=_make_source_url(rel_path, commit_sha, cls.line_start, cls.line_end),
            repo=_REPO_SLUG,
            path=rel_path,
            commit_sha=commit_sha,
            indexed_at=now,
            metadata={
                "module_path": mp,
                "chunk_type": "class_overview",
                "class_name": cls.name,
                "method_name": "",
                "base_classes": cls.base_classes,
                "method_signature": "",
                "is_dataclass": cls.is_dataclass,
                "is_abstract": any(m.is_abstract for m in cls.methods),
                "language": "python",
                "line_start": cls.line_start,
                "line_end": cls.line_end,
            },
        ))

        # Method chunks (only for non-trivial methods)
        for method in cls.methods:
            body_lines = method.line_end - method.line_start + 1
            if body_lines < _MIN_METHOD_LINES:
                continue
            method_content = _build_method_chunk(cls, method, mp)
            sig = build_signature(method.name, method.parameters, method.return_type)
            records.append(ChunkedRecord(
                chunk_id=_make_chunk_id(mp, "method", cls.name, method.name, commit_sha, line_start=method.line_start),
                content=method_content,
                content_type="source",
                source_url=_make_source_url(
                    rel_path, commit_sha, method.line_start, method.line_end
                ),
                repo=_REPO_SLUG,
                path=rel_path,
                commit_sha=commit_sha,
                indexed_at=now,
                metadata={
                    "module_path": mp,
                    "chunk_type": "method",
                    "class_name": cls.name,
                    "method_name": method.name,
                    "base_classes": cls.base_classes,
                    "method_signature": sig,
                    "return_type": method.return_type or "",
                    "is_dataclass": cls.is_dataclass,
                    "is_abstract": method.is_abstract,
                    "language": "python",
                    "line_start": method.line_start,
                    "line_end": method.line_end,
                },
            ))

    # --- Top-level function chunks ---
    for func in module_info.functions:
        body_lines = func.line_end - func.line_start + 1
        if body_lines < _MIN_METHOD_LINES:
            continue
        func_content = _build_function_chunk(func, mp)
        sig = build_signature(func.name, func.parameters, func.return_type)
        records.append(ChunkedRecord(
            chunk_id=_make_chunk_id(mp, "function", "", func.name, commit_sha, line_start=func.line_start),
            content=func_content,
            content_type="source",
            source_url=_make_source_url(rel_path, commit_sha, func.line_start, func.line_end),
            repo=_REPO_SLUG,
            path=rel_path,
            commit_sha=commit_sha,
            indexed_at=now,
            metadata={
                "module_path": mp,
                "chunk_type": "function",
                "class_name": "",
                "method_name": func.name,
                "base_classes": [],
                "method_signature": sig,
                "return_type": func.return_type or "",
                "is_dataclass": False,
                "is_abstract": False,
                "language": "python",
                "line_start": func.line_start,
                "line_end": func.line_end,
            },
        ))

    return records


def _build_module_overview(info: ModuleInfo) -> str:
    """Build module overview content."""
    parts: list[str] = [f"# Module: {info.module_path}"]
    if info.docstring:
        parts.append(f"\n{info.docstring}")
    if info.classes:
        parts.append("\n## Classes")
        for cls in info.classes:
            bases = f"({', '.join(cls.base_classes)})" if cls.base_classes else ""
            marker = " [dataclass]" if cls.is_dataclass else ""
            parts.append(f"- {cls.name}{bases}{marker}")
    if info.functions:
        parts.append("\n## Functions")
        for func in info.functions:
            sig = build_signature(func.name, func.parameters, func.return_type)
            parts.append(f"- def {func.name}{sig}")
    return "\n".join(parts)


def _build_class_overview(cls: ClassInfo, module_path: str) -> str:
    """Build class overview content."""
    parts: list[str] = [f"# Class: {cls.name}"]
    parts.append(f"Module: {module_path}")
    if cls.base_classes:
        parts.append(f"Bases: {', '.join(cls.base_classes)}")
    if cls.is_dataclass:
        parts.append("Type: dataclass")
    if cls.docstring:
        parts.append(f"\n{cls.docstring}")

    # Constructor
    init_method = next((m for m in cls.methods if m.name == "__init__"), None)
    if init_method:
        sig = build_signature("__init__", init_method.parameters, init_method.return_type)
        parts.append(f"\n## Constructor\n```python\ndef __init__{sig}\n```")  # sig is (params) -> ret
        if init_method.docstring:
            parts.append(init_method.docstring)

    # Methods listing
    other_methods = [m for m in cls.methods if m.name != "__init__"]
    if other_methods:
        parts.append("\n## Methods")
        for m in other_methods:
            sig = build_signature(m.name, m.parameters, m.return_type)
            markers: list[str] = []
            if m.is_abstract:
                markers.append("abstract")
            if any(d == "staticmethod" for d in m.decorators):
                markers.append("static")
            if any(d == "classmethod" for d in m.decorators):
                markers.append("classmethod")
            marker_str = f" [{', '.join(markers)}]" if markers else ""
            parts.append(f"- def {m.name}{sig}{marker_str}")
    return "\n".join(parts)


def _build_method_chunk(cls: ClassInfo, method: MethodInfo, module_path: str) -> str:
    """Build method chunk content with full source."""
    parts: list[str] = [f"# {cls.name}.{method.name}"]
    parts.append(f"Module: {module_path}")
    if method.docstring:
        parts.append(f"\n{method.docstring}")
    parts.append(f"\n```python\n{method.source}\n```")
    return "\n".join(parts)


def _build_function_chunk(func: FunctionInfo, module_path: str) -> str:
    """Build function chunk content with full source."""
    parts: list[str] = [f"# {func.name}"]
    parts.append(f"Module: {module_path}")
    if func.docstring:
        parts.append(f"\n{func.docstring}")
    parts.append(f"\n```python\n{func.source}\n```")
    return "\n".join(parts)
