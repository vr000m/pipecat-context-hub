"""Unit tests for CLI helpers."""

from __future__ import annotations

import os
from pathlib import Path

from pipecat_context_hub.cli import _load_dotenv


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
