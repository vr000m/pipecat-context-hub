"""Embedding service for the Pipecat Context Hub.

Provides ``EmbeddingService`` (lazy-loaded sentence-transformers) and
``EmbeddingIndexWriter`` (decorator that auto-computes embeddings before upsert).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pipecat_context_hub.shared.config import EmbeddingConfig
from pipecat_context_hub.shared.types import ChunkedRecord

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Compute embeddings using a local sentence-transformers model.

    The model is loaded lazily on first use to avoid slow import at startup
    (important for ``serve`` where the model may not be needed immediately).
    """

    def __init__(self, config: EmbeddingConfig | None = None) -> None:
        cfg = config or EmbeddingConfig()
        self._model_name = cfg.model_name
        self._dimension = cfg.dimension
        self._model: SentenceTransformer | None = None

    def _get_model(self) -> SentenceTransformer:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("Loading embedding model %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Compute embeddings for a batch of texts."""
        if not texts:
            return []
        model = self._get_model()
        embeddings: Any = model.encode(texts, show_progress_bar=False)
        return embeddings.tolist()  # type: ignore[no-any-return]

    def embed_query(self, query: str) -> list[float]:
        """Compute embedding for a single query string."""
        return self.embed_texts([query])[0]

    def embed_records(self, records: list[ChunkedRecord]) -> list[ChunkedRecord]:
        """Compute and attach embeddings to records that lack them.

        Mutates the records in-place and returns the same list.
        """
        needs_embedding = [r for r in records if r.embedding is None]
        if not needs_embedding:
            return records
        texts = [r.content for r in needs_embedding]
        vectors = self.embed_texts(texts)
        for record, vector in zip(needs_embedding, vectors):
            record.embedding = vector
        logger.debug("Computed embeddings for %d / %d records", len(needs_embedding), len(records))
        return records


class EmbeddingIndexWriter:
    """Wraps an IndexWriter to auto-compute embeddings before upsert.

    This keeps ingesters unchanged and centralises embedding logic.
    """

    def __init__(
        self,
        inner: Any,  # IndexStore (satisfies IndexWriter protocol)
        embedding_service: EmbeddingService,
    ) -> None:
        self._inner = inner
        self._embedding = embedding_service

    async def upsert(self, records: list[ChunkedRecord]) -> int:
        """Compute embeddings, then delegate to the inner writer."""
        enriched = await asyncio.to_thread(self._embedding.embed_records, records)
        return await self._inner.upsert(enriched)  # type: ignore[no-any-return]

    async def delete_by_source(self, source_url: str) -> int:
        """Delegate to the inner writer."""
        return await self._inner.delete_by_source(source_url)  # type: ignore[no-any-return]
