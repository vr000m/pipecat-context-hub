"""Unit tests for CLI helpers."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from pipecat_context_hub.cli import _load_dotenv, main


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


class TestRefreshCommand:
    """Tests for the refresh command's incremental skip logic."""

    def _make_mocks(self):
        """Create shared mock objects for refresh tests."""
        mock_index_store = MagicMock()
        mock_index_store.get_metadata = MagicMock(return_value=None)
        mock_index_store.set_metadata = MagicMock()
        mock_index_store.delete_by_content_type = AsyncMock(return_value=0)
        mock_index_store.delete_by_repo = AsyncMock(return_value=0)
        mock_index_store.get_index_stats = MagicMock(return_value={
            "counts_by_type": {"doc": 100, "code": 200},
            "total": 300,
            "commit_shas": [],
        })
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
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_force_flag_bypasses_skip(
        self, mock_si_cls, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """--force bypasses all skip logic even when hashes match."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source

        # Simulate matching hash/SHA (would skip without --force)
        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        mock_store.get_metadata = MagicMock(side_effect=lambda key: {
            "docs:content_hash": content_hash,
            "repo:pipecat-ai/pipecat:commit_sha": "abc123",
            "repo:pipecat-ai/pipecat-examples:commit_sha": "abc123",
        }.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh", "--force"])

        assert result.exit_code == 0
        # With --force, docs should be re-ingested despite matching hash
        mock_crawler.ingest.assert_called_once()
        # With --force, repos should be re-ingested despite matching SHA
        # Ingest is called once per changed repo (2 default repos)
        assert mock_github.ingest.call_count == 2

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_skip_when_sha_matches(
        self, mock_si_cls, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """Refresh skips unchanged sources when hashes/SHAs match."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source

        import hashlib
        content = "# Page\nSource: https://example.com\nContent here"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        mock_store.get_metadata = MagicMock(side_effect=lambda key: {
            "docs:content_hash": content_hash,
            "repo:pipecat-ai/pipecat:commit_sha": "abc123",
            "repo:pipecat-ai/pipecat-examples:commit_sha": "abc123",
        }.get(key))

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
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_full_ingest_when_sha_differs(
        self, mock_si_cls, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """Refresh re-ingests when stored SHA differs from current."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source

        # Stored SHA is old, current is different
        mock_store.get_metadata = MagicMock(side_effect=lambda key: {
            "docs:content_hash": "old-hash",
            "repo:pipecat-ai/pipecat:commit_sha": "old-sha",
            "repo:pipecat-ai/pipecat-examples:commit_sha": "old-sha",
        }.get(key))

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["refresh"])

        assert result.exit_code == 0
        # Different hash → docs re-ingested
        mock_crawler.ingest.assert_called_once()
        # Different SHA → repos re-ingested (once per changed repo)
        assert mock_github.ingest.call_count == 2

    @patch("pipecat_context_hub.services.index.store.IndexStore")
    @patch("pipecat_context_hub.services.embedding.EmbeddingService")
    @patch("pipecat_context_hub.services.embedding.EmbeddingIndexWriter")
    @patch("pipecat_context_hub.services.ingest.docs_crawler.DocsCrawler")
    @patch("pipecat_context_hub.services.ingest.github_ingest.GitHubRepoIngester")
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_docs_hash_not_stored_on_ingest_error(
        self, mock_si_cls, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """Docs content hash is not cached when ingest returns errors."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source

        # Docs ingest returns errors (e.g. upsert failure)
        mock_crawler.ingest = AsyncMock(
            return_value=MagicMock(records_upserted=0, errors=["Upsert failed"]),
        )
        # Repos unchanged so they don't interfere
        mock_store.get_metadata = MagicMock(side_effect=lambda key: {
            "repo:pipecat-ai/pipecat:commit_sha": "abc123",
            "repo:pipecat-ai/pipecat-examples:commit_sha": "abc123",
        }.get(key))

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
    @patch("pipecat_context_hub.services.ingest.source_ingest.SourceIngester")
    def test_repo_sha_not_stored_on_ingest_error(
        self, mock_si_cls, mock_gh_cls, mock_dc_cls,
        mock_eiw_cls, mock_es_cls, mock_is_cls,
        tmp_path, monkeypatch,
    ):
        """Repo commit SHA is not cached when code/source ingest has errors."""
        mock_store, mock_crawler, mock_github, mock_source = self._make_mocks()
        mock_is_cls.return_value = mock_store
        mock_dc_cls.return_value = mock_crawler
        mock_gh_cls.return_value = mock_github
        mock_si_cls.return_value = mock_source

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
        assert "repo:pipecat-ai/pipecat:commit_sha" not in set_calls
        assert "repo:pipecat-ai/pipecat-examples:commit_sha" not in set_calls
