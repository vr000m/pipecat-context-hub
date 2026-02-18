"""ChromaDB-backed vector index for embedding similarity search.

Wraps ChromaDB's PersistentClient to store ChunkedRecord embeddings and
support filtered vector queries. Uses collection name ``latest`` as the
single namespace for v0.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb

from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

logger = logging.getLogger(__name__)

COLLECTION_NAME = "latest"


def _record_to_metadata(
    record: ChunkedRecord,
) -> dict[str, str | int | float | bool]:
    """Extract ChromaDB-storable metadata from a ChunkedRecord.

    ChromaDB metadata values must be str, int, float, or bool.
    We store everything as str for simplicity and filter on exact match.
    """
    meta: dict[str, str | int | float | bool] = {
        "source_url": record.source_url,
        "content_type": record.content_type,
        "path": record.path,
        "indexed_at": record.indexed_at.isoformat(),
    }
    if record.repo is not None:
        meta["repo"] = record.repo
    if record.commit_sha is not None:
        meta["commit_sha"] = record.commit_sha
    # Store capability_tags as comma-separated string for ChromaDB
    tags = record.metadata.get("capability_tags")
    if tags and isinstance(tags, list):
        meta["capability_tags"] = ",".join(str(t) for t in tags)
    return meta


def _metadata_to_record_fields(
    chunk_id: str,
    document: str,
    meta: Mapping[str, Any],
) -> ChunkedRecord:
    """Reconstruct a ChunkedRecord from ChromaDB stored data."""
    extra_meta: dict[str, Any] = {}
    capability_tags_str = meta.get("capability_tags", "")
    if capability_tags_str:
        extra_meta["capability_tags"] = capability_tags_str.split(",")

    return ChunkedRecord(
        chunk_id=chunk_id,
        content=document,
        content_type=meta.get("content_type", "doc"),
        source_url=meta.get("source_url", ""),
        repo=meta.get("repo"),
        path=meta.get("path", ""),
        commit_sha=meta.get("commit_sha"),
        indexed_at=datetime.fromisoformat(meta["indexed_at"]),
        metadata=extra_meta,
    )


def _build_where_clause(filters: dict[str, Any]) -> dict[str, Any] | None:
    """Convert IndexQuery.filters into a ChromaDB ``where`` clause.

    ChromaDB 0.6.x ``where`` supports: $eq, $ne, $in, $nin, $gt, $gte, $lt, $lte.
    Only ``repo`` and ``content_type`` can be pushed down as exact-match filters.
    ``path`` (prefix) and ``capability_tags`` (substring) are applied as
    post-filters in :meth:`VectorIndex.search`.
    """
    conditions: list[dict[str, Any]] = []

    if "repo" in filters:
        conditions.append({"repo": {"$eq": filters["repo"]}})
    if "content_type" in filters:
        conditions.append({"content_type": {"$eq": filters["content_type"]}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _apply_post_filters(
    results: list[IndexResult], filters: dict[str, Any]
) -> list[IndexResult]:
    """Apply filters that ChromaDB cannot handle natively.

    - path: prefix match
    - capability_tags: substring match on comma-separated metadata string
    """
    filtered = results

    if "path" in filters:
        prefix = filters["path"]
        filtered = [r for r in filtered if r.chunk.path.startswith(prefix)]

    if "capability_tags" in filters:
        tag = filters["capability_tags"]
        tags_to_match: list[str] = tag if isinstance(tag, list) else [str(tag)]
        filtered = [
            r
            for r in filtered
            if _record_has_tags(r, tags_to_match)
        ]

    return filtered


def _record_has_tags(result: IndexResult, tags: list[str]) -> bool:
    """Check if a result's metadata contains all requested capability tags."""
    record_tags = result.chunk.metadata.get("capability_tags", [])
    if isinstance(record_tags, str):
        record_tags = record_tags.split(",")
    return all(t in record_tags for t in tags)


class VectorIndex:
    """ChromaDB vector index for embedding similarity search.

    This class handles storage and retrieval of vector embeddings using
    ChromaDB's PersistentClient. It persists data to a local directory
    so indexes survive process restarts.
    """

    def __init__(self, chroma_path: Path) -> None:
        self._chroma_path = chroma_path
        self._chroma_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(chroma_path))
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("VectorIndex initialized at %s", chroma_path)

    def upsert(self, records: list[ChunkedRecord]) -> int:
        """Upsert records into ChromaDB. Returns count of records written."""
        if not records:
            return 0

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[Mapping[str, str | int | float | bool]] = []
        embeddings: list[Sequence[float]] = []

        for record in records:
            if record.embedding is None:
                logger.warning("Skipping record %s: no embedding", record.chunk_id)
                continue
            ids.append(record.chunk_id)
            documents.append(record.content)
            metadatas.append(_record_to_metadata(record))
            embeddings.append(record.embedding)

        if not ids:
            return 0

        self._collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        logger.debug("Upserted %d records into vector index", len(ids))
        return len(ids)

    def delete_by_source(self, source_url: str) -> int:
        """Delete all records matching a source URL. Returns count deleted."""
        where_clause: dict[str, Any] = {"source_url": {"$eq": source_url}}
        # Get IDs first so we can report count
        existing = self._collection.get(
            where=where_clause,
            include=[],
        )
        count = len(existing["ids"])
        if count > 0:
            self._collection.delete(where=where_clause)
            logger.debug("Deleted %d records from vector index for source %s", count, source_url)
        return count

    def search(self, query: IndexQuery) -> list[IndexResult]:
        """Search by embedding similarity. Returns results ranked by score."""
        if query.query_embedding is None:
            logger.warning("vector_search called without query_embedding")
            return []

        needs_post_filter = "path" in query.filters or "capability_tags" in query.filters
        where = _build_where_clause(query.filters)

        # Clamp n_results to collection size to prevent ChromaDB crash
        collection_count = self._collection.count()
        if collection_count == 0:
            return []

        # Over-fetch when post-filtering to ensure enough results survive.
        n_results = query.limit * 3 if needs_post_filter else query.limit
        n_results = min(n_results, collection_count)

        kwargs: dict[str, Any] = {
            "query_embeddings": [query.query_embedding],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where is not None:
            kwargs["where"] = where

        results = self._collection.query(**kwargs)

        items: list[IndexResult] = []
        result_ids = results["ids"][0] if results["ids"] else []
        result_docs = results["documents"][0] if results["documents"] else []
        result_metas = results["metadatas"][0] if results["metadatas"] else []
        result_dists = results["distances"][0] if results["distances"] else []

        for chunk_id, doc, meta, dist in zip(
            result_ids, result_docs, result_metas, result_dists
        ):
            # ChromaDB cosine distance: 0 = identical, 2 = opposite
            # Convert to similarity score: 1 - (distance / 2) gives 0..1 range
            score = 1.0 - (dist / 2.0)
            record = _metadata_to_record_fields(chunk_id, doc, meta)
            items.append(
                IndexResult(
                    chunk=record,
                    score=score,
                    match_type="vector",
                )
            )

        if needs_post_filter:
            items = _apply_post_filters(items, query.filters)

        return items[: query.limit]
