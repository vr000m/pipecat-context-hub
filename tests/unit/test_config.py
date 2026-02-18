"""Tests for HubConfig and sub-configs — defaults, serialization, computed fields."""

from __future__ import annotations

from pathlib import Path

from pipecat_context_hub.shared.config import (
    ChunkingConfig,
    EmbeddingConfig,
    HubConfig,
    ServerConfig,
    SourceConfig,
    StorageConfig,
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
        assert s.deepwiki_enabled is False
        assert s.deepwiki_urls == []

    def test_custom_llms_txt_url(self):
        s = SourceConfig(docs_llms_txt_url="https://example.com/docs.txt")
        assert s.docs_llms_txt_url == "https://example.com/docs.txt"

    def test_round_trip(self):
        _round_trip(SourceConfig())


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
            sources=SourceConfig(deepwiki_enabled=True),
        )
        rebuilt = _round_trip(h)
        assert rebuilt.chunking.doc_max_tokens == 1024
        assert rebuilt.storage.data_dir == Path("/tmp/custom")
        assert rebuilt.storage.sqlite_path == Path("/tmp/custom/metadata.db")
        assert rebuilt.sources.deepwiki_enabled is True
