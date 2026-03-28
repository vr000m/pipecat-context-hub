"""Source ingester using Python AST extraction.

Walks a cloned repo's ``src/`` packages (from GitHubRepoIngester's clone),
extracts API metadata via AST, and produces ChunkedRecord objects with
content_type="source".  One SourceIngester instance per repo slug.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from git import Repo as GitRepo

from pipecat_context_hub.services.ingest.rst_type_parser import parse_rst_types
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


def _sanitize_slug(slug: str) -> str:
    """Sanitize a repo slug to a safe directory name.

    Must match the sanitization in GitHubRepoIngester.clone_or_fetch
    so source ingest finds the same clone directory.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", slug)

class SourceIngester:
    """Ingests source code from a single repository as API reference chunks."""

    def __init__(self, config: HubConfig, writer: IndexWriter, repo_slug: str) -> None:
        self._repos_dir = config.storage.data_dir / "repos"
        self._writer = writer
        self._repo_slug = repo_slug

    async def ingest(self) -> IngestResult:
        """Extract API metadata from repo source and index it."""
        start = time.monotonic()
        errors: list[str] = []
        records: list[ChunkedRecord] = []

        # 1. Locate the repo clone
        clone_dir = self._repos_dir / _sanitize_slug(self._repo_slug)

        # 2. Check for .pyi stubs at repo root BEFORE src/ check.
        # Root-only glob (not recursive) — stubs are at repo root for known
        # targets like daily-python. .pyi files are NOT in _CODE_EXTENSIONS
        # to avoid duplicate indexing by GitHubRepoIngester.
        pyi_files: list[Path] = sorted(
            f for f in clone_dir.glob("*.pyi")
            if f.is_file() and not f.is_symlink()
        )

        # 3. Discover Python packages under src/
        src_dir = clone_dir / "src"
        pkg_dirs: list[Path] = []
        if src_dir.is_dir():
            pkg_dirs = sorted(
                d for d in src_dir.iterdir()
                if d.is_dir() and (d / "__init__.py").is_file()
            )

        # 2b. Check for RST type docs in docs/
        rst_files: list[Path] = []
        docs_dir = clone_dir / "docs"
        if docs_dir.is_dir() and not docs_dir.is_symlink():
            rst_files = sorted(
                f for f in docs_dir.rglob("*.rst")
                if f.is_file() and not f.is_symlink()
            )

        # Nothing to index
        if not pkg_dirs and not pyi_files and not rst_files:
            return IngestResult(source=f"source:{self._repo_slug}")

        if pyi_files and not pkg_dirs:
            logger.info(
                "No Python packages in src/, found %d .pyi stubs at root (%s)",
                len(pyi_files), self._repo_slug,
            )

        # 3. Get commit SHA
        commit_sha = _get_commit_sha(clone_dir)

        now = datetime.now(tz=timezone.utc)

        # 4. Walk each package directory
        total_files = 0
        for pkg_dir in pkg_dirs:
            py_files = _find_python_files(pkg_dir)
            total_files += len(py_files)
            logger.info(
                "Found %d Python files in %s (%s)",
                len(py_files), pkg_dir.name, self._repo_slug,
            )

            for py_file in py_files:
                # Skip symlinks and files that resolve outside the repo
                if py_file.is_symlink():
                    continue
                try:
                    py_file.resolve().relative_to(clone_dir.resolve())
                    source = py_file.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    rel = py_file.relative_to(clone_dir).as_posix()
                    errors.append(f"Error reading {rel}: {exc}")
                    continue

                rel_path_from_src = py_file.relative_to(src_dir).as_posix()
                rel_path = f"src/{rel_path_from_src}"
                module_path = rel_path_from_src.replace("/", ".").removesuffix(".py")
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
                    repo_slug=self._repo_slug,
                )
                records.extend(file_records)

        # 4b. Index .pyi stubs (fallback path for repos without Python packages)
        for pyi_file in pyi_files:
            total_files += 1
            try:
                pyi_file.resolve().relative_to(clone_dir.resolve())
                source = pyi_file.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                errors.append(f"Error reading {pyi_file.name}: {exc}")
                continue

            rel_path = pyi_file.name  # e.g., "daily.pyi"
            module_path = pyi_file.stem  # e.g., "daily"

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
                repo_slug=self._repo_slug,
            )
            records.extend(file_records)

        # 4c. Index RST type definitions from docs/
        for rst_file in rst_files:
            try:
                rst_file.resolve().relative_to(clone_dir.resolve())
            except ValueError:
                errors.append(f"RST file escapes repo: {rst_file}")
                continue

            type_defs = parse_rst_types(rst_file)
            if not type_defs:
                continue

            total_files += 1
            rel_path = rst_file.relative_to(clone_dir).as_posix()
            # Derive module_path from repo slug (e.g. "daily-co/daily-python" → "daily").
            # This is intentionally approximate for RST sources — unlike AST chunks
            # where module_path is a dotted Python import path, RST files have no
            # Python import path. The derived name is used for module prefix filtering.
            module_path = self._repo_slug.split("/")[-1].replace("-", "_")
            # Strip common suffixes like "_python" for cleaner module names
            for suffix in ("_python", "_sdk", "_client"):
                if module_path.endswith(suffix):
                    module_path = module_path[: -len(suffix)]
                    break

            for typedef in type_defs:
                chunk_id = hashlib.sha256(
                    f"{self._repo_slug}:{rel_path}:{typedef.name}".encode()
                ).hexdigest()[:24]

                source_url = _make_source_url(
                    self._repo_slug, rel_path, commit_sha,
                    typedef.line_start, typedef.line_end,
                )

                content = typedef.render_content(module_path)

                metadata: dict[str, object] = {
                    "chunk_type": "type_definition",
                    "class_name": typedef.name,
                    "module_path": module_path,
                    "line_start": typedef.line_start,
                    "line_end": typedef.line_end,
                }
                if typedef.fields:
                    metadata["fields"] = [
                        {"key": f.key, "value_type": f.value_type}
                        for f in typedef.fields
                    ]
                elif typedef.alternatives:
                    # Flatten all alternative fields for search
                    all_fields: list[dict[str, str]] = []
                    for alt in typedef.alternatives:
                        all_fields.extend(
                            {"key": f.key, "value_type": f.value_type}
                            for f in alt
                        )
                    metadata["fields"] = all_fields
                if typedef.rst_refs:
                    metadata["rst_refs"] = typedef.rst_refs

                records.append(ChunkedRecord(
                    chunk_id=chunk_id,
                    content=content,
                    content_type="source",
                    source_url=source_url,
                    repo=self._repo_slug,
                    path=rel_path,
                    commit_sha=commit_sha,
                    indexed_at=now,
                    metadata=metadata,
                ))

            logger.info(
                "RST type ingest (%s): file=%s types=%d",
                self._repo_slug, rel_path, len(type_defs),
            )

        # 5. Batch upsert
        upserted = 0
        if records:
            try:
                upserted = await self._writer.upsert(records)
            except Exception as exc:
                errors.append(f"IndexWriter.upsert failed: {exc}")

        duration = round(time.monotonic() - start, 3)
        logger.info(
            "Source ingest (%s): files=%d chunks=%d upserted=%d errors=%d duration=%.1fs",
            self._repo_slug, total_files, len(records), upserted, len(errors), duration,
        )
        return IngestResult(
            source=f"source:{self._repo_slug}",
            records_upserted=upserted,
            errors=errors,
            duration_seconds=duration,
        )


def _get_commit_sha(clone_dir: Path) -> str:
    """Get HEAD commit SHA from git repo."""
    try:
        return GitRepo(str(clone_dir)).head.commit.hexsha
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
    repo_slug: str, module_path: str, chunk_type: str, class_name: str,
    method_name: str, commit_sha: str, line_start: int = 0,
) -> str:
    """Deterministic chunk ID scoped to repo."""
    key = f"source:{repo_slug}:{module_path}:{chunk_type}:{class_name}:{method_name}:{commit_sha}:{line_start}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _make_source_url(repo_slug: str, rel_path: str, commit_sha: str, line_start: int, line_end: int) -> str:
    """Build GitHub source URL with line range.

    ``rel_path`` must be relative to the repo root (e.g. ``src/pipecat/foo.py``
    or ``daily.pyi``), not relative to ``src/``.
    """
    base = f"https://github.com/{repo_slug}/blob/{commit_sha}/{rel_path}"
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
    repo_slug: str,
) -> list[ChunkedRecord]:
    """Build ChunkedRecord list from extracted module info."""
    records: list[ChunkedRecord] = []
    mp = module_info.module_path

    # Pipecat-internal imports for propagation to class/method chunks.
    # Module overview retains the full imports list unchanged.
    # Include both absolute pipecat imports and relative imports (from . / from ..)
    # since relative imports within pipecat packages are also pipecat-internal.
    pipecat_imports = [
        i for i in module_info.imports
        if "pipecat" in i or i.startswith("from .")
    ]

    # --- Module overview chunk ---
    module_content = _build_module_overview(module_info)
    records.append(ChunkedRecord(
        chunk_id=_make_chunk_id(repo_slug, mp, "module_overview", "", "", commit_sha, line_start=1),
        content=module_content,
        content_type="source",
        source_url=_make_source_url(repo_slug, rel_path, commit_sha, 1, len(source.splitlines())),
        repo=repo_slug,
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
            "imports": module_info.imports,
        },
    ))

    # --- Class chunks ---
    for cls in module_info.classes:
        # Class overview
        class_content = _build_class_overview(cls, mp)
        records.append(ChunkedRecord(
            chunk_id=_make_chunk_id(repo_slug, mp, "class_overview", cls.name, "", commit_sha, line_start=cls.line_start),
            content=class_content,
            content_type="source",
            source_url=_make_source_url(repo_slug, rel_path, commit_sha, cls.line_start, cls.line_end),
            repo=repo_slug,
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
                "imports": pipecat_imports,
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
                chunk_id=_make_chunk_id(repo_slug, mp, "method", cls.name, method.name, commit_sha, line_start=method.line_start),
                content=method_content,
                content_type="source",
                source_url=_make_source_url(
                    repo_slug, rel_path, commit_sha, method.line_start, method.line_end
                ),
                repo=repo_slug,
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
                    "yields": method.yields,
                    "calls": method.calls,
                    "imports": method.imports,
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
            chunk_id=_make_chunk_id(repo_slug, mp, "function", "", func.name, commit_sha, line_start=func.line_start),
            content=func_content,
            content_type="source",
            source_url=_make_source_url(repo_slug, rel_path, commit_sha, func.line_start, func.line_end),
            repo=repo_slug,
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
                "yields": func.yields,
                "calls": func.calls,
                "imports": func.imports,
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
    if method.yields:
        parts.append(f"\n## Yields\n{', '.join(method.yields)}")
    if method.calls:
        parts.append(f"\n## Calls\n{', '.join(method.calls)}")
    return "\n".join(parts)


def _build_function_chunk(func: FunctionInfo, module_path: str) -> str:
    """Build function chunk content with full source."""
    parts: list[str] = [f"# {func.name}"]
    parts.append(f"Module: {module_path}")
    if func.docstring:
        parts.append(f"\n{func.docstring}")
    parts.append(f"\n```python\n{func.source}\n```")
    if func.yields:
        parts.append(f"\n## Yields\n{', '.join(func.yields)}")
    if func.calls:
        parts.append(f"\n## Calls\n{', '.join(func.calls)}")
    return "\n".join(parts)
