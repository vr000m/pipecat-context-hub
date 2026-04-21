"""Unit tests for CLI helpers."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from pipecat_context_hub.cli import (
    _load_dotenv,
    _print_refresh_summary,
    _redact_home,
    _safe_hr,
    main,
)
from pipecat_context_hub.shared.config import HubConfig


class TestLoadDotenv:
    """Tests for the .env file parser."""

    def test_basic_unquoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("FOO=bar\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FOO", raising=False)
        _load_dotenv()
        assert os.environ["FOO"] == "bar"

    def test_double_quoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text('KEY="hello world"\n')
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        _load_dotenv()
        assert os.environ["KEY"] == "hello world"

    def test_single_quoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("KEY='hello world'\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        _load_dotenv()
        assert os.environ["KEY"] == "hello world"

    def test_inline_comment_stripped_unquoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("KEY=value # this is a comment\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        _load_dotenv()
        assert os.environ["KEY"] == "value"

    def test_inline_comment_stripped_quoted(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text('KEY="org/a,org/b" # note\n')
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        _load_dotenv()
        assert os.environ["KEY"] == "org/a,org/b"

    def test_hash_inside_quotes_preserved(self, tmp_path: Path, monkeypatch):
        """Hash inside quotes is NOT treated as a comment."""
        (tmp_path / ".env").write_text('KEY="color #fff"\n')
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        _load_dotenv()
        assert os.environ["KEY"] == "color #fff"

    def test_comment_lines_skipped(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("# comment\nKEY=val\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        _load_dotenv()
        assert os.environ["KEY"] == "val"

    def test_empty_lines_skipped(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("\n\nKEY=val\n\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KEY", raising=False)
        _load_dotenv()
        assert os.environ["KEY"] == "val"

    def test_existing_env_not_overwritten(self, tmp_path: Path, monkeypatch):
        (tmp_path / ".env").write_text("KEY=from_file\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KEY", "from_shell")
        _load_dotenv()
        assert os.environ["KEY"] == "from_shell"

    def test_no_env_file(self, tmp_path: Path, monkeypatch):
        """No .env file is fine — no error raised."""
        monkeypatch.chdir(tmp_path)
        _load_dotenv()  # should not raise

    def test_repo_slugs_with_inline_comment(self, tmp_path: Path, monkeypatch):
        """Realistic case: PIPECAT_HUB_EXTRA_REPOS with inline comment."""
        (tmp_path / ".env").write_text(
            'PIPECAT_HUB_EXTRA_REPOS="org/repo-a,org/repo-b" # community repos\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PIPECAT_HUB_EXTRA_REPOS", raising=False)
        _load_dotenv()
        assert os.environ["PIPECAT_HUB_EXTRA_REPOS"] == "org/repo-a,org/repo-b"


class TestRedactHome:
    """Tests for the home-directory redaction helper used in startup telemetry."""

    def test_replaces_home_prefix_with_tilde(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HOME", str(tmp_path))
        nested = tmp_path / "Library" / "Application Support" / "hub" / "data"
        assert _redact_home(nested) == "~" + str(nested)[len(str(tmp_path)):]

    def test_exact_home_path_becomes_tilde(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _redact_home(tmp_path) == "~"

    def test_non_home_path_unchanged(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HOME", str(tmp_path))
        unrelated = Path("/var/lib/hub/data")
        assert _redact_home(unrelated) == "/var/lib/hub/data"

    def test_sibling_of_home_not_redacted(self, monkeypatch, tmp_path: Path):
        # /home/alice should not match /home/alicebob as a prefix.
        monkeypatch.setenv("HOME", str(tmp_path / "alice"))
        (tmp_path / "alice").mkdir()
        sibling = tmp_path / "alicebob" / "data"
        assert _redact_home(sibling) == str(sibling)

    def test_accepts_string_input(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _redact_home(str(tmp_path / "foo")) == "~" + os.sep + "foo"


_DEFAULT_REPOS = HubConfig().sources.repos
_DEFAULT_REPO_COUNT = len(_DEFAULT_REPOS)


def _sha_metadata(sha: str = "abc123", repos: list[str] | None = None) -> dict[str, str]:
    """Build commit_sha metadata dict for all default repos."""
    repos = repos or _DEFAULT_REPOS
    return {f"repo:{r}:commit_sha": sha for r in repos}


class TestRefreshCommand:
    """Tests for the refresh command's incremental skip logic."""

    @pytest.fixture(autouse=True)
    def _mock_deprecation_map(self):
        """Prevent real gh CLI calls and filesystem access during refresh tests."""
        with (
            patch(
                "pipecat_context_hub.services.ingest.deprecation_map.build_deprecation_map_from_source",
                return_value=MagicMock(entries=[], save=MagicMock()),
            ),
            patch(
                "pipecat_context_hub.services.ingest.deprecation_map.build_deprecation_map_from_releases",
                return_value=MagicMock(entries=[], save=MagicMock()),
            ),
            patch(
                "pipecat_context_hub.services.ingest.deprecation_map.build_deprecation_map_from_changelog",
                return_value=MagicMock(entries=[], save=MagicMock()),
            ),
        ):
            yield

    def _make_mocks(self):
        """Create shared mock objects for refresh tests."""
        mock_index_store = MagicMock()
        mock_index_store.get_metadata = MagicMock(return_value=None)
        mock_index_store.set_metadata = MagicMock()
        mock_index_store.delete_metadata = MagicMock()
        mock_index_store.get_all_metadata = MagicMock(return_value={})
        mock_index_store.delete_by_content_type = AsyncMock(return_value=0)
        mock_index_store.delete_by_repo = AsyncMock(return_value=0)
        mock_index_store.get_index_stats = MagicMock(return_value={
            "counts_by_type": {"doc": 100, "code": 200},
            "total": 300,
            "commit_shas": [],
        })
        mock_index_store.reset = MagicMock()
        mock_index_store.close = MagicMock()

        mock_crawler = MagicMock()
        mock_crawler.fetch_llms_txt = AsyncMock(return_value="# Page\nSource: https://example.com\nContent here")
        mock_crawler.ingest = AsyncMock(return_value=MagicMock(records_upserted=10, errors=[]))
        mock_crawler.close = AsyncMock()

        mock_github = MagicMock()
        mock_github.clone_or_fetch = MagicMock(return_value=(Path("/tmp/repo"), "abc123"))
        mock_github.ingest = AsyncMock(return_value=MagicMock(records_upserted=20, errors=[]))

        mock_source_ingester = MagicMock()
        mock_source_ingester.ingest = AsyncMock(return_value=MagicMock(records_upserted=5, errors=[]))

        return mock_index_store, mock_crawler, mock_github, mock_source_ingester

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_force_flag_bypasses_skip(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """--force bypasses all skip logic even when hashes match."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Simulate matching hash/SHA (would skip without --force)
        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--force"])

        assert result.exit_code == 0
        # With --force, docs should be re-ingested despite matching hash
        mock_crawler.ingest.assert_called_once()
        # With --force, repos should be re-ingested despite matching SHA
        assert mock_github.ingest.call_count == _DEFAULT_REPO_COUNT

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_skip_when_sha_matches(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """Refresh skips unchanged sources when hashes/SHAs match."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # Docs should be skipped (matching hash)
        mock_crawler.ingest.assert_not_called()
        # Repos should be skipped (matching SHA)
        mock_github.ingest.assert_not_called()
        mock_source.ingest.assert_not_called()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_recovered_repo_forces_reingest_even_when_sha_matches(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """A repo whose corrupt clone was recovered must be re-ingested even
        when its remote SHA matches the stored one — otherwise the index
        keeps reflecting the empty/broken prior state."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Mark every configured repo as recovered this run.
        config = HubConfig()
        recovered = set(config.sources.effective_repos)
        mock_github.recovered_repos = recovered

        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0, result.output
        # Docs hash still matches — docs path untouched.
        mock_crawler.ingest.assert_not_called()
        # But every recovered repo must be re-ingested despite matching SHA.
        assert mock_github.ingest.call_count == _DEFAULT_REPO_COUNT
        assert mock_source.ingest.call_count == _DEFAULT_REPO_COUNT

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_full_ingest_when_sha_differs(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """Refresh re-ingests when stored SHA differs from current."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Stored SHA is old, current is different
        meta = {"docs:content_hash": "old-hash", **_sha_metadata("old-sha")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # Different hash → docs re-ingested
        mock_crawler.ingest.assert_called_once()
        # Different SHA → repos re-ingested (once per changed repo)
        assert mock_github.ingest.call_count == _DEFAULT_REPO_COUNT

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_docs_hash_not_stored_on_ingest_error(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """Docs content hash is not cached when ingest returns errors."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Docs ingest returns errors (e.g. upsert failure)
        mock_crawler.ingest = AsyncMock(
            return_value=MagicMock(records_upserted=0, errors=["Upsert failed"]),
        )
        # Repos unchanged so they don't interfere
        mock_store.get_metadata = MagicMock(side_effect=lambda key: _sha_metadata("abc123").get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # docs:content_hash should NOT have been stored
        set_calls = {
            call.args[0] for call in mock_store.set_metadata.call_args_list
        }
        assert "docs:content_hash" not in set_calls

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_repo_sha_not_stored_on_ingest_error(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """Repo commit SHA is not cached when code/source ingest has errors."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # GitHub ingest returns errors for any repo
        mock_github.ingest = AsyncMock(
            return_value=MagicMock(records_upserted=0, errors=["clone failed"]),
        )
        # Docs unchanged
        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        mock_store.get_metadata = MagicMock(side_effect=lambda key: {
            "docs:content_hash": content_hash,
        }.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # repo:*:commit_sha should NOT have been stored for changed repos with errors
        set_calls = {
            call.args[0] for call in mock_store.set_metadata.call_args_list
        }
        for repo in _DEFAULT_REPOS:
            assert f"repo:{repo}:commit_sha" not in set_calls
        # Failed repos should have their cached SHA deleted (P1)
        delete_calls = {
            call.args[0] for call in mock_store.delete_metadata.call_args_list
        }
        for repo in _DEFAULT_REPOS:
            assert f"repo:{repo}:commit_sha" in delete_calls

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_force_failed_repo_invalidates_cached_sha(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """--force with ingest failure deletes cached SHA so next refresh retries."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # GitHub ingest fails
        mock_github.ingest = AsyncMock(
            return_value=MagicMock(records_upserted=0, errors=["transient error"]),
        )
        # SHA matches (would skip without --force), but --force overrides
        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--force"])

        assert result.exit_code == 0
        # Failed repos should have cached SHA deleted, not preserved
        delete_calls = {
            call.args[0] for call in mock_store.delete_metadata.call_args_list
        }
        for repo in _DEFAULT_REPOS:
            assert f"repo:{repo}:commit_sha" in delete_calls

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_removed_repo_cleaned_up(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """Repos no longer in effective_repos have their data and SHA cleaned up."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        # Simulate a previously-indexed repo that is no longer configured
        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        all_meta = {**_sha_metadata("abc123"), "repo:old-org/removed-repo:commit_sha": "def456"}
        mock_store.get_all_metadata = MagicMock(return_value=all_meta)
        meta = {"docs:content_hash": content_hash, **all_meta}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # The removed repo should be cleaned up
        mock_store.delete_by_repo.assert_any_call("old-org/removed-repo")
        mock_store.delete_metadata.assert_any_call("repo:old-org/removed-repo:commit_sha")

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    @patch("pipecat_context_hub.cli._delete_local_index_storage")
    def test_reset_index_forces_full_rebuild(
        self, mock_delete_storage, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """--reset-index should wipe state and force a full re-ingest."""
        events: list[str] = []
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_delete_storage.side_effect = lambda *_args, **_kwargs: events.append("delete")

        def _record_store(*_args, **_kwargs):
            events.append("store")
            return mock_store

        mock_is_cls.side_effect = _record_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_ref_tainted.return_value = False

        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--reset-index"])

        assert result.exit_code == 0
        mock_delete_storage.assert_called_once()
        assert events[:2] == ["delete", "store"]
        mock_store.reset.assert_not_called()
        mock_crawler.ingest.assert_called_once()
        assert mock_github.ingest.call_count == _DEFAULT_REPO_COUNT
        mock_store.close.assert_called_once()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_tainted_ref_skips_refresh_and_keeps_last_known_good(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """A tainted upstream HEAD should be skipped without deleting a safe cached SHA."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_github.clone_or_fetch.side_effect = lambda repo_slug, _checkout=False, tag=None: (
            Path(f"/tmp/{repo_slug.replace('/', '_')}"),
            "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
        )
        mock_ref_tainted.side_effect = lambda _repo_path, sha, _refs: sha == "badcafe"

        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        # Override pipecat with a good known SHA (not the tainted one)
        meta["repo:pipecat-ai/pipecat:commit_sha"] = "good123"
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REFS", "pipecat-ai/pipecat@badcafe")
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        mock_github.ingest.assert_not_called()
        mock_source.ingest.assert_not_called()
        mock_store.delete_by_repo.assert_not_called()
        # delete_metadata should not have been called for any repo SHA keys;
        # the only allowed call is clearing framework_version when not pinned.
        for call in mock_store.delete_metadata.call_args_list:
            assert call.args[0] == "framework_version", (
                f"Unexpected delete_metadata call: {call.args[0]}"
            )
        set_calls = {
            call.args[0] for call in mock_store.set_metadata.call_args_list
        }
        assert "repo:pipecat-ai/pipecat:commit_sha" not in set_calls

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.github_ingest.repo_ref_is_tainted")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_tainted_ref_removes_indexed_tainted_sha(
        self, mock_si_cls, mock_ref_tainted, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """If the cached SHA is also tainted, local records are removed."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source
        mock_github.clone_or_fetch.side_effect = lambda repo_slug, _checkout=False, tag=None: (
            Path(f"/tmp/{repo_slug.replace('/', '_')}"),
            "badcafe" if repo_slug == "pipecat-ai/pipecat" else "abc123",
        )
        mock_ref_tainted.side_effect = lambda _repo_path, sha, _refs: sha == "badcafe"

        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"docs:content_hash": content_hash, **_sha_metadata("abc123")}
        # Override pipecat with the tainted SHA to trigger removal
        meta["repo:pipecat-ai/pipecat:commit_sha"] = "badcafe"
        mock_store.get_metadata = MagicMock(side_effect=lambda key: meta.get(key))

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PIPECAT_HUB_TAINTED_REFS", "pipecat-ai/pipecat@badcafe")
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        mock_store.delete_by_repo.assert_any_call("pipecat-ai/pipecat")
        mock_store.delete_metadata.assert_any_call("repo:pipecat-ai/pipecat:commit_sha")
        mock_github.ingest.assert_not_called()
        mock_source.ingest.assert_not_called()
        set_calls = {
            call.args[0] for call in mock_store.set_metadata.call_args_list
        }
        assert "repo:pipecat-ai/pipecat:commit_sha" not in set_calls


class TestServeEmptyIndex:
    """Serve must fail fast on empty or unopenable indexes rather than hang."""

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_empty_index_exits_nonzero(self, mock_is_cls, tmp_path, monkeypatch):
        mock_store = MagicMock()
        mock_store.get_index_stats = MagicMock(return_value={
            "counts_by_type": {}, "total": 0, "commit_shas": [],
        })
        mock_store.close = MagicMock()
        mock_is_cls.return_value = mock_store

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 2
        mock_store.close.assert_called_once()

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_open_failure_exits_nonzero(self, mock_is_cls, tmp_path, monkeypatch):
        mock_is_cls.side_effect = RuntimeError("corrupt sqlite")

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 2

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_stats_failure_closes_store(self, mock_is_cls, tmp_path, monkeypatch):
        """If IndexStore opens but get_index_stats raises, close() is called."""
        mock_store = MagicMock()
        mock_store.get_index_stats = MagicMock(side_effect=RuntimeError("fts broken"))
        mock_store.close = MagicMock()
        mock_is_cls.return_value = mock_store

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code == 2
        mock_store.close.assert_called_once()

    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.index.store.IndexStore")
    def test_post_open_exception_closes_store(
        self, mock_is_cls, mock_es_cls, tmp_path, monkeypatch
    ):
        """An exception after successful open must still close the store."""
        mock_store = MagicMock()
        mock_store.get_index_stats = MagicMock(return_value={
            "counts_by_type": {"doc": 1}, "total": 1, "commit_shas": [],
        })
        mock_store.close = MagicMock()
        mock_is_cls.return_value = mock_store
        mock_es_cls.side_effect = RuntimeError("embedding model missing")

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["serve"])

        assert result.exit_code != 0
        mock_store.close.assert_called_once()


class TestSafeHr:
    def test_utf8_returns_box_drawing(self, monkeypatch):
        fake_stdout = MagicMock()
        fake_stdout.encoding = "utf-8"
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(5) == "\u2500" * 5

    def test_cp1252_falls_back_to_ascii(self, monkeypatch):
        fake_stdout = MagicMock()
        fake_stdout.encoding = "cp1252"
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(4) == "----"

    def test_cp1254_falls_back_to_ascii(self, monkeypatch):
        fake_stdout = MagicMock()
        fake_stdout.encoding = "cp1254"
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(3) == "---"

    def test_cp437_keeps_box_drawing(self, monkeypatch):
        """cp437 is an OEM codepage that does include U+2500 — keep the glyph."""
        fake_stdout = MagicMock()
        fake_stdout.encoding = "cp437"
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(3) == "\u2500\u2500\u2500"

    def test_missing_encoding_falls_back_to_ascii(self, monkeypatch):
        fake_stdout = MagicMock(spec=[])  # no .encoding attribute
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", fake_stdout)
        assert _safe_hr(2) == "--"


class TestPrintRefreshSummaryEncoding:
    def test_does_not_raise_on_cp1254(self, monkeypatch):
        import io

        cp1254_stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1254", errors="strict", write_through=True
        )
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", cp1254_stdout)

        source_status: dict[str, dict[str, str | int]] = {
            "pipecat-ai/pipecat": {
                "status": "updated",
                "sha": "abcdef12",
                "existing": 100,
                "updated": 200,
            },
        }
        # Should not raise UnicodeEncodeError; _safe_hr falls back to ASCII.
        _print_refresh_summary(source_status, 200, 0, 1.2)
        cp1254_stdout.flush()
        raw = cp1254_stdout.buffer.getvalue().decode("cp1254")
        assert "\u2500" not in raw
        assert "-" * 8 in raw

    def test_does_not_raise_on_cp437_with_placeholder_rows(self, monkeypatch):
        """cp437 cannot encode U+2014 em dash — every placeholder must fall back."""
        import io

        cp437_stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp437", errors="strict", write_through=True
        )
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", cp437_stdout)

        # Exercise every placeholder code path: docs row (sha="—"),
        # skipped repo (updated="—"), error repo, and zero-existing repo.
        source_status: dict[str, dict[str, str | int]] = {
            "docs.pipecat.ai": {
                "status": "updated",
                "sha": "\u2014",
                "existing": 0,
                "updated": 500,
            },
            "pipecat-ai/pipecat": {
                "status": "skipped",
                "sha": "abcdef12",
                "existing": 1000,
                "updated": "\u2014",
            },
            "pipecat-ai/other": {
                "status": "error",
                "sha": "\u2014",
                "existing": 0,
                "updated": "\u2014",
            },
        }
        # No exception — and the em dash (which cp437 cannot encode) must
        # have been swapped for an ASCII placeholder on every row.
        _print_refresh_summary(source_status, 500, 1, 2.3)
        cp437_stdout.flush()
        raw = cp437_stdout.buffer.getvalue().decode("cp437")
        assert "\u2014" not in raw

    def test_non_encodable_sha_value_normalized(self, monkeypatch):
        """Any non-encodable cell value — not just the current em-dash
        sentinel — must be swapped for the ASCII placeholder. Guards
        against sentinel-drift silently re-introducing the crash."""
        import io

        cp437_stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp437", errors="strict", write_through=True
        )
        monkeypatch.setattr("pipecat_context_hub.cli.sys.stdout", cp437_stdout)

        # U+2026 (ellipsis) is not encodable in cp437 either; use it as a
        # stand-in for any future sentinel drift.
        source_status: dict[str, dict[str, str | int]] = {
            "some-source": {
                "status": "updated",
                "sha": "\u2026",
                "existing": 10,
                "updated": 20,
            },
        }
        _print_refresh_summary(source_status, 20, 0, 1.0)
        cp437_stdout.flush()
        raw = cp437_stdout.buffer.getvalue().decode("cp437")
        assert "\u2026" not in raw

    def test_recovered_repos_surfaced_in_summary(self, capsys):
        source_status: dict[str, dict[str, str | int]] = {
            "pipecat-ai/pipecat": {
                "status": "updated",
                "sha": "abcdef12",
                "existing": 0,
                "updated": 5,
            },
        }
        _print_refresh_summary(
            source_status,
            5,
            0,
            1.0,
            recovered_repos=["pipecat-ai/pipecat"],
        )
        out = capsys.readouterr().out
        assert "Recovered 1 corrupt clone(s)" in out
        assert "pipecat-ai/pipecat" in out
