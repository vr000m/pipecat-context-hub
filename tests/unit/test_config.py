"""Tests for HubConfig and sub-configs — defaults, serialization, computed fields."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from pipecat_context_hub.shared.config import (
    ChunkingConfig,
    EmbeddingConfig,
    HubConfig,
    RerankerConfig,
    ServerConfig,
    SourceConfig,
    StorageConfig,
    _DEFAULT_RERANKER_MODEL,
    _EXTRA_REPOS_ENV,
    _RERANKER_MODEL_ENV,
    _TAINTED_REFS_ENV,
    _TAINTED_REPOS_ENV,
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
        assert s.repos == [
            "pipecat-ai/pipecat",
            "pipecat-ai/pipecat-examples",
            "daily-co/daily-python",
            "pipecat-ai/pipecat-client-web",
            "pipecat-ai/pipecat-client-web-transports",
            "pipecat-ai/voice-ui-kit",
            "pipecat-ai/pipecat-flows-editor",
            "pipecat-ai/web-client-ui",
            "pipecat-ai/small-webrtc-prebuilt",
        ]

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
            assert s.effective_repos == s.repos + ["org/repo-a", "org/repo-b"]

    def test_effective_repos_deduplicates(self):
        """Env var duplicates of default repos are ignored."""
        with patch.dict(os.environ, {_EXTRA_REPOS_ENV: "pipecat-ai/pipecat,org/new"}):
            s = SourceConfig()
            assert s.effective_repos == s.repos + ["org/new"]

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

    def test_effective_repos_excludes_tainted_repos(self):
        """Tainted repos are removed from the effective refresh list."""
        with patch.dict(
            os.environ,
            {
                _EXTRA_REPOS_ENV: "org/repo-a",
                _TAINTED_REPOS_ENV: "pipecat-ai/pipecat,org/repo-a",
            },
        ):
            s = SourceConfig()
            expected = [r for r in s.repos if r != "pipecat-ai/pipecat"]
            assert s.effective_repos == expected
            assert s.tainted_repos == ["pipecat-ai/pipecat", "org/repo-a"]

    def test_tainted_refs_by_repo_parses_env(self):
        """Tainted refs are parsed from org/repo@ref entries."""
        with patch.dict(
            os.environ,
            {_TAINTED_REFS_ENV: "pipecat-ai/pipecat@v0.0.9,pipecat-ai/pipecat@deadbeef,broken-entry"},
        ):
            s = SourceConfig()
            assert s.tainted_refs_by_repo == {
                "pipecat-ai/pipecat": ["v0.0.9", "deadbeef"],
            }


class TestRerankerConfigEffectiveModel:
    """Env-var resolution for PIPECAT_HUB_RERANKER_MODEL."""

    _ALT_MODEL = "cross-encoder/ms-marco-MiniLM-L-12-v2"
    _TINY_MODEL = "cross-encoder/ms-marco-TinyBERT-L-2-v2"

    def test_unset_returns_field_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_RERANKER_MODEL_ENV, None)
            assert RerankerConfig().effective_model == _DEFAULT_RERANKER_MODEL

    def test_env_selects_allowed_model(self):
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: self._ALT_MODEL}):
            assert RerankerConfig().effective_model == self._ALT_MODEL

    def test_env_tiny_model(self):
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: self._TINY_MODEL}):
            assert RerankerConfig().effective_model == self._TINY_MODEL

    def test_invalid_env_falls_back_to_field(self, caplog):
        # Field is the default (valid), so fallback is the default.
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: "cross-encoder/not-real"}):
            with caplog.at_level("WARNING"):
                model = RerankerConfig().effective_model
        assert model == _DEFAULT_RERANKER_MODEL
        # Warning must name the actual fallback target, not the invalid env value.
        unknown_msgs = [r.getMessage() for r in caplog.records if "Unknown" in r.getMessage()]
        assert any(_DEFAULT_RERANKER_MODEL in m for m in unknown_msgs)

    def test_invalid_env_and_invalid_field_warn_with_accurate_target(self, caplog):
        # Both env and field are invalid — fallback must be the hardcoded
        # default, and the env-warning must name that (not the bad field).
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: "cross-encoder/bad-env"}):
            cfg = RerankerConfig(cross_encoder_model="cross-encoder/bad-field")
            with caplog.at_level("WARNING"):
                model = cfg.effective_model
        assert model == _DEFAULT_RERANKER_MODEL
        messages = [r.getMessage() for r in caplog.records]
        # Env-fallback warning names default (not the invalid field).
        env_warn = next(m for m in messages if "bad-env" in m)
        assert _DEFAULT_RERANKER_MODEL in env_warn
        assert "bad-field" not in env_warn
        # Field-invalid warning is also emitted.
        assert any("bad-field" in m and "not allowlisted" in m for m in messages)

    def test_empty_env_uses_field(self):
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: "   "}):
            assert RerankerConfig().effective_model == _DEFAULT_RERANKER_MODEL

    def test_env_is_whitespace_trimmed(self):
        with patch.dict(os.environ, {_RERANKER_MODEL_ENV: f"  {self._ALT_MODEL}  "}):
            assert RerankerConfig().effective_model == self._ALT_MODEL

    def test_invalid_field_unset_env_falls_back_to_default(self, caplog):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_RERANKER_MODEL_ENV, None)
            cfg = RerankerConfig(cross_encoder_model="cross-encoder/not-real")
            with caplog.at_level("WARNING"):
                assert cfg.effective_model == _DEFAULT_RERANKER_MODEL
        assert any(
            "not-real" in r.getMessage() and "not allowlisted" in r.getMessage()
            for r in caplog.records
        )


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
