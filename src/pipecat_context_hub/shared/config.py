"""Configuration models for the Pipecat Context Hub.

Defines chunking policies, embedding settings, storage paths, and server config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field


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
    """Source repositories and docs URL."""

    docs_url: str = Field(
        default="https://docs.pipecat.ai/", description="Primary docs site to crawl."
    )
    repos: list[str] = Field(
        default=["pipecat-ai/pipecat", "pipecat-ai/pipecat-examples"],
        description="GitHub repos to ingest.",
    )
    deepwiki_enabled: bool = Field(
        default=False, description="Whether to ingest DeepWiki as secondary source."
    )
    deepwiki_urls: list[str] = Field(
        default_factory=list, description="DeepWiki URL allowlist (if enabled)."
    )


class HubConfig(BaseModel):
    """Top-level configuration for the Pipecat Context Hub."""

    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    sources: SourceConfig = Field(default_factory=SourceConfig)
