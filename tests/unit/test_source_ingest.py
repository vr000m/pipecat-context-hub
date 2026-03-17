"""Tests for the SourceIngester."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


from pipecat_context_hub.services.ingest.ast_extractor import extract_module_info
from pipecat_context_hub.services.ingest.source_ingest import (
    SourceIngester,
    _build_chunks,
    _find_python_files,
    _make_chunk_id,
    _make_source_url,
    _sanitize_slug,
    _SKIP_DIRS,
)
from pipecat_context_hub.shared.types import ChunkedRecord

_TEST_REPO_SLUG = "pipecat-ai/pipecat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_writer() -> AsyncMock:
    """Create a mock IndexWriter."""
    writer = AsyncMock()
    writer.upsert = AsyncMock(side_effect=lambda records: len(records))
    writer.delete_by_source = AsyncMock(return_value=0)
    return writer


def _create_git_repo(repo_dir: Path, files: dict[str, str]) -> str:
    """Initialise a git repo at repo_dir with the given files and return commit SHA."""
    from git import Repo as GitRepo

    repo_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        fpath = repo_dir / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    git_repo = GitRepo.init(str(repo_dir))
    git_repo.index.add([str(repo_dir / p) for p in files])
    git_repo.index.commit("initial commit")
    return git_repo.head.commit.hexsha


# ---------------------------------------------------------------------------
# _find_python_files tests
# ---------------------------------------------------------------------------


class TestFindPythonFiles:
    """Tests for _find_python_files."""

    def test_skips_tests_dir(self, tmp_path: Path):
        """Python files inside tests/ are skipped."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("pass")
        (tmp_path / "core.py").write_text("pass")

        result = _find_python_files(tmp_path)
        names = {p.name for p in result}
        assert "test_foo.py" not in names
        assert "core.py" in names

    def test_includes_py_files(self, tmp_path: Path):
        """Normal .py files are included."""
        (tmp_path / "foo.py").write_text("pass")
        (tmp_path / "bar.py").write_text("pass")
        (tmp_path / "not_python.txt").write_text("hello")

        result = _find_python_files(tmp_path)
        names = {p.name for p in result}
        assert "foo.py" in names
        assert "bar.py" in names
        assert "not_python.txt" not in names

    def test_skips_pycache(self, tmp_path: Path):
        """__pycache__ directories are skipped."""
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-312.pyc").write_text("...")
        (tmp_path / "real.py").write_text("pass")

        result = _find_python_files(tmp_path)
        assert all("__pycache__" not in str(p) for p in result)
        assert len(result) == 1

    def test_skips_all_skip_dirs(self, tmp_path: Path):
        """All directories in _SKIP_DIRS are skipped."""
        for dirname in _SKIP_DIRS:
            d = tmp_path / dirname
            d.mkdir(exist_ok=True)
            (d / "file.py").write_text("pass")

        (tmp_path / "good.py").write_text("pass")
        result = _find_python_files(tmp_path)
        assert len(result) == 1
        assert result[0].name == "good.py"


# ---------------------------------------------------------------------------
# _sanitize_slug tests
# ---------------------------------------------------------------------------


class TestSanitizeSlug:
    """Tests for slug sanitization matching GitHubRepoIngester."""

    def test_slash_replaced(self):
        assert _sanitize_slug("pipecat-ai/pipecat") == "pipecat-ai_pipecat"

    def test_dot_replaced(self):
        """Dots in repo names are replaced, matching github_ingest sanitization."""
        assert _sanitize_slug("org/repo.name") == "org_repo_name"

    def test_preserves_hyphens(self):
        assert _sanitize_slug("vr000m/pipecat-mcp-server") == "vr000m_pipecat-mcp-server"

    def test_matches_github_ingest_regex(self):
        """Produces identical output to re.sub(r'[^a-zA-Z0-9_-]', '_', slug)."""
        import re
        slugs = [
            "pipecat-ai/pipecat",
            "org/repo.v2",
            "user/my_repo",
            "vr000m/pipecat-mcp-server",
        ]
        for slug in slugs:
            expected = re.sub(r"[^a-zA-Z0-9_-]", "_", slug)
            assert _sanitize_slug(slug) == expected, f"Mismatch for {slug}"


# ---------------------------------------------------------------------------
# _make_chunk_id tests
# ---------------------------------------------------------------------------


class TestMakeChunkId:
    """Tests for deterministic chunk ID generation."""

    def test_deterministic(self):
        """Same inputs produce the same ID."""
        id1 = _make_chunk_id("org/repo", "mod.path", "class_overview", "MyClass", "", "abc123", line_start=10)
        id2 = _make_chunk_id("org/repo", "mod.path", "class_overview", "MyClass", "", "abc123", line_start=10)
        assert id1 == id2

    def test_different_inputs_different_ids(self):
        """Different inputs produce different IDs."""
        id1 = _make_chunk_id("org/repo", "mod.a", "class_overview", "A", "", "sha1", line_start=1)
        id2 = _make_chunk_id("org/repo", "mod.b", "class_overview", "B", "", "sha2", line_start=1)
        assert id1 != id2

    def test_same_name_different_lines(self):
        """Duplicate class/method names at different lines produce different IDs."""
        id1 = _make_chunk_id("org/repo", "mod", "class_overview", "Foo", "", "sha", line_start=10)
        id2 = _make_chunk_id("org/repo", "mod", "class_overview", "Foo", "", "sha", line_start=50)
        assert id1 != id2

    def test_different_repos_different_ids(self):
        """Same module in different repos produces different IDs."""
        id1 = _make_chunk_id("org/repo-a", "mod", "class_overview", "Foo", "", "sha", line_start=10)
        id2 = _make_chunk_id("org/repo-b", "mod", "class_overview", "Foo", "", "sha", line_start=10)
        assert id1 != id2

    def test_format(self):
        """Chunk ID is 24-char hex string."""
        cid = _make_chunk_id("r", "m", "t", "c", "f", "s", line_start=1)
        assert len(cid) == 24
        # Should be valid hex.
        int(cid, 16)

    def test_matches_expected_sha256(self):
        """Chunk ID matches the expected SHA-256 prefix."""
        key = "source:org/repo:mod.path:module_overview:::abc:1"
        expected = hashlib.sha256(key.encode()).hexdigest()[:24]
        assert _make_chunk_id("org/repo", "mod.path", "module_overview", "", "", "abc", line_start=1) == expected


# ---------------------------------------------------------------------------
# _make_source_url tests
# ---------------------------------------------------------------------------


class TestMakeSourceUrl:
    """Tests for GitHub source URL generation."""

    def test_url_with_line_range(self):
        """URL includes line range fragment."""
        url = _make_source_url(_TEST_REPO_SLUG, "pipecat/frames/base.py", "abc123", 10, 50)
        assert url == (
            "https://github.com/pipecat-ai/pipecat/blob/abc123"
            "/src/pipecat/frames/base.py#L10-L50"
        )

    def test_url_without_line_range(self):
        """URL without line range when start/end are 0."""
        url = _make_source_url(_TEST_REPO_SLUG, "pipecat/frames/base.py", "abc123", 0, 0)
        assert url == (
            "https://github.com/pipecat-ai/pipecat/blob/abc123"
            "/src/pipecat/frames/base.py"
        )


# ---------------------------------------------------------------------------
# _build_chunks tests
# ---------------------------------------------------------------------------

_SIMPLE_MODULE_SOURCE = '''\
"""A simple module."""

class MyProcessor:
    """Processes frames."""

    def __init__(self, name: str):
        self.name = name

    def process(self, frame):
        """Process a single frame."""
        result = self.transform(frame)
        return result

    def tiny(self):
        pass


def helper_function(x, y):
    """A helper."""
    return x + y
    # extra line
    # more lines


def small():
    pass
'''


class TestBuildChunks:
    """Tests for _build_chunks."""

    def _get_chunks(self) -> list[ChunkedRecord]:
        """Build chunks from _SIMPLE_MODULE_SOURCE."""
        module_info = extract_module_info(_SIMPLE_MODULE_SOURCE, "pipecat.processors.my")
        return _build_chunks(
            module_info=module_info,
            source=_SIMPLE_MODULE_SOURCE,
            rel_path="pipecat/processors/my.py",
            commit_sha="deadbeef",
            now=datetime(2026, 2, 21, tzinfo=timezone.utc),
            repo_slug=_TEST_REPO_SLUG,
        )

    def test_module_overview_is_first(self):
        """First chunk should be the module overview."""
        chunks = self._get_chunks()
        assert len(chunks) > 0
        assert chunks[0].metadata["chunk_type"] == "module_overview"

    def test_class_overview_created(self):
        """A class_overview chunk is created for MyProcessor."""
        chunks = self._get_chunks()
        class_chunks = [c for c in chunks if c.metadata["chunk_type"] == "class_overview"]
        assert len(class_chunks) == 1
        assert class_chunks[0].metadata["class_name"] == "MyProcessor"

    def test_method_chunk_created_for_nontrivial(self):
        """Methods >= _MIN_METHOD_LINES get their own chunk."""
        chunks = self._get_chunks()
        method_chunks = [c for c in chunks if c.metadata["chunk_type"] == "method"]
        method_names = {c.metadata["method_name"] for c in method_chunks}
        # process has 4 lines (def + docstring + 2 body), __init__ has 2 lines (def + body).
        # 'tiny' has 2 lines (def + pass), should be excluded.
        assert "process" in method_names
        # tiny is too small
        assert "tiny" not in method_names

    def test_function_chunk_created(self):
        """Top-level functions >= _MIN_METHOD_LINES get a chunk."""
        chunks = self._get_chunks()
        func_chunks = [c for c in chunks if c.metadata["chunk_type"] == "function"]
        func_names = {c.metadata["method_name"] for c in func_chunks}
        assert "helper_function" in func_names
        # small() has only 2 lines, should be excluded.
        assert "small" not in func_names

    def test_all_content_type_is_source(self):
        """All chunks have content_type='source'."""
        chunks = self._get_chunks()
        for chunk in chunks:
            assert chunk.content_type == "source"

    def test_metadata_fields_present(self):
        """All chunks have required metadata fields."""
        chunks = self._get_chunks()
        required_keys = {
            "module_path", "chunk_type", "class_name", "method_name",
            "language", "line_start", "line_end",
        }
        for chunk in chunks:
            missing = required_keys - set(chunk.metadata.keys())
            assert not missing, f"Chunk {chunk.chunk_id} missing metadata: {missing}"

    def test_repo_is_pipecat(self):
        """All chunks have repo set to pipecat-ai/pipecat."""
        chunks = self._get_chunks()
        for chunk in chunks:
            assert chunk.repo == "pipecat-ai/pipecat"

    def test_commit_sha_propagated(self):
        """All chunks carry the commit SHA."""
        chunks = self._get_chunks()
        for chunk in chunks:
            assert chunk.commit_sha == "deadbeef"

    def test_source_url_format(self):
        """Source URLs point to GitHub with correct commit."""
        chunks = self._get_chunks()
        for chunk in chunks:
            assert chunk.source_url.startswith(
                "https://github.com/pipecat-ai/pipecat/blob/deadbeef/src/"
            )

    def test_class_overview_has_base_classes(self):
        """Class overview metadata includes base_classes."""
        chunks = self._get_chunks()
        class_chunks = [c for c in chunks if c.metadata["chunk_type"] == "class_overview"]
        assert len(class_chunks) == 1
        assert isinstance(class_chunks[0].metadata["base_classes"], list)

    def test_backslash_rel_path_produces_valid_module_path(self):
        """Regression: Windows-style backslash rel_path must not corrupt module_path.

        _build_chunks receives rel_path from the ingester. If the caller
        passes a Windows-style backslash path (e.g. from ``str(Path(...))``
        on Windows), module_path must still be dotted, not contain
        backslashes.
        """
        module_info = extract_module_info("x = 1\n", "pipecat.services.tts")
        chunks = _build_chunks(
            module_info=module_info,
            source="x = 1\n",
            rel_path="pipecat\\services\\tts.py",  # Windows-style
            commit_sha="abc",
            now=datetime(2026, 2, 21, tzinfo=timezone.utc),
            repo_slug=_TEST_REPO_SLUG,
        )
        # Source URLs should still work (backslashes are fine in URL path)
        # but module_path is derived from module_info, not rel_path, so it
        # should be correct. The real risk is in the ingester's rel_path
        # derivation — see test_rel_path_uses_posix_separators below.
        assert len(chunks) > 0
        for chunk in chunks:
            assert "\\" not in chunk.metadata["module_path"]


class TestRelPathNormalization:
    """Regression tests for cross-platform path normalization."""

    def test_rel_path_uses_posix_separators(self, tmp_path: Path):
        """as_posix() ensures forward slashes regardless of OS.

        On Windows, Path.relative_to() returns backslash-separated paths.
        The ingester must use as_posix() to normalize before building
        module_path. We verify this by checking that as_posix() on a
        relative path always uses forward slashes.
        """
        # Simulate the exact code path from source_ingest.py:
        #   rel_path = py_file.relative_to(clone_dir / "src").as_posix()
        #   module_path = rel_path.replace("/", ".").removesuffix(".py")
        clone_src = tmp_path / "src"
        clone_src.mkdir()
        nested = clone_src / "pipecat" / "services" / "tts.py"
        nested.parent.mkdir(parents=True)
        nested.write_text("x = 1\n")

        rel_path = nested.relative_to(clone_src).as_posix()
        module_path = rel_path.replace("/", ".").removesuffix(".py")

        assert rel_path == "pipecat/services/tts.py"
        assert module_path == "pipecat.services.tts"
        assert "\\" not in rel_path
        assert "\\" not in module_path

    def test_init_module_path_normalization(self, tmp_path: Path):
        """__init__.py normalization works with posix paths."""
        clone_src = tmp_path / "src"
        nested = clone_src / "pipecat" / "frames" / "__init__.py"
        nested.parent.mkdir(parents=True)
        nested.write_text("")

        rel_path = nested.relative_to(clone_src).as_posix()
        module_path = rel_path.replace("/", ".").removesuffix(".py")
        if module_path.endswith(".__init__"):
            module_path = module_path.removesuffix(".__init__")

        assert module_path == "pipecat.frames"


# ---------------------------------------------------------------------------
# SourceIngester tests
# ---------------------------------------------------------------------------


class TestSourceIngester:
    """Tests for the SourceIngester class."""

    def _make_config(self, tmp_path: Path) -> MagicMock:
        config = MagicMock()
        config.storage.data_dir = tmp_path
        return config

    async def test_ingest_missing_src_dir(self, tmp_path: Path):
        """Returns 0 records when repo has no src/ directory."""
        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = SourceIngester(config, writer, "pipecat-ai/pipecat")

        result = await ingester.ingest()

        assert result.source == "source:pipecat-ai/pipecat"
        assert result.errors == []
        assert result.records_upserted == 0

    async def test_ingest_with_mock_files(self, tmp_path: Path):
        """Ingests Python files from a mock pipecat source tree."""
        # Create the expected directory structure.
        clone_dir = tmp_path / "repos" / "pipecat-ai_pipecat"
        src_dir = clone_dir / "src" / "pipecat"
        frames_dir = src_dir / "frames"
        frames_dir.mkdir(parents=True)

        # Write a simple Python file.
        (frames_dir / "__init__.py").write_text("")
        (frames_dir / "base.py").write_text(
            '"""Frame base classes."""\n\n\n'
            "class Frame:\n"
            '    """Base class for all frames."""\n\n'
            "    def __init__(self):\n"
            "        self.id = None\n"
            "        self.name = None\n"
            "        self.data = {}\n\n"
            "    def process(self, data):\n"
            '        """Process incoming data."""\n'
            "        self.data = data\n"
            "        return self.data\n"
        )

        # Also create a top-level __init__.py
        (src_dir / "__init__.py").write_text('"""Pipecat package."""\n')

        # Init a git repo so _get_commit_sha works.
        commit_sha = _create_git_repo(clone_dir, {
            "src/pipecat/__init__.py": '"""Pipecat package."""\n',
            "src/pipecat/frames/__init__.py": "",
            "src/pipecat/frames/base.py": (frames_dir / "base.py").read_text(),
        })

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = SourceIngester(config, writer, "pipecat-ai/pipecat")

        result = await ingester.ingest()

        assert result.source == "source:pipecat-ai/pipecat"
        assert result.errors == []
        assert result.records_upserted > 0

        writer.upsert.assert_called_once()
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]

        # Should have at least: module overviews + class overview + method chunks
        assert len(records) >= 3

        # All records should be source type.
        for rec in records:
            assert rec.content_type == "source"
            assert rec.repo == "pipecat-ai/pipecat"
            assert rec.commit_sha == commit_sha
            assert isinstance(rec.indexed_at, datetime)

        # Check that we have module_overview, class_overview, and method chunks.
        chunk_types = {rec.metadata["chunk_type"] for rec in records}
        assert "module_overview" in chunk_types
        assert "class_overview" in chunk_types

    async def test_ingest_skips_test_dirs(self, tmp_path: Path):
        """Test directories inside pipecat source are skipped."""
        clone_dir = tmp_path / "repos" / "pipecat-ai_pipecat"
        src_dir = clone_dir / "src" / "pipecat"

        # Create a normal module and a tests dir.
        (src_dir / "core").mkdir(parents=True)
        (src_dir / "core" / "main.py").write_text("class Core:\n    pass\n")
        (src_dir / "tests").mkdir(parents=True)
        (src_dir / "tests" / "test_core.py").write_text("def test_it(): pass\n")
        (src_dir / "__init__.py").write_text("")

        _create_git_repo(clone_dir, {
            "src/pipecat/__init__.py": "",
            "src/pipecat/core/main.py": "class Core:\n    pass\n",
            "src/pipecat/tests/test_core.py": "def test_it(): pass\n",
        })

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = SourceIngester(config, writer, "pipecat-ai/pipecat")

        result = await ingester.ingest()

        assert result.errors == []
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        paths = {rec.path for rec in records}
        assert not any("tests" in p for p in paths)
        assert any("core" in p for p in paths)

    async def test_ingest_syntax_error_reported(self, tmp_path: Path):
        """Files with syntax errors are reported but don't crash ingestion."""
        clone_dir = tmp_path / "repos" / "pipecat-ai_pipecat"
        src_dir = clone_dir / "src" / "pipecat"
        src_dir.mkdir(parents=True)

        (src_dir / "__init__.py").write_text("")
        (src_dir / "good.py").write_text("x = 1\n")
        (src_dir / "bad.py").write_text("def broken(:\n")

        _create_git_repo(clone_dir, {
            "src/pipecat/__init__.py": "",
            "src/pipecat/good.py": "x = 1\n",
            "src/pipecat/bad.py": "def broken(:\n",
        })

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = SourceIngester(config, writer, "pipecat-ai/pipecat")

        result = await ingester.ingest()

        # Should still produce records for the good file.
        assert result.records_upserted > 0
        # Should report the syntax error.
        assert any("SyntaxError" in e for e in result.errors)

    async def test_ingest_upsert_failure(self, tmp_path: Path):
        """Writer.upsert failure is reported as an error."""
        clone_dir = tmp_path / "repos" / "pipecat-ai_pipecat"
        src_dir = clone_dir / "src" / "pipecat"
        src_dir.mkdir(parents=True)

        (src_dir / "__init__.py").write_text("")
        (src_dir / "mod.py").write_text("x = 1\n")

        _create_git_repo(clone_dir, {
            "src/pipecat/__init__.py": "",
            "src/pipecat/mod.py": "x = 1\n",
        })

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        writer.upsert = AsyncMock(side_effect=RuntimeError("db error"))
        ingester = SourceIngester(config, writer, "pipecat-ai/pipecat")

        result = await ingester.ingest()

        assert result.records_upserted == 0
        assert any("upsert failed" in e for e in result.errors)

    async def test_ingest_idempotent(self, tmp_path: Path):
        """Same commit SHA produces identical chunk IDs across runs."""
        clone_dir = tmp_path / "repos" / "pipecat-ai_pipecat"
        src_dir = clone_dir / "src" / "pipecat"
        src_dir.mkdir(parents=True)

        (src_dir / "__init__.py").write_text("")
        (src_dir / "mod.py").write_text("class A:\n    pass\n")

        _create_git_repo(clone_dir, {
            "src/pipecat/__init__.py": "",
            "src/pipecat/mod.py": "class A:\n    pass\n",
        })

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = SourceIngester(config, writer, "pipecat-ai/pipecat")

        await ingester.ingest()
        await ingester.ingest()

        records1: list[ChunkedRecord] = writer.upsert.call_args_list[0][0][0]
        records2: list[ChunkedRecord] = writer.upsert.call_args_list[1][0][0]
        ids1 = sorted(r.chunk_id for r in records1)
        ids2 = sorted(r.chunk_id for r in records2)
        assert ids1 == ids2

    async def test_init_module_path(self, tmp_path: Path):
        """__init__.py files get the parent package as module_path."""
        clone_dir = tmp_path / "repos" / "pipecat-ai_pipecat"
        src_dir = clone_dir / "src" / "pipecat" / "frames"
        src_dir.mkdir(parents=True)

        (clone_dir / "src" / "pipecat" / "__init__.py").write_text("")
        (src_dir / "__init__.py").write_text('"""Frames package."""\n')

        _create_git_repo(clone_dir, {
            "src/pipecat/__init__.py": "",
            "src/pipecat/frames/__init__.py": '"""Frames package."""\n',
        })

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = SourceIngester(config, writer, "pipecat-ai/pipecat")

        result = await ingester.ingest()

        assert result.errors == []
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        module_paths = {rec.metadata["module_path"] for rec in records}
        # pipecat/frames/__init__.py -> module_path "pipecat.frames" (not "pipecat.frames.__init__")
        assert "pipecat.frames" in module_paths

    async def test_ingest_extra_repo(self, tmp_path: Path):
        """SourceIngester processes a non-pipecat repo with src/ layout."""
        clone_dir = tmp_path / "repos" / "org_extra-repo"
        src_dir = clone_dir / "src" / "my_pkg"
        src_dir.mkdir(parents=True)

        (src_dir / "__init__.py").write_text('"""My package."""\n')
        (src_dir / "agent.py").write_text(
            "class BaseAgent:\n"
            '    """Base agent class."""\n\n'
            "    def __init__(self, name: str):\n"
            "        self.name = name\n"
            "        self.state = {}\n"
            "        self.running = False\n\n"
            "    def run(self):\n"
            '        """Run the agent."""\n'
            "        self.running = True\n"
            "        return self.state\n"
        )

        _create_git_repo(clone_dir, {
            "src/my_pkg/__init__.py": '"""My package."""\n',
            "src/my_pkg/agent.py": (src_dir / "agent.py").read_text(),
        })

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = SourceIngester(config, writer, "org/extra-repo")

        result = await ingester.ingest()

        assert result.source == "source:org/extra-repo"
        assert result.errors == []
        assert result.records_upserted > 0

        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        for rec in records:
            assert rec.content_type == "source"
            assert rec.repo == "org/extra-repo"

        chunk_types = {rec.metadata["chunk_type"] for rec in records}
        assert "module_overview" in chunk_types
        assert "class_overview" in chunk_types

        for rec in records:
            assert rec.source_url.startswith("https://github.com/org/extra-repo/blob/")

    async def test_repo_without_src_dir(self, tmp_path: Path):
        """Repo without src/ directory returns 0 records, no errors."""
        clone_dir = tmp_path / "repos" / "org_no-src-repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / "README.md").write_text("# No src layout\n")

        _create_git_repo(clone_dir, {"README.md": "# No src layout\n"})

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = SourceIngester(config, writer, "org/no-src-repo")

        result = await ingester.ingest()

        assert result.source == "source:org/no-src-repo"
        assert result.errors == []
        assert result.records_upserted == 0
        writer.upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Call-graph metadata tests
# ---------------------------------------------------------------------------


_CALLGRAPH_MODULE_SOURCE = '''\
"""Module with yields and calls."""

from pipecat.frames.frames import TTSAudioRawFrame, TTSStoppedFrame
from .utils import helper_func
import os

class TTSService:
    """Text-to-speech service."""

    async def run_tts(self, text: str):
        """Generate TTS audio."""
        audio = self._synthesize(text)
        yield TTSAudioRawFrame(audio=audio)
        yield TTSStoppedFrame()
        await self.push_frame(audio)
'''


class TestCallGraphMetadata:
    """Tests for yields/calls/imports in chunk metadata."""

    def _get_chunks(self) -> list[ChunkedRecord]:
        """Build chunks from _CALLGRAPH_MODULE_SOURCE."""
        module_info = extract_module_info(_CALLGRAPH_MODULE_SOURCE, "pipecat.services.tts")
        return _build_chunks(
            module_info=module_info,
            source=_CALLGRAPH_MODULE_SOURCE,
            rel_path="pipecat/services/tts.py",
            commit_sha="abc123",
            now=datetime(2026, 3, 16, tzinfo=timezone.utc),
            repo_slug=_TEST_REPO_SLUG,
        )

    def test_method_chunk_has_yields(self):
        """Method chunks include yields metadata."""
        chunks = self._get_chunks()
        method_chunks = [c for c in chunks if c.metadata["chunk_type"] == "method"]
        assert len(method_chunks) >= 1
        run_tts = [c for c in method_chunks if c.metadata["method_name"] == "run_tts"][0]
        assert "TTSAudioRawFrame" in run_tts.metadata["yields"]
        assert "TTSStoppedFrame" in run_tts.metadata["yields"]

    def test_method_chunk_has_calls(self):
        """Method chunks include calls metadata."""
        chunks = self._get_chunks()
        method_chunks = [c for c in chunks if c.metadata["chunk_type"] == "method"]
        run_tts = [c for c in method_chunks if c.metadata["method_name"] == "run_tts"][0]
        assert "_synthesize" in run_tts.metadata["calls"]
        assert "push_frame" in run_tts.metadata["calls"]

    def test_method_chunk_has_pipecat_imports_only(self):
        """Method chunks get pipecat-internal imports (absolute + relative), not stdlib."""
        chunks = self._get_chunks()
        method_chunks = [c for c in chunks if c.metadata["chunk_type"] == "method"]
        run_tts = [c for c in method_chunks if c.metadata["method_name"] == "run_tts"][0]
        imports = run_tts.metadata["imports"]
        assert any("pipecat" in i for i in imports)
        assert any(i.startswith("from .") for i in imports), "relative imports should be included"
        assert not any("import os" == i for i in imports)

    def test_class_overview_has_pipecat_imports(self):
        """Class overview chunks get pipecat-internal imports."""
        chunks = self._get_chunks()
        class_chunks = [c for c in chunks if c.metadata["chunk_type"] == "class_overview"]
        assert len(class_chunks) == 1
        imports = class_chunks[0].metadata["imports"]
        assert any("pipecat" in i for i in imports)
        assert not any("import os" == i for i in imports)

    def test_module_overview_retains_full_imports(self):
        """Module overview retains the full imports list."""
        chunks = self._get_chunks()
        module_chunks = [c for c in chunks if c.metadata["chunk_type"] == "module_overview"]
        assert len(module_chunks) == 1
        imports = module_chunks[0].metadata["imports"]
        assert any("pipecat" in i for i in imports)
        assert any("import os" == i for i in imports)

    def test_method_content_includes_yields_section(self):
        """Method chunk content text includes Yields section."""
        chunks = self._get_chunks()
        method_chunks = [c for c in chunks if c.metadata["chunk_type"] == "method"]
        run_tts = [c for c in method_chunks if c.metadata["method_name"] == "run_tts"][0]
        assert "## Yields" in run_tts.content
        assert "TTSAudioRawFrame" in run_tts.content

    def test_method_content_includes_calls_section(self):
        """Method chunk content text includes Calls section."""
        chunks = self._get_chunks()
        method_chunks = [c for c in chunks if c.metadata["chunk_type"] == "method"]
        run_tts = [c for c in method_chunks if c.metadata["method_name"] == "run_tts"][0]
        assert "## Calls" in run_tts.content
        assert "push_frame" in run_tts.content

    def test_existing_chunks_still_have_required_metadata(self):
        """Existing required metadata fields still present after adding new fields."""
        chunks = self._get_chunks()
        required_keys = {
            "module_path", "chunk_type", "class_name", "method_name",
            "language", "line_start", "line_end",
        }
        for chunk in chunks:
            missing = required_keys - set(chunk.metadata.keys())
            assert not missing, f"Chunk {chunk.chunk_id} missing metadata: {missing}"
