"""Unified index store combining ChromaDB vector search and SQLite FTS5.

Implements both ``IndexWriter`` and ``IndexReader`` protocols by delegating
to the VectorIndex and FTSIndex backends.
"""

from __future__ import annotations

import logging

from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.services.index.vector import VectorIndex
from pipecat_context_hub.shared.config import StorageConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

logger = logging.getLogger(__name__)


class IndexStore:
    """Unified index store satisfying both IndexWriter and IndexReader.

    Writes go to both ChromaDB (vector) and SQLite FTS5 (keyword) backends.
    Reads are dispatched to the appropriate backend based on the search type.
    """

    def __init__(self, config: StorageConfig) -> None:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self._vector = VectorIndex(config.chroma_path)
        self._fts = FTSIndex(config.sqlite_path)
        logger.info("IndexStore initialized with data_dir=%s", config.data_dir)

    async def upsert(self, records: list[ChunkedRecord]) -> int:
        """Insert or update records in both indexes. Returns count written."""
        vector_count = self._vector.upsert(records)
        try:
            fts_count = self._fts.upsert(records)
        except Exception:
            logger.exception("FTS upsert failed; vector index may have diverged")
            fts_count = 0
        if vector_count != fts_count:
            logger.warning(
                "Index divergence: vector=%d fts=%d records", vector_count, fts_count
            )
        return vector_count

    async def delete_by_source(self, source_url: str) -> int:
        """Delete records by source URL from both indexes. Returns count deleted."""
        vector_count = self._vector.delete_by_source(source_url)
        self._fts.delete_by_source(source_url)
        return vector_count

    async def vector_search(self, query: IndexQuery) -> list[IndexResult]:
        """Return results ranked by embedding similarity."""
        return self._vector.search(query)

    async def keyword_search(self, query: IndexQuery) -> list[IndexResult]:
        """Return results ranked by FTS5 keyword relevance."""
        return self._fts.search(query)

    def close(self) -> None:
        """Close underlying database connections."""
        self._fts.close()
