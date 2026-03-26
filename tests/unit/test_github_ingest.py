"""Unit tests for the GitHub repo ingester."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pipecat_context_hub.services.ingest.github_ingest import (
    GitHubRepoIngester,
    _ROOT_FALLBACK_SKIP_ROOT_DIRS,
    _chunk_by_boundaries,
    _chunk_by_lines,
    _chunk_code,
    _discover_root_level_examples,
    _find_example_dirs,
    _infer_domain,
    _iter_code_files,
    _iter_root_level_code_files,
    _make_chunk_id,
    repo_ref_is_tainted,
)
from pipecat_context_hub.shared.config import HubConfig, StorageConfig
from pipecat_context_hub.shared.types import ChunkedRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_writer() -> AsyncMock:
    """Create a mock IndexWriter."""
    writer = AsyncMock()
    writer.upsert = AsyncMock(side_effect=lambda records: len(records))
    writer.delete_by_source = AsyncMock(return_value=0)
    return writer


def _create_fake_repo(tmp_path: Path, repo_name: str, files: dict[str, str]) -> Path:
    """Create a fake git-like repo directory with files.

    ``files`` maps relative paths to content.
    """
    from git import Repo as GitRepo

    repo_dir = tmp_path / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)

    for rel_path, content in files.items():
        fpath = repo_dir / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    git_repo = GitRepo.init(str(repo_dir))
    git_repo.index.add([str(repo_dir / p) for p in files])
    git_repo.index.commit("initial commit")
    return repo_dir


def _create_remote_and_clone(tmp_path: Path, repo_slug: str, files: dict[str, str]) -> tuple[Path, Path]:
    """Create a source repo with a bare origin and return the local clone path."""
    from git import Repo as GitRepo

    source_dir = _create_fake_repo(tmp_path, "source_repo", files)
    source_repo = GitRepo(str(source_dir))
    bare_remote = tmp_path / "origin.git"
    GitRepo.clone_from(str(source_dir), str(bare_remote), bare=True)
    source_repo.create_remote("origin", str(bare_remote))
    branch = source_repo.active_branch.name
    source_repo.git.push("origin", branch, "--tags")

    safe_name = repo_slug.replace("/", "_")
    clone_dir = tmp_path / "data" / "repos" / safe_name
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    GitRepo.clone_from(str(bare_remote), str(clone_dir))
    return source_dir, clone_dir


def _commit_and_push(
    source_dir: Path,
    rel_path: str,
    content: str,
    *,
    tag: str | None = None,
) -> str:
    """Commit a change in the source repo and push it to origin."""
    from git import Repo as GitRepo

    repo = GitRepo(str(source_dir))
    file_path = source_dir / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    repo.index.add([str(file_path)])
    repo.index.commit("update")
    if tag is not None:
        repo.create_tag(tag, message=f"tag {tag}")
    branch = repo.active_branch.name
    repo.git.push("origin", branch, "--tags")
    return repo.head.commit.hexsha


# ---------------------------------------------------------------------------
# _chunk_code tests
# ---------------------------------------------------------------------------


class TestChunkCode:
    """Tests for the _chunk_code function."""

    def test_small_file_single_chunk(self):
        """A file smaller than max_tokens returns a single chunk."""
        source = "x = 1\ny = 2\n"
        chunks = _chunk_code(source, max_tokens=256)
        assert len(chunks) == 1
        assert chunks[0] == source

    def test_function_boundary_splitting(self):
        """Large file with functions splits at boundaries."""
        funcs = []
        for i in range(10):
            body = "\n".join(f"    line_{i}_{j} = {j}" for j in range(20))
            funcs.append(f"def func_{i}():\n{body}\n\n")
        source = "\n".join(funcs)
        chunks = _chunk_code(source, max_tokens=100, overlap_tokens=0, prefer_boundaries=True)
        assert len(chunks) > 1
        # Each chunk should contain at least one function definition.
        for chunk in chunks:
            assert "def func_" in chunk or "line_" in chunk

    def test_line_based_fallback(self):
        """When prefer_boundaries=False, falls back to line splitting."""
        source = "\n".join(f"line_{i} = {i}" for i in range(200))
        chunks = _chunk_code(source, max_tokens=50, overlap_tokens=5, prefer_boundaries=False)
        assert len(chunks) > 1

    def test_no_boundaries_falls_back(self):
        """File with no function/class defs falls back to line-based."""
        source = "\n".join(f"x_{i} = {i}" for i in range(200))
        chunks = _chunk_code(source, max_tokens=50, overlap_tokens=0, prefer_boundaries=True)
        assert len(chunks) > 1

    def test_overlap_present(self):
        """Chunks with overlap share content at boundaries."""
        funcs = []
        for i in range(5):
            body = "\n".join(f"    v_{i}_{j} = {j}" for j in range(30))
            funcs.append(f"def func_{i}():\n{body}\n\n")
        source = "\n".join(funcs)
        chunks = _chunk_code(source, max_tokens=80, overlap_tokens=10, prefer_boundaries=True)
        if len(chunks) >= 2:
            # The end of chunk 0 should appear at the start of chunk 1.
            tail = chunks[0][-20:]
            assert tail in chunks[1]

    def test_empty_source(self):
        """Empty string returns single empty chunk."""
        chunks = _chunk_code("", max_tokens=256)
        assert chunks == [""]


# ---------------------------------------------------------------------------
# _chunk_by_lines tests
# ---------------------------------------------------------------------------


class TestChunkByLines:
    """Tests for the line-based chunker."""

    def test_basic_splitting(self):
        lines = [f"line_{i}\n" for i in range(100)]
        chunks = _chunk_by_lines(lines, max_tokens=20, overlap_tokens=0)
        assert len(chunks) > 1
        # All lines should appear across chunks.
        joined = "".join(chunks)
        for line in lines:
            assert line in joined

    def test_single_line_chunk(self):
        lines = ["short\n"]
        chunks = _chunk_by_lines(lines, max_tokens=256, overlap_tokens=0)
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# _chunk_by_boundaries tests
# ---------------------------------------------------------------------------


class TestChunkByBoundaries:
    """Tests for the boundary-aware chunker."""

    def test_no_boundaries_returns_empty(self):
        lines = ["x = 1\n", "y = 2\n"]
        result = _chunk_by_boundaries(lines, max_tokens=256, overlap_tokens=0)
        assert result == []

    def test_single_function(self):
        lines = ["def foo():\n", "    return 1\n"]
        result = _chunk_by_boundaries(lines, max_tokens=256, overlap_tokens=0)
        assert len(result) == 1
        assert "def foo" in result[0]


# ---------------------------------------------------------------------------
# _make_chunk_id tests
# ---------------------------------------------------------------------------


class TestMakeChunkId:
    """Tests for deterministic chunk ID generation."""

    def test_deterministic(self):
        id1 = _make_chunk_id("pipecat-ai/pipecat", "examples/foo.py", "abc123", 0)
        id2 = _make_chunk_id("pipecat-ai/pipecat", "examples/foo.py", "abc123", 0)
        assert id1 == id2

    def test_different_index_different_id(self):
        id1 = _make_chunk_id("repo", "path.py", "sha", 0)
        id2 = _make_chunk_id("repo", "path.py", "sha", 1)
        assert id1 != id2

    def test_format(self):
        cid = _make_chunk_id("r", "p", "s", 0)
        assert len(cid) == 24
        # Should be valid hex.
        int(cid, 16)

    def test_matches_expected_sha256(self):
        key = "repo:path.py:sha:0"
        expected = hashlib.sha256(key.encode()).hexdigest()[:24]
        assert _make_chunk_id("repo", "path.py", "sha", 0) == expected


class TestRepoRefIsTainted:
    def test_matches_commit_prefix(self, tmp_path: Path):
        repo_dir = _create_fake_repo(tmp_path, "repo", {"main.py": "print('ok')\n"})
        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha
        assert repo_ref_is_tainted(repo_dir, commit_sha, {commit_sha[:8]})

    def test_matches_tag_name(self, tmp_path: Path):
        repo_dir = _create_fake_repo(tmp_path, "repo", {"main.py": "print('ok')\n"})
        from git import Repo as GitRepo

        git_repo = GitRepo(str(repo_dir))
        git_repo.create_tag("v1.2.3", message="test tag")
        commit_sha = git_repo.head.commit.hexsha
        assert repo_ref_is_tainted(repo_dir, commit_sha, {"v1.2.3"})

    def test_non_matching_tag_returns_false(self, tmp_path: Path):
        repo_dir = _create_fake_repo(tmp_path, "repo", {"main.py": "print('ok')\n"})
        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha
        assert not repo_ref_is_tainted(repo_dir, commit_sha, {"v9.9.9"})

    def test_repo_open_failure_fails_closed_for_named_refs(self, tmp_path: Path):
        missing_repo = tmp_path / "missing-repo"
        assert repo_ref_is_tainted(missing_repo, "deadbeef", {"v1.2.3"})


class TestCloneOrFetchCheckoutControl:
    def test_checkout_false_keeps_existing_worktree_and_fetches_tags(self, tmp_path: Path):
        from git import Repo as GitRepo

        repo_slug = "test-org/test-repo"
        source_dir, clone_dir = _create_remote_and_clone(
            tmp_path,
            repo_slug,
            {"main.py": "print('old')\n"},
        )
        config = HubConfig(
            storage=StorageConfig(data_dir=tmp_path / "data"),
            sources=HubConfig().sources.model_copy(update={"repos": [repo_slug]}),
        )
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        local_repo = GitRepo(str(clone_dir))
        old_sha = local_repo.head.commit.hexsha
        new_sha = _commit_and_push(
            source_dir,
            "main.py",
            "print('new')\n",
            tag="v1.2.3",
        )

        repo_path, fetched_sha = ingester.clone_or_fetch(repo_slug, checkout=False)

        assert repo_path == clone_dir
        assert fetched_sha == new_sha
        assert GitRepo(str(clone_dir)).head.commit.hexsha == old_sha
        assert repo_ref_is_tainted(clone_dir, fetched_sha, {"v1.2.3"})

        ingester.checkout_commit(clone_dir, fetched_sha)
        assert GitRepo(str(clone_dir)).head.commit.hexsha == new_sha

    async def test_ingest_prefetched_repo_ensures_advertised_checkout(self, tmp_path: Path):
        from git import Repo as GitRepo

        repo_slug = "test-org/test-repo"
        source_dir, clone_dir = _create_remote_and_clone(
            tmp_path,
            repo_slug,
            {"main.py": "print('old')\n"},
        )
        new_sha = _commit_and_push(
            source_dir,
            "main.py",
            "print('new')\n",
        )
        config = HubConfig(
            storage=StorageConfig(data_dir=tmp_path / "data"),
            sources=HubConfig().sources.model_copy(update={"repos": [repo_slug]}),
        )
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        repo_path, prefetched_sha = ingester.clone_or_fetch(repo_slug, checkout=False)

        result = await ingester.ingest(
            repos=[repo_slug],
            prefetched={repo_slug: (repo_path, prefetched_sha)},
        )

        assert result.errors == []
        upserted_records = writer.upsert.call_args.args[0]
        assert upserted_records
        assert any("print('new')" in record.content for record in upserted_records)
        assert GitRepo(str(clone_dir)).head.commit.hexsha == new_sha


# ---------------------------------------------------------------------------
# _find_example_dirs tests
# ---------------------------------------------------------------------------


class TestFindExampleDirs:
    """Tests for example directory discovery."""

    def test_finds_direct_example_dirs(self, tmp_path: Path):
        """Finds example dirs that directly contain code files."""
        ex = tmp_path / "examples" / "bot1"
        ex.mkdir(parents=True)
        (ex / "main.py").write_text("print('hello')")

        result = _find_example_dirs(tmp_path)
        assert len(result) == 1
        assert result[0] == ex

    def test_finds_nested_example_dirs(self, tmp_path: Path):
        """Finds subdirs under category dirs (e.g. foundational/)."""
        cat = tmp_path / "examples" / "foundational" / "01-hello"
        cat.mkdir(parents=True)
        (cat / "bot.py").write_text("pass")

        result = _find_example_dirs(tmp_path)
        assert len(result) == 1
        assert result[0].name == "01-hello"

    def test_no_examples_dir_falls_back_to_root(self, tmp_path: Path):
        """Repo with no examples/ dir falls back to root as single example."""
        result = _find_example_dirs(tmp_path)
        assert result == [tmp_path]

    def test_flat_files_in_examples_dir(self, tmp_path: Path):
        """Flat .py files directly in examples/ cause examples/ to be returned."""
        ex = tmp_path / "examples"
        ex.mkdir(parents=True)
        (ex / "single_agent.py").write_text("print('agent')")
        (ex / "two_agents.py").write_text("print('agents')")

        result = _find_example_dirs(tmp_path)
        assert ex in result

    def test_flat_files_mixed_with_subdirs(self, tmp_path: Path):
        """Flat .py files in examples/ alongside subdirectory examples."""
        ex = tmp_path / "examples"
        ex.mkdir(parents=True)
        (ex / "standalone.py").write_text("print('standalone')")
        subdir = ex / "my-bot"
        subdir.mkdir()
        (subdir / "bot.py").write_text("print('bot')")

        result = _find_example_dirs(tmp_path)
        assert subdir in result
        assert ex in result

    def test_skips_pycache(self, tmp_path: Path):
        ex = tmp_path / "examples" / "__pycache__"
        ex.mkdir(parents=True)
        (ex / "cached.pyc").write_text("...")

        result = _find_example_dirs(tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# _iter_code_files tests
# ---------------------------------------------------------------------------


class TestIterCodeFiles:
    """Tests for code file iteration."""

    def test_finds_python_files(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("pass")
        (tmp_path / "config.yaml").write_text("key: val")
        (tmp_path / "readme.md").write_text("# Hi")  # not a code extension

        result = _iter_code_files(tmp_path)
        extensions = {p.suffix for p in result}
        assert ".py" in extensions
        assert ".yaml" in extensions
        assert ".md" not in extensions

    def test_skips_pycache(self, tmp_path: Path):
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-312.pyc").write_text("...")

        result = _iter_code_files(tmp_path)
        assert len(result) == 0

    def test_skips_large_files(self, tmp_path: Path):
        big = tmp_path / "big.py"
        big.write_text("x" * 600_000)

        result = _iter_code_files(tmp_path)
        assert len(result) == 0

    def test_skip_root_dirs_excludes_top_level_tests_and_docs(self, tmp_path: Path):
        """With skip_root_dirs, top-level tests/ and docs/ are excluded."""
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "server.py").write_text("pass")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_server.py").write_text("pass")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "conf.py").write_text("pass")
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push")

        result = _iter_code_files(tmp_path, skip_root_dirs=_ROOT_FALLBACK_SKIP_ROOT_DIRS)
        paths = {str(p.relative_to(tmp_path)) for p in result}
        assert "src/pkg/server.py" in paths
        assert "tests/test_server.py" not in paths
        assert "docs/conf.py" not in paths
        assert ".github/workflows/ci.yml" not in paths

    def test_skip_root_dirs_keeps_src_and_lib(self, tmp_path: Path):
        """Root fallback intentionally keeps src/ and lib/ (source code)."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mod.py").write_text("pass")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "util.py").write_text("pass")

        result = _iter_code_files(tmp_path, skip_root_dirs=_ROOT_FALLBACK_SKIP_ROOT_DIRS)
        names = {p.name for p in result}
        assert "mod.py" in names
        assert "util.py" in names

    def test_skip_root_dirs_keeps_nested_config_module(self, tmp_path: Path):
        """Nested config/ module is NOT excluded — only top-level config/ is."""
        (tmp_path / "src" / "pkg" / "config").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "config" / "settings.py").write_text("DB='postgres'")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "deploy.yaml").write_text("env: prod")

        result = _iter_code_files(tmp_path, skip_root_dirs=_ROOT_FALLBACK_SKIP_ROOT_DIRS)
        paths = {str(p.relative_to(tmp_path)) for p in result}
        assert "src/pkg/config/settings.py" in paths
        assert "config/deploy.yaml" not in paths


# ---------------------------------------------------------------------------
# GitHubRepoIngester tests
# ---------------------------------------------------------------------------


class TestGitHubRepoIngester:
    """Tests for the main ingester class."""

    def _make_config(self, tmp_path: Path, repos: list[str] | None = None) -> HubConfig:
        return HubConfig(
            storage=StorageConfig(data_dir=tmp_path / "data"),
            sources=HubConfig().sources.model_copy(
                update={"repos": repos or ["test-org/test-repo"]}
            ),
        )

    async def test_ingest_processes_example_code(self, tmp_path: Path):
        """Full integration-style test with a real git repo on disk."""
        # Set up a fake repo with an example directory.
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "examples/bot1/main.py": (
                    "import os\n\n"
                    "def run():\n"
                    "    print('hello')\n\n"
                    "if __name__ == '__main__':\n"
                    "    run()\n"
                ),
                "examples/bot1/config.yaml": "name: bot1\n",
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        # Patch clone_or_fetch to return our fake repo.
        from git import Repo as GitRepo

        git_repo = GitRepo(str(repo_dir))
        commit_sha = git_repo.head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        assert result.errors == []
        writer.upsert.assert_called_once()

        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        assert len(records) > 0

        # Verify record fields.
        for rec in records:
            assert rec.content_type == "code"
            assert rec.repo == "test-org/test-repo"
            assert rec.commit_sha == commit_sha
            assert rec.path.startswith("examples/")
            assert rec.metadata["repo"] == "test-org/test-repo"
            assert rec.metadata["commit_sha"] == commit_sha
            assert isinstance(rec.indexed_at, datetime)

    async def test_ingest_idempotent(self, tmp_path: Path):
        """Same commit SHA produces identical chunk IDs."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {"examples/bot1/main.py": "def hello():\n    pass\n"},
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        git_repo = GitRepo(str(repo_dir))
        commit_sha = git_repo.head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            await ingester.ingest()
            await ingester.ingest()

        records1: list[ChunkedRecord] = writer.upsert.call_args_list[0][0][0]
        records2: list[ChunkedRecord] = writer.upsert.call_args_list[1][0][0]

        ids1 = [r.chunk_id for r in records1]
        ids2 = [r.chunk_id for r in records2]
        assert ids1 == ids2

    async def test_ingest_multiple_repos(self, tmp_path: Path):
        """Ingester processes all configured repos."""
        repos = ["org/repo-a", "org/repo-b"]
        config = self._make_config(tmp_path, repos=repos)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        repo_a = _create_fake_repo(
            tmp_path / "repos_a",
            "repo_a",
            {"examples/ex1/main.py": "a = 1\n"},
        )
        repo_b = _create_fake_repo(
            tmp_path / "repos_b",
            "repo_b",
            {"examples/ex1/main.py": "b = 2\n"},
        )

        from git import Repo as GitRepo

        sha_a = GitRepo(str(repo_a)).head.commit.hexsha
        sha_b = GitRepo(str(repo_b)).head.commit.hexsha

        def mock_clone(slug: str) -> tuple[Path, str]:
            if slug == "org/repo-a":
                return repo_a, sha_a
            return repo_b, sha_b

        with patch.object(ingester, "clone_or_fetch", side_effect=mock_clone):
            result = await ingester.ingest()

        assert result.source == "github"
        assert result.records_upserted > 0
        assert result.errors == []
        # upsert called once per repo.
        assert writer.upsert.call_count == 2

    async def test_ingest_clone_failure(self, tmp_path: Path):
        """Clone failure is reported as an error, not an exception."""
        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        with patch.object(
            ingester, "clone_or_fetch", side_effect=RuntimeError("network error")
        ):
            result = await ingester.ingest()

        assert len(result.errors) == 1
        assert "network error" in result.errors[0]
        assert result.records_upserted == 0

    async def test_ingest_src_layout_repo(self, tmp_path: Path):
        """Repo with only src/ dir (no examples/) is indexed via root fallback."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {"src/main.py": "pass\n"},
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        assert result.errors == []
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        assert any(r.path == "src/main.py" for r in records)

    async def test_root_fallback_excludes_tests_and_docs(self, tmp_path: Path):
        """Root-fallback ingestion skips tests/, docs/, .github/ files."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "src/server.py": "def serve(): pass\n",
                "tests/test_server.py": "def test_it(): pass\n",
                "docs/conf.py": "project = 'x'\n",
                ".github/workflows/ci.yml": "on: push\n",
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        paths = {r.path for r in records}
        assert "src/server.py" in paths
        assert "tests/test_server.py" not in paths
        assert "docs/conf.py" not in paths
        assert ".github/workflows/ci.yml" not in paths

    async def test_root_fallback_chunks_have_taxonomy_metadata(self, tmp_path: Path):
        """Root-fallback repos (src/-layout) get execution_mode/capability_tags."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "src/pkg/server.py": (
                    "from pipecat.pipeline import Pipeline\n"
                    "from pipecat.services.deepgram import DeepgramSTTService\n"
                    "def main():\n"
                    "    Pipeline()\n"
                ),
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        assert result.errors == []
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]

        for rec in records:
            assert rec.metadata.get("execution_mode") is not None, (
                f"chunk {rec.path} missing execution_mode"
            )
            assert isinstance(rec.metadata.get("capability_tags"), list), (
                f"chunk {rec.path} missing capability_tags"
            )
        # deepgram is not a cloud tag → execution_mode should be "local"
        assert records[0].metadata["execution_mode"] == "local"
        tag_names = records[0].metadata["capability_tags"]
        assert "deepgram" in tag_names

    async def test_source_url_format(self, tmp_path: Path):
        """Records have correct GitHub blob URLs."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {"examples/bot1/app.py": "x = 1\n"},
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            await ingester.ingest()

        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        for rec in records:
            assert rec.source_url.startswith("https://github.com/test-org/test-repo/blob/")
            assert commit_sha in rec.source_url

    async def test_ingester_implements_protocol(self, tmp_path: Path):
        """GitHubRepoIngester satisfies the Ingester protocol."""
        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        assert hasattr(ingester, "ingest")
        assert callable(ingester.ingest)

    async def test_foundational_nested_examples(self, tmp_path: Path):
        """Discovers nested example dirs (foundational/01-hello pattern)."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "examples/foundational/01-hello/bot.py": "def hello(): pass\n",
                "examples/foundational/02-goodbye/bot.py": "def bye(): pass\n",
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted >= 2
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        paths = {r.path for r in records}
        assert any("01-hello" in p for p in paths)
        assert any("02-goodbye" in p for p in paths)

    async def test_taxonomy_metadata_enrichment(self, tmp_path: Path):
        """Records are enriched with taxonomy-derived metadata."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "examples/foundational/01-hello/bot.py": (
                    "from pipecat.transports.daily import DailyTransport\n"
                    "from pipecat.services.deepgram import DeepgramSTT\n\n"
                    "def main():\n"
                    "    transport = DailyTransport()\n"
                    "    stt = DeepgramSTT()\n"
                ),
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        rec = records[0]

        # Taxonomy should have populated these fields
        assert rec.metadata.get("language") == "python"
        assert rec.metadata.get("foundational_class") == "01-hello"
        assert isinstance(rec.metadata.get("capability_tags"), list)
        # Code imports daily + deepgram, so those tags should be present
        tags = rec.metadata["capability_tags"]
        assert "daily" in tags
        assert "deepgram" in tags
        # Line range metadata should be set
        assert rec.metadata.get("line_start") == 1
        assert isinstance(rec.metadata.get("line_end"), int)
        assert rec.metadata["line_end"] >= 1
        # execution_mode inferred from capability tags:
        # daily tag → cloud
        assert rec.metadata.get("execution_mode") == "cloud"

    async def test_execution_mode_local_when_no_cloud_tags(self, tmp_path: Path):
        """Examples without cloud transport tags get execution_mode='local'."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "examples/foundational/01-hello/bot.py": (
                    "from pipecat.pipeline import Pipeline\n"
                    "def main():\n"
                    "    pipeline = Pipeline([])\n"
                ),
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            await ingester.ingest()

        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        assert records[0].metadata.get("execution_mode") == "local"

    async def test_flat_foundational_files_get_taxonomy(self, tmp_path: Path):
        """Flat .py files in examples/foundational/ get per-file taxonomy metadata."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "examples/foundational/01-say-one-thing.py": (
                    "from pipecat.services.elevenlabs import ElevenLabsTTSService\n"
                    "from pipecat.pipeline.pipeline import Pipeline\n"
                    "async def main():\n"
                    "    pipeline = Pipeline()\n"
                ),
                "examples/foundational/07-interruptible.py": (
                    "from pipecat.transports.daily import DailyTransport\n"
                    "from pipecat.services.deepgram import DeepgramSTTService\n"
                    "class MyBot:\n"
                    "    pass\n"
                ),
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]

        # Build a lookup by path for verification.
        by_path: dict[str, ChunkedRecord] = {}
        for rec in records:
            by_path[rec.path] = rec

        # Flat file 01-say-one-thing.py should have taxonomy metadata.
        say = by_path.get("examples/foundational/01-say-one-thing.py")
        assert say is not None
        assert say.metadata.get("foundational_class") == "01-say-one-thing"
        assert isinstance(say.metadata.get("capability_tags"), list)
        assert "elevenlabs" in say.metadata["capability_tags"]

        # Flat file 07-interruptible.py should have cloud execution_mode.
        inter = by_path.get("examples/foundational/07-interruptible.py")
        assert inter is not None
        assert inter.metadata.get("foundational_class") == "07-interruptible"
        assert "daily" in inter.metadata.get("capability_tags", [])
        assert inter.metadata.get("execution_mode") == "cloud"

    async def test_root_level_example_dirs(self, tmp_path: Path):
        """Repos without examples/ dir discover root-level example dirs."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-examples",
            {
                "chatbot/main.py": (
                    "from pipecat.services.openai import OpenAILLMService\n"
                    "def run(): pass\n"
                ),
                "chatbot/README.md": "# Chatbot\n\nA conversational bot.\n",
                "storytelling/app.py": (
                    "from pipecat.services.anthropic import AnthropicLLMService\n"
                    "def run(): pass\n"
                ),
            },
        )

        config = self._make_config(tmp_path, repos=["test-org/test-examples"])
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        paths = {r.path for r in records}
        assert any("chatbot" in p for p in paths)
        assert any("storytelling" in p for p in paths)


# ---------------------------------------------------------------------------
# Root-level example discovery tests
# ---------------------------------------------------------------------------


class TestDiscoverRootLevelExamples:
    """Tests for _discover_root_level_examples."""

    def test_finds_code_dirs(self, tmp_path: Path):
        """Finds root-level dirs that contain code files."""
        chatbot = tmp_path / "chatbot"
        chatbot.mkdir()
        (chatbot / "main.py").write_text("pass")

        result = _discover_root_level_examples(tmp_path)
        assert len(result) == 1
        assert result[0].name == "chatbot"

    def test_skips_non_example_dirs_falls_back_to_root(self, tmp_path: Path):
        """Skips src, docs, tests, hidden dirs — falls back to repo root."""
        for name in ["src", "docs", "tests", ".github", "__pycache__"]:
            d = tmp_path / name
            d.mkdir()
            (d / "file.py").write_text("pass")

        result = _discover_root_level_examples(tmp_path)
        # Individual filtered dirs are not returned; root is the fallback.
        assert result == [tmp_path]
        assert not any(r.name in {"src", "docs", "tests"} for r in result)

    def test_skips_dirs_without_code_falls_back_to_root(self, tmp_path: Path):
        """Skips dirs that don't contain code files — falls back to repo root."""
        d = tmp_path / "images"
        d.mkdir()
        (d / "logo.png").write_bytes(b"\x89PNG")

        result = _discover_root_level_examples(tmp_path)
        assert result == [tmp_path]

    def test_empty_root_falls_back_to_root(self, tmp_path: Path):
        """Empty repo root falls back to root itself."""
        result = _discover_root_level_examples(tmp_path)
        assert result == [tmp_path]

    def test_no_fallback_when_code_dirs_found(self, tmp_path: Path):
        """Root is NOT included when qualifying subdirs are found."""
        bot = tmp_path / "bot"
        bot.mkdir()
        (bot / "main.py").write_text("pass")

        result = _discover_root_level_examples(tmp_path)
        assert result == [bot]
        assert tmp_path not in result


# ---------------------------------------------------------------------------
# _iter_root_level_code_files tests
# ---------------------------------------------------------------------------


class TestIterRootLevelCodeFiles:
    """Tests for _iter_root_level_code_files (non-recursive)."""

    def test_finds_root_level_code_files(self, tmp_path: Path):
        """Returns code files directly in the directory."""
        (tmp_path / "app.py").write_text("pass")
        (tmp_path / "config.yaml").write_text("key: val")
        (tmp_path / "README.md").write_text("# Hi")

        result = _iter_root_level_code_files(tmp_path)
        names = {p.name for p in result}
        assert "app.py" in names
        assert "config.yaml" in names
        assert "README.md" not in names

    def test_does_not_recurse(self, tmp_path: Path):
        """Does NOT return files in subdirectories."""
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "nested.py").write_text("pass")
        (tmp_path / "root.py").write_text("pass")

        result = _iter_root_level_code_files(tmp_path)
        names = {p.name for p in result}
        assert "root.py" in names
        assert "nested.py" not in names

    def test_skips_large_files(self, tmp_path: Path):
        """Skips files exceeding the size limit."""
        big = tmp_path / "big.py"
        big.write_text("x" * 600_000)

        result = _iter_root_level_code_files(tmp_path)
        assert len(result) == 0

    def test_empty_dir(self, tmp_path: Path):
        result = _iter_root_level_code_files(tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# Root-level file capture in Layout B ingestion
# ---------------------------------------------------------------------------


class TestRootLevelFileCapture:
    """Tests for root-level code file capture in Layout B repos."""

    def _make_config(self, tmp_path: Path, repos: list[str] | None = None) -> HubConfig:
        return HubConfig(
            storage=StorageConfig(data_dir=tmp_path / "data"),
            sources=HubConfig().sources.model_copy(
                update={"repos": repos or ["test-org/test-repo"]}
            ),
        )

    async def test_root_files_captured_alongside_subdirs(self, tmp_path: Path):
        """Root-level code files are indexed alongside subdirectory examples."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "main.py": "def entry(): pass\n",
                "config.yaml": "name: bot\n",
                "processors/video.py": "def process(): pass\n",
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        assert result.errors == []
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        paths = {r.path for r in records}
        # Root-level files captured.
        assert "main.py" in paths
        assert "config.yaml" in paths
        # Subdirectory files also captured.
        assert "processors/video.py" in paths

    async def test_root_files_have_taxonomy_metadata(self, tmp_path: Path):
        """Root-level code files get execution_mode/capability_tags from repo-root taxonomy."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "sidekick.py": (
                    "from pipecat.pipeline import Pipeline\n"
                    "from pipecat.services.deepgram import DeepgramSTTService\n"
                    "def main(): pass\n"
                ),
                "processors/video.py": (
                    "from pipecat.services.openai import OpenAILLMService\n"
                    "def process(): pass\n"
                ),
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        by_path = {r.path: r for r in records}

        # Root-level sidekick.py should have taxonomy metadata.
        root_rec = by_path["sidekick.py"]
        assert root_rec.metadata.get("execution_mode") is not None, (
            "root-level file missing execution_mode"
        )
        assert isinstance(root_rec.metadata.get("capability_tags"), list), (
            "root-level file missing capability_tags"
        )

    async def test_root_files_not_captured_for_examples_layout(self, tmp_path: Path):
        """Layout A repos (with examples/ dir) do NOT capture root-level files."""
        repo_dir = _create_fake_repo(
            tmp_path / "data" / "repos",
            "test-org_test-repo",
            {
                "setup.py": "pass\n",
                "examples/bot1/main.py": "def run(): pass\n",
            },
        )

        config = self._make_config(tmp_path)
        writer = _make_mock_writer()
        ingester = GitHubRepoIngester(config, writer)

        from git import Repo as GitRepo

        commit_sha = GitRepo(str(repo_dir)).head.commit.hexsha

        with patch.object(
            ingester, "clone_or_fetch", return_value=(repo_dir, commit_sha)
        ):
            result = await ingester.ingest()

        assert result.records_upserted > 0
        records: list[ChunkedRecord] = writer.upsert.call_args[0][0]
        paths = {r.path for r in records}
        # Root-level setup.py should NOT be captured for Layout A.
        assert "setup.py" not in paths
        assert "examples/bot1/main.py" in paths


# ---------------------------------------------------------------------------
# Domain inference tests
# ---------------------------------------------------------------------------


class TestInferDomain:
    """Tests for _infer_domain heuristic."""

    def test_python_is_backend(self):
        assert _infer_domain("examples/bot.py", "python") == "backend"

    def test_typescript_is_frontend(self):
        assert _infer_domain("client/app/src/App.tsx", "typescript") == "frontend"

    def test_javascript_is_frontend(self):
        assert _infer_domain("client/index.js", "javascript") == "frontend"

    def test_yaml_is_config(self):
        assert _infer_domain("config.yaml", "yaml") == "config"

    def test_toml_is_config(self):
        assert _infer_domain("pyproject.toml", "toml") == "config"

    def test_json_is_config(self):
        assert _infer_domain("package.json", "json") == "config"

    def test_github_workflow_is_infra(self):
        assert _infer_domain(".github/workflows/ci.yml", "yaml") == "infra"

    def test_ci_directory_is_infra(self):
        assert _infer_domain("ci/deploy.yaml", "yaml") == "infra"

    def test_deploy_directory_is_infra(self):
        assert _infer_domain("deploy/k8s.yaml", "yaml") == "infra"

    def test_docker_compose_is_config(self):
        assert _infer_domain("docker-compose.yml", "yaml") == "config"

    def test_empty_path_defaults_to_backend(self):
        assert _infer_domain("", None) == "backend"

    def test_empty_path_with_python(self):
        assert _infer_domain("", "python") == "backend"

    def test_pcc_deploy_toml_is_config(self):
        assert _infer_domain("server/pcc-deploy.toml", "toml") == "config"
