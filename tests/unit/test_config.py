"""Tests for HubConfig and sub-configs — defaults, serialization, computed fields."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from pipecat_context_hub.shared.config import (
    ChunkingConfig,
    EmbeddingConfig,
    HubConfig,
    ServerConfig,
    SourceConfig,
    StorageConfig,
    _EXTRA_REPOS_ENV,
)


def _round_trip(model_instance):
    """Serialize to JSON and back; assert equality."""
    json_str = model_instance.model_dump_json()
    rebuilt = type(model_instance).model_validate_json(json_str)
    assert rebuilt == model_instance
    return rebuilt


class TestChunkingConfig:
    def test_defaults(self):
        c = ChunkingConfig()
        assert c.doc_max_tokens == 512
        assert c.doc_overlap_tokens == 50
        assert c.code_max_tokens == 256
        assert c.code_overlap_tokens == 25
        assert c.code_prefer_function_boundaries is True

    def test_round_trip(self):
        _round_trip(ChunkingConfig())

    def test_custom_values(self):
        c = ChunkingConfig(doc_max_tokens=1024, code_prefer_function_boundaries=False)
        rebuilt = _round_trip(c)
        assert rebuilt.doc_max_tokens == 1024
        assert rebuilt.code_prefer_function_boundaries is False


class TestEmbeddingConfig:
    def test_defaults(self):
        e = EmbeddingConfig()
        assert e.model_name == "all-MiniLM-L6-v2"
        assert e.dimension == 384

    def test_round_trip(self):
        _round_trip(EmbeddingConfig())


class TestStorageConfig:
    def test_defaults(self):
        s = StorageConfig()
        assert s.data_dir == Path.home() / ".pipecat-context-hub"
        assert s.sqlite_filename == "metadata.db"
        assert s.chroma_dirname == "chroma"

    def test_computed_paths(self):
        s = StorageConfig(data_dir=Path("/tmp/test-hub"))
        assert s.sqlite_path == Path("/tmp/test-hub/metadata.db")
        assert s.chroma_path == Path("/tmp/test-hub/chroma")

    def test_computed_fields_in_model_dump(self):
        """computed_field values must appear in model_dump() for serialization."""
        s = StorageConfig(data_dir=Path("/tmp/test-hub"))
        dumped = s.model_dump()
        assert "sqlite_path" in dumped
        assert "chroma_path" in dumped
        assert dumped["sqlite_path"] == Path("/tmp/test-hub/metadata.db")

    def test_round_trip(self):
        _round_trip(StorageConfig(data_dir=Path("/tmp/test-hub")))


class TestServerConfig:
    def test_defaults(self):
        s = ServerConfig()
        assert s.transport == "stdio"
        assert s.log_level == "INFO"

    def test_round_trip(self):
        _round_trip(ServerConfig())


class TestSourceConfig:
    def test_defaults(self):
        s = SourceConfig()
        assert s.docs_url == "https://docs.pipecat.ai/"
        assert s.docs_llms_txt_url == "https://docs.pipecat.ai/llms-full.txt"
        assert s.repos == ["pipecat-ai/pipecat", "pipecat-ai/pipecat-examples"]

    def test_custom_llms_txt_url(self):
        s = SourceConfig(docs_llms_txt_url="https://example.com/docs.txt")
        assert s.docs_llms_txt_url == "https://example.com/docs.txt"

    def test_round_trip(self):
        _round_trip(SourceConfig())

    def test_effective_repos_without_env(self):
        """Without env var, effective_repos equals repos."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_EXTRA_REPOS_ENV, None)
            s = SourceConfig()
            assert s.effective_repos == s.repos

    def test_effective_repos_with_env(self):
        """Env var appends extra repos to defaults."""
        with patch.dict(os.environ, {_EXTRA_REPOS_ENV: "org/repo-a,org/repo-b"}):
            s = SourceConfig()
            assert s.effective_repos == [
                "pipecat-ai/pipecat",
                "pipecat-ai/pipecat-examples",
                "org/repo-a",
                "org/repo-b",
            ]

    def test_effective_repos_deduplicates(self):
        """Env var duplicates of default repos are ignored."""
        with patch.dict(os.environ, {_EXTRA_REPOS_ENV: "pipecat-ai/pipecat,org/new"}):
            s = SourceConfig()
            assert s.effective_repos == [
                "pipecat-ai/pipecat",
                "pipecat-ai/pipecat-examples",
                "org/new",
            ]

    def test_effective_repos_strips_whitespace(self):
        """Whitespace around slugs is trimmed."""
        with patch.dict(os.environ, {_EXTRA_REPOS_ENV: " org/a , org/b "}):
            s = SourceConfig()
            assert "org/a" in s.effective_repos
            assert "org/b" in s.effective_repos

    def test_effective_repos_ignores_empty_env(self):
        """Empty or whitespace-only env var adds nothing."""
        with patch.dict(os.environ, {_EXTRA_REPOS_ENV: "  "}):
            s = SourceConfig()
            assert s.effective_repos == s.repos


class TestHubConfig:
    def test_defaults(self):
        h = HubConfig()
        assert h.chunking.doc_max_tokens == 512
        assert h.embedding.model_name == "all-MiniLM-L6-v2"
        assert h.storage.sqlite_filename == "metadata.db"
        assert h.server.transport == "stdio"
        assert h.sources.docs_url == "https://docs.pipecat.ai/"

    def test_round_trip(self):
        _round_trip(HubConfig())

    def test_nested_override(self):
        h = HubConfig(
            chunking=ChunkingConfig(doc_max_tokens=1024),
            storage=StorageConfig(data_dir=Path("/tmp/custom")),
            sources=SourceConfig(docs_llms_txt_url="https://example.com/docs.txt"),
        )
        rebuilt = _round_trip(h)
        assert rebuilt.chunking.doc_max_tokens == 1024
        assert rebuilt.storage.data_dir == Path("/tmp/custom")
        assert rebuilt.storage.sqlite_path == Path("/tmp/custom/metadata.db")
        assert rebuilt.sources.docs_llms_txt_url == "https://example.com/docs.txt"
