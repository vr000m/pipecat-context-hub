"""Configuration models for the Pipecat Context Hub.

Defines chunking policies, embedding settings, storage paths, and server config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field

# Environment variable for adding extra repos (comma-separated).
_EXTRA_REPOS_ENV = "PIPECAT_HUB_EXTRA_REPOS"

# Environment variable for skipping entire tainted repos (comma-separated).
_TAINTED_REPOS_ENV = "PIPECAT_HUB_TAINTED_REPOS"

# Environment variable for skipping specific tainted refs.
# Format: org/repo@ref,org/repo@other-ref
_TAINTED_REFS_ENV = "PIPECAT_HUB_TAINTED_REFS"

# Environment variable for enabling cross-encoder reranking.
_RERANKER_ENABLED_ENV = "PIPECAT_HUB_RERANKER_ENABLED"

# Environment variable for selecting the cross-encoder reranker model.
_RERANKER_MODEL_ENV = "PIPECAT_HUB_RERANKER_MODEL"

# Default cross-encoder model used when no override is supplied.
_DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# Environment variable for pinning the framework repo to a specific git tag.
_FRAMEWORK_VERSION_ENV = "PIPECAT_HUB_FRAMEWORK_VERSION"


def _split_csv_env(raw: str) -> list[str]:
    """Split a comma-separated env var into trimmed non-empty entries."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate entries while preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _parse_tainted_refs(raw: str) -> dict[str, list[str]]:
    """Parse org/repo@ref entries from an env var.

    Malformed entries are ignored rather than raising at config-load time.
    """
    parsed: dict[str, list[str]] = {}
    for entry in _split_csv_env(raw):
        if "@" not in entry:
            continue
        repo_slug, ref = entry.rsplit("@", 1)
        repo_slug = repo_slug.strip()
        ref = ref.strip()
        if not repo_slug or not ref:
            continue
        refs = parsed.setdefault(repo_slug, [])
        if ref not in refs:
            refs.append(ref)
    return parsed


class ChunkingConfig(BaseModel):
    """Chunking policies for docs vs code."""

    doc_max_tokens: int = Field(default=512, description="Max tokens per doc chunk.")
    doc_overlap_tokens: int = Field(default=50, description="Token overlap between doc chunks.")
    code_max_tokens: int = Field(default=256, description="Max tokens per code chunk.")
    code_overlap_tokens: int = Field(default=25, description="Token overlap between code chunks.")
    code_prefer_function_boundaries: bool = Field(
        default=True,
        description="Try to split code at function/class boundaries when possible.",
    )


class EmbeddingConfig(BaseModel):
    """Embedding model settings."""

    model_name: str = Field(
        default="all-MiniLM-L6-v2",
        description="Sentence-transformers model for local embeddings.",
    )
    dimension: int = Field(default=384, description="Embedding vector dimension.")


class StorageConfig(BaseModel):
    """Local storage paths."""

    data_dir: Path = Field(
        default=Path.home() / ".pipecat-context-hub",
        description="Root directory for all local data.",
    )
    sqlite_filename: str = Field(default="metadata.db", description="SQLite database filename.")
    chroma_dirname: str = Field(default="chroma", description="ChromaDB persistence directory.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlite_path(self) -> Path:
        """Full path to SQLite database. Included in model_dump()/JSON."""
        return self.data_dir / self.sqlite_filename

    @computed_field  # type: ignore[prop-decorator]
    @property
    def chroma_path(self) -> Path:
        """Full path to ChromaDB directory. Included in model_dump()/JSON."""
        return self.data_dir / self.chroma_dirname


class RerankerConfig(BaseModel):
    """Cross-encoder reranking settings.

    Enable via ``PIPECAT_HUB_RERANKER_ENABLED=1`` environment variable or
    by setting ``enabled=True`` in Python. Disabled by default.
    """

    enabled: bool = Field(
        default=True,
        description="Enable cross-encoder reranking (adds ~50-100ms latency). "
        "Set PIPECAT_HUB_RERANKER_ENABLED=0 to disable via env var.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_enabled(self) -> bool:
        """Check both the field and the environment variable.

        The env var overrides the field in both directions:
        ``PIPECAT_HUB_RERANKER_ENABLED=0`` disables even if ``enabled=True``.
        """
        env = os.environ.get(_RERANKER_ENABLED_ENV, "").strip().lower()
        if env in ("0", "false", "no"):
            return False
        if env in ("1", "true", "yes"):
            return True
        return self.enabled
    cross_encoder_model: str = Field(
        default=_DEFAULT_RERANKER_MODEL,
        description="Cross-encoder model name from sentence-transformers. "
        "Override via PIPECAT_HUB_RERANKER_MODEL env var.",
    )
    top_n: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of top candidates to score with cross-encoder.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_model(self) -> str:
        """Resolve the active reranker model.

        Precedence: ``PIPECAT_HUB_RERANKER_MODEL`` env var >
        ``cross_encoder_model`` field > hardcoded default. An unknown env
        value logs a warning and falls back to the field default — never
        raises, so a misconfigured env var does not block server boot.
        """
        # Imported lazily to avoid import cycles: cross_encoder imports
        # from shared.types, and shared is a sibling package.
        from pipecat_context_hub.services.retrieval.cross_encoder import (
            _ALLOWED_MODELS,
        )

        env_value = os.environ.get(_RERANKER_MODEL_ENV, "").strip()
        if env_value:
            if env_value in _ALLOWED_MODELS:
                return env_value
            import logging

            logging.getLogger(__name__).warning(
                "Unknown %s value '%s' — falling back to '%s'. Allowed: %s",
                _RERANKER_MODEL_ENV,
                env_value,
                self.cross_encoder_model,
                ", ".join(sorted(_ALLOWED_MODELS)),
            )
        if self.cross_encoder_model in _ALLOWED_MODELS:
            return self.cross_encoder_model
        return _DEFAULT_RERANKER_MODEL


class ServerConfig(BaseModel):
    """MCP server settings."""

    transport: Literal["stdio"] = Field(default="stdio", description="Transport type (stdio only in v0).")
    log_level: str = Field(default="INFO", description="Logging level.")


class SourceConfig(BaseModel):
    """Source repositories and docs URL.

    Extra repos can be added via the ``PIPECAT_HUB_EXTRA_REPOS`` environment
    variable (comma-separated slugs, e.g.
    ``PIPECAT_HUB_EXTRA_REPOS="vr000m/decartai-sidekick,vr000m/pipecat-mcp-server"``).
    They are appended to the default repos list.

    Entire repos can be skipped via ``PIPECAT_HUB_TAINTED_REPOS`` and
    specific upstream refs can be skipped via ``PIPECAT_HUB_TAINTED_REFS``
    using ``org/repo@ref`` entries where ``ref`` is a tag or commit SHA/prefix.
    """

    docs_url: str = Field(
        default="https://docs.pipecat.ai/",
        description="Base docs URL (used as canonical source identifier).",
    )
    docs_llms_txt_url: str = Field(
        default="https://docs.pipecat.ai/llms-full.txt",
        description="URL for the pre-rendered llms-full.txt docs file.",
    )
    repos: list[str] = Field(
        default=[
            "pipecat-ai/pipecat",
            "pipecat-ai/pipecat-examples",
            "daily-co/daily-python",
            # Core TypeScript SDKs
            "pipecat-ai/pipecat-client-web",
            "pipecat-ai/pipecat-client-web-transports",
            "pipecat-ai/voice-ui-kit",
            "pipecat-ai/pipecat-flows-editor",
            "pipecat-ai/web-client-ui",
            "pipecat-ai/small-webrtc-prebuilt",
        ],
        description="GitHub repos to ingest.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_repos(self) -> list[str]:
        """Repos list with extra repos appended and tainted repos removed."""
        result = _dedupe_preserve_order(
            list(self.repos) + _split_csv_env(os.environ.get(_EXTRA_REPOS_ENV, ""))
        )
        tainted = set(self.tainted_repos)
        return [slug for slug in result if slug not in tainted]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tainted_repos(self) -> list[str]:
        """Repos explicitly blocked from refresh by local policy."""
        return _dedupe_preserve_order(
            _split_csv_env(os.environ.get(_TAINTED_REPOS_ENV, ""))
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tainted_refs_by_repo(self) -> dict[str, list[str]]:
        """Mapping of repo slug to tainted upstream refs to skip."""
        return _parse_tainted_refs(os.environ.get(_TAINTED_REFS_ENV, ""))


class HubConfig(BaseModel):
    """Top-level configuration for the Pipecat Context Hub."""

    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    sources: SourceConfig = Field(default_factory=SourceConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    framework_version: str | None = Field(
        default=None,
        description="Pin the framework repo (pipecat-ai/pipecat) to a specific git tag "
        "(e.g. 'v0.0.96'). When set, source chunks come from that tag instead of HEAD. "
        "Set via --framework-version CLI flag or PIPECAT_HUB_FRAMEWORK_VERSION env var.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_framework_version(self) -> str | None:
        """Resolve framework version from field or env var.

        CLI flag (stored in ``framework_version``) takes precedence over env var.
        """
        if self.framework_version is not None:
            return self.framework_version
        env = os.environ.get(_FRAMEWORK_VERSION_ENV, "").strip()
        return env or None
