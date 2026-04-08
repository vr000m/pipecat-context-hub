"""Tests for Phase 4: Historical Version Indexing (version-pinned indexing).

Tests cover:
- Config: framework_version field + env var + effective_framework_version
- GitHubRepoIngester: _resolve_tag, clone_or_fetch with tag parameter
- CLI: --framework-version flag propagation
- HubStatusOutput: framework_version field
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester
from pipecat_context_hub.shared.config import (
    HubConfig,
    StorageConfig,
    _FRAMEWORK_VERSION_ENV,
)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestFrameworkVersionConfig:
    """Tests for the framework_version config field and env var."""

    def test_default_is_none(self):
        config = HubConfig()
        assert config.framework_version is None
        assert config.effective_framework_version is None

    def test_explicit_field_value(self):
        config = HubConfig(framework_version="v0.0.96")
        assert config.framework_version == "v0.0.96"
        assert config.effective_framework_version == "v0.0.96"

    def test_env_var_when_field_is_none(self):
        with patch.dict(os.environ, {_FRAMEWORK_VERSION_ENV: "v0.0.95"}):
            config = HubConfig()
            assert config.framework_version is None
            assert config.effective_framework_version == "v0.0.95"

    def test_field_takes_precedence_over_env_var(self):
        with patch.dict(os.environ, {_FRAMEWORK_VERSION_ENV: "v0.0.95"}):
            config = HubConfig(framework_version="v0.0.96")
            assert config.effective_framework_version == "v0.0.96"

    def test_empty_env_var_returns_none(self):
        with patch.dict(os.environ, {_FRAMEWORK_VERSION_ENV: "  "}):
            config = HubConfig()
            assert config.effective_framework_version is None

    def test_model_copy_propagates_version(self):
        config = HubConfig()
        updated = config.model_copy(update={"framework_version": "v0.0.96"})
        assert updated.effective_framework_version == "v0.0.96"
        assert config.effective_framework_version is None  # original unchanged


# ---------------------------------------------------------------------------
# GitHubRepoIngester._resolve_tag tests
# ---------------------------------------------------------------------------


def _create_tagged_repo(tmp_path: Path, tags: list[str]) -> Path:
    """Create a local git repo with the given tags on HEAD."""
    from git import Repo as GitRepo

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_repo = GitRepo.init(str(repo_dir))
    with git_repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "t@t.com")
    (repo_dir / "README.md").write_text("# Test\n")
    git_repo.index.add(["README.md"])
    git_repo.index.commit("initial")
    for tag in tags:
        # Use update_ref to avoid GPG signing issues in CI/local configs
        git_repo.git.update_ref(f"refs/tags/{tag}", "HEAD")
    return repo_dir


class TestResolveTag:
    """Tests for GitHubRepoIngester._resolve_tag."""

    def test_exact_tag_match(self, tmp_path: Path):
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.96"])
        git_repo = GitRepo(str(repo_dir))
        expected_sha = git_repo.head.commit.hexsha

        sha = GitHubRepoIngester._resolve_tag(git_repo, "v0.0.96")
        assert sha == expected_sha

    def test_auto_prefix_v(self, tmp_path: Path):
        """Passing '0.0.96' resolves to tag 'v0.0.96'."""
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.96"])
        git_repo = GitRepo(str(repo_dir))
        expected_sha = git_repo.head.commit.hexsha

        sha = GitHubRepoIngester._resolve_tag(git_repo, "0.0.96")
        assert sha == expected_sha

    def test_strip_v_prefix(self, tmp_path: Path):
        """Passing 'v1.0.0' resolves to tag '1.0.0' when no v-prefix tag exists."""
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["1.0.0"])
        git_repo = GitRepo(str(repo_dir))
        expected_sha = git_repo.head.commit.hexsha

        sha = GitHubRepoIngester._resolve_tag(git_repo, "v1.0.0")
        assert sha == expected_sha

    def test_missing_tag_raises(self, tmp_path: Path):
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, ["v0.0.96"])
        git_repo = GitRepo(str(repo_dir))

        with pytest.raises(ValueError, match="Tag 'v999.0.0' not found"):
            GitHubRepoIngester._resolve_tag(git_repo, "v999.0.0")

    def test_annotated_tag(self, tmp_path: Path):
        """Annotated tags are dereferenced to their commit."""
        from git import Repo as GitRepo

        from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester

        repo_dir = _create_tagged_repo(tmp_path, [])
        git_repo = GitRepo(str(repo_dir))
        expected_sha = git_repo.head.commit.hexsha
        # Create an annotated tag via git command to avoid GPG issues
        git_repo.git.tag("v0.0.50", "-a", "-m", "Release v0.0.50", "--no-sign")

        sha = GitHubRepoIngester._resolve_tag(git_repo, "v0.0.50")
        assert sha == expected_sha


# ---------------------------------------------------------------------------
# clone_or_fetch with tag parameter
# ---------------------------------------------------------------------------


def _make_mock_writer():
    """Create a mock IndexWriter."""
    from unittest.mock import AsyncMock

    writer = AsyncMock()
    writer.upsert = AsyncMock(side_effect=lambda records: len(records))
    writer.delete_by_source = AsyncMock(return_value=0)
    return writer


def _create_remote_and_clone_with_tags(
    tmp_path: Path,
    repo_slug: str,
    files: dict[str, str],
    tags: list[str],
) -> tuple[Path, Path]:
    """Create a source repo with tagged commits, a bare origin, and a local clone."""
    from git import Repo as GitRepo

    # Source repo
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    git_repo = GitRepo.init(str(source_dir))
    with git_repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "t@t.com")
    for rel_path, content in files.items():
        fpath = source_dir / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content)
    git_repo.index.add(list(files.keys()))
    git_repo.index.commit("initial")
    for tag in tags:
        git_repo.git.update_ref(f"refs/tags/{tag}", "HEAD")

    # Bare remote
    bare_remote = tmp_path / "origin.git"
    GitRepo.clone_from(str(source_dir), str(bare_remote), bare=True)
    source_repo = GitRepo(str(source_dir))
    source_repo.create_remote("origin", str(bare_remote))
    branch = source_repo.active_branch.name
    source_repo.git.push("origin", branch, "--tags")

    # Local clone (mimicking the data/repos/<safe_name> layout)
    safe_name = repo_slug.replace("/", "_")
    clone_dir = tmp_path / "data" / "repos" / safe_name
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    GitRepo.clone_from(str(bare_remote), str(clone_dir))

    return source_dir, clone_dir


class TestCloneOrFetchWithTag:
    """Tests for clone_or_fetch with the tag parameter."""

    def test_clone_or_fetch_with_tag_resolves_correct_sha(self, tmp_path: Path):
        from git import Repo as GitRepo

        repo_slug = "test-org/test-repo"
        source_dir, clone_dir = _create_remote_and_clone_with_tags(
            tmp_path, repo_slug, {"main.py": "print('v1')\n"}, ["v0.0.96"],
        )
        # Add a second commit at HEAD (beyond the tag)
        source_repo = GitRepo(str(source_dir))
        (source_dir / "main.py").write_text("print('v2')\n")
        source_repo.index.add(["main.py"])
        source_repo.index.commit("v2 update")
        branch = source_repo.active_branch.name
        source_repo.git.push("origin", branch)

        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())

        # Fetch with tag — should get the tagged commit, not HEAD
        repo_path, tag_sha = ingester.clone_or_fetch(repo_slug, tag="v0.0.96")
        tagged_repo = GitRepo(str(repo_path))
        assert tagged_repo.head.commit.hexsha == tag_sha
        # The file content should be from the tagged version
        assert (repo_path / "main.py").read_text() == "print('v1')\n"

    def test_clone_or_fetch_without_tag_gets_head(self, tmp_path: Path):
        from git import Repo as GitRepo

        repo_slug = "test-org/test-repo"
        source_dir, clone_dir = _create_remote_and_clone_with_tags(
            tmp_path, repo_slug, {"main.py": "print('v1')\n"}, ["v0.0.96"],
        )
        # Add a second commit
        source_repo = GitRepo(str(source_dir))
        (source_dir / "main.py").write_text("print('v2')\n")
        source_repo.index.add(["main.py"])
        source_repo.index.commit("v2 update")
        branch = source_repo.active_branch.name
        source_repo.git.push("origin", branch)

        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())

        # No tag — should get HEAD
        repo_path, head_sha = ingester.clone_or_fetch(repo_slug, tag=None)
        assert (repo_path / "main.py").read_text() == "print('v2')\n"

    def test_invalid_tag_raises_on_fetch(self, tmp_path: Path):
        repo_slug = "test-org/test-repo"
        _create_remote_and_clone_with_tags(
            tmp_path, repo_slug, {"main.py": "x = 1\n"}, ["v0.0.96"],
        )

        config = HubConfig(storage=StorageConfig(data_dir=tmp_path / "data"))
        ingester = GitHubRepoIngester(config, _make_mock_writer())

        with pytest.raises(ValueError, match="not found"):
            ingester.clone_or_fetch(repo_slug, tag="v999.0.0")


# ---------------------------------------------------------------------------
# HubStatusOutput framework_version field
# ---------------------------------------------------------------------------


class TestHubStatusFrameworkVersion:
    """Tests for the framework_version field on HubStatusOutput."""

    def test_default_is_none(self):
        from pipecat_context_hub.shared.types import HubStatusOutput

        output = HubStatusOutput(server_version="0.0.16")
        assert output.framework_version is None

    def test_explicit_value(self):
        from pipecat_context_hub.shared.types import HubStatusOutput

        output = HubStatusOutput(server_version="0.0.16", framework_version="v0.0.96")
        assert output.framework_version == "v0.0.96"

    def test_serialization_round_trip(self):
        from pipecat_context_hub.shared.types import HubStatusOutput

        output = HubStatusOutput(
            server_version="0.0.16",
            framework_version="v0.0.96",
            total_records=100,
        )
        json_str = output.model_dump_json()
        restored = HubStatusOutput.model_validate_json(json_str)
        assert restored.framework_version == "v0.0.96"
