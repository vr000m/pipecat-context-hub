"""Unified index store combining ChromaDB vector search and SQLite FTS5.

Implements both ``IndexWriter`` and ``IndexReader`` protocols by delegating
to the VectorIndex and FTSIndex backends.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

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
        self._data_dir = config.data_dir
        self._vector = VectorIndex(config.chroma_path)
        self._fts = FTSIndex(config.sqlite_path)
        logger.info("IndexStore initialized with data_dir=%s", config.data_dir)

    @property
    def data_dir(self) -> Path:
        """Path to the index data directory."""
        return self._data_dir

    def clear(self) -> None:
        """Drop all records from both indexes for a clean rebuild."""
        self._vector.clear()
        self._fts.clear()
        logger.info("IndexStore cleared")

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

    async def delete_by_content_type(self, content_type: str) -> int:
        """Delete records by content type from both indexes. Returns count deleted."""
        vector_count = self._vector.delete_by_content_type(content_type)
        self._fts.delete_by_content_type(content_type)
        return vector_count

    async def delete_by_repo(self, repo: str) -> int:
        """Delete records by repo from both indexes. Returns count deleted."""
        vector_count = self._vector.delete_by_repo(repo)
        try:
            fts_count = self._fts.delete_by_repo(repo)
        except Exception:
            logger.exception("FTS delete_by_repo failed; indexes may have diverged")
            fts_count = 0
        if vector_count != fts_count:
            logger.warning(
                "Delete divergence (repo=%s): vector=%d fts=%d",
                repo, vector_count, fts_count,
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

    def set_metadata(self, key: str, value: str) -> None:
        """Store a key-value pair in persistent index metadata."""
        self._fts.set_metadata(key, value)

    def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key, or None if not found."""
        return self._fts.get_metadata(key)

    def delete_metadata(self, key: str) -> None:
        """Remove a metadata key if it exists."""
        self._fts.delete_metadata(key)

    def get_all_metadata(self) -> dict[str, str]:
        """Return all persistent index metadata as a dict."""
        return self._fts.get_all_metadata()

    def get_index_stats(self) -> dict[str, Any]:
        """Return index statistics (counts by type, total, commit SHAs)."""
        return self._fts.get_index_stats()

    def close(self) -> None:
        """Close underlying database connections."""
        self._fts.close()
