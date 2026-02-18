"""Integration test fixtures for the Pipecat Context Hub.

Provides real (non-mock) instances of IndexStore, EmbeddingService, and
HybridRetriever backed by a temporary directory that is cleaned up after
each test session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipecat_context_hub.services.embedding import (
    EmbeddingIndexWriter,
    EmbeddingService,
)
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever
from pipecat_context_hub.shared.config import EmbeddingConfig, StorageConfig


@pytest.fixture(scope="session")
def embedding_service():
    """Session-scoped embedding service (model loaded once)."""
    return EmbeddingService(EmbeddingConfig())


@pytest.fixture()
def tmp_storage_config(tmp_path: Path):
    """Per-test temporary storage config."""
    return StorageConfig(data_dir=tmp_path / "data")


@pytest.fixture()
def index_store(tmp_storage_config: StorageConfig):
    """Per-test IndexStore backed by tmp dir."""
    store = IndexStore(tmp_storage_config)
    yield store
    store.close()


@pytest.fixture()
def embedding_writer(index_store: IndexStore, embedding_service: EmbeddingService):
    """IndexWriter that auto-computes embeddings before upsert."""
    return EmbeddingIndexWriter(index_store, embedding_service)


@pytest.fixture()
def retriever(index_store: IndexStore, embedding_service: EmbeddingService):
    """HybridRetriever wired to real index + embeddings."""
    return HybridRetriever(index_store, embedding_service)
