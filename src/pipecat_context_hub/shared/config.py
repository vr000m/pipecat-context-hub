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
        default=["pipecat-ai/pipecat", "pipecat-ai/pipecat-examples"],
        description="GitHub repos to ingest.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_repos(self) -> list[str]:
        """Repos list with any ``PIPECAT_HUB_EXTRA_REPOS`` entries appended."""
        extra = os.environ.get(_EXTRA_REPOS_ENV, "").strip()
        if not extra:
            return list(self.repos)
        extra_slugs = [s.strip() for s in extra.split(",") if s.strip()]
        # Deduplicate while preserving order.
        seen = set(self.repos)
        result = list(self.repos)
        for slug in extra_slugs:
            if slug not in seen:
                seen.add(slug)
                result.append(slug)
        return result


class HubConfig(BaseModel):
    """Top-level configuration for the Pipecat Context Hub."""

    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    sources: SourceConfig = Field(default_factory=SourceConfig)
