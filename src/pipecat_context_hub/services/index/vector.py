"""ChromaDB-backed vector index for embedding similarity search.

Wraps ChromaDB's PersistentClient to store ChunkedRecord embeddings and
support filtered vector queries. Uses collection name ``latest`` as the
single namespace for v0.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb

from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

logger = logging.getLogger(__name__)

COLLECTION_NAME = "latest"

# ChromaDB limits batch operations to ~5,461 embeddings. Use a safe limit.
_CHROMA_BATCH_SIZE = 5000


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
    # Persist additional metadata fields used by filters
    for key in ("foundational_class", "language", "execution_mode"):
        val = record.metadata.get(key)
        if val is not None:
            meta[key] = str(val)
    for key in ("line_start", "line_end"):
        val = record.metadata.get(key)
        if val is not None:
            meta[key] = int(val)
    # Source API metadata fields
    for key in ("module_path", "class_name", "chunk_type", "method_name", "method_signature", "return_type"):
        val = record.metadata.get(key)
        if val is not None:
            meta[key] = str(val)
    # Boolean metadata fields
    for key in ("is_dataclass", "is_abstract"):
        val = record.metadata.get(key)
        if val is not None:
            meta[key] = bool(val)
    # List metadata stored as JSON string (lossless for values containing commas)
    base_classes = record.metadata.get("base_classes")
    if base_classes and isinstance(base_classes, list):
        meta["base_classes"] = json.dumps(base_classes)
    imports = record.metadata.get("imports")
    if imports and isinstance(imports, list):
        meta["imports"] = json.dumps(imports)
    yields = record.metadata.get("yields")
    if yields and isinstance(yields, list):
        meta["yields"] = json.dumps(yields)
    calls = record.metadata.get("calls")
    if calls and isinstance(calls, list):
        meta["calls"] = json.dumps(calls)
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
    # Round-trip optional metadata fields
    for key in ("foundational_class", "language", "execution_mode"):
        val = meta.get(key)
        if val is not None:
            extra_meta[key] = val
    for key in ("line_start", "line_end"):
        val = meta.get(key)
        if val is not None:
            extra_meta[key] = int(val)
    for key in ("module_path", "class_name", "chunk_type", "method_name", "method_signature", "return_type"):
        val = meta.get(key)
        if val is not None:
            extra_meta[key] = val
    for key in ("is_dataclass", "is_abstract"):
        val = meta.get(key)
        if val is not None:
            extra_meta[key] = bool(val)
    base_classes_str = meta.get("base_classes", "")
    if base_classes_str:
        try:
            extra_meta["base_classes"] = json.loads(base_classes_str)
        except (json.JSONDecodeError, TypeError):
            # Fallback for legacy comma-separated format
            extra_meta["base_classes"] = base_classes_str.split(",")
    imports_str = meta.get("imports", "")
    if imports_str:
        try:
            extra_meta["imports"] = json.loads(imports_str)
        except (json.JSONDecodeError, TypeError):
            extra_meta["imports"] = []
    yields_str = meta.get("yields", "")
    if yields_str:
        try:
            extra_meta["yields"] = json.loads(yields_str)
        except (json.JSONDecodeError, TypeError):
            extra_meta["yields"] = []
    calls_str = meta.get("calls", "")
    if calls_str:
        try:
            extra_meta["calls"] = json.loads(calls_str)
        except (json.JSONDecodeError, TypeError):
            extra_meta["calls"] = []

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
    if "foundational_class" in filters:
        conditions.append({"foundational_class": {"$eq": filters["foundational_class"]}})
    if "language" in filters:
        conditions.append({"language": {"$eq": filters["language"]}})
    if "execution_mode" in filters:
        conditions.append({"execution_mode": {"$eq": filters["execution_mode"]}})
    if "class_name" in filters:
        conditions.append({"class_name": {"$eq": filters["class_name"]}})
    if "method_name" in filters:
        conditions.append({"method_name": {"$eq": filters["method_name"]}})
    if "chunk_type" in filters:
        conditions.append({"chunk_type": {"$eq": filters["chunk_type"]}})
    if "is_dataclass" in filters:
        conditions.append({"is_dataclass": {"$eq": filters["is_dataclass"]}})

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

    if "module_path" in filters:
        prefix = filters["module_path"]
        filtered = [r for r in filtered if r.chunk.metadata.get("module_path", "").startswith(prefix)]

    if "yields" in filters:
        val = filters["yields"]
        filtered = [r for r in filtered if val in r.chunk.metadata.get("yields", [])]
    if "calls" in filters:
        val = filters["calls"]
        filtered = [r for r in filtered if val in r.chunk.metadata.get("calls", [])]

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

    def clear(self) -> None:
        """Drop all records by deleting and recreating the collection."""
        self._client.delete_collection(COLLECTION_NAME)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("VectorIndex cleared")

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

        # ChromaDB limits batch operations to ~5,461 embeddings.
        for i in range(0, len(ids), _CHROMA_BATCH_SIZE):
            end = i + _CHROMA_BATCH_SIZE
            self._collection.upsert(
                ids=ids[i:end],
                documents=documents[i:end],
                metadatas=metadatas[i:end],
                embeddings=embeddings[i:end],
            )
        logger.debug("Upserted %d records into vector index", len(ids))
        return len(ids)

    def delete_by_content_type(self, content_type: str) -> int:
        """Delete all records matching a content type. Returns count deleted."""
        where_clause: dict[str, Any] = {"content_type": {"$eq": content_type}}
        existing = self._collection.get(where=where_clause, include=[])
        ids = existing["ids"]
        count = len(ids)
        if count > 0:
            # ChromaDB limits batch operations to ~5,000 records.
            for i in range(0, count, _CHROMA_BATCH_SIZE):
                batch = ids[i : i + _CHROMA_BATCH_SIZE]
                self._collection.delete(ids=batch)
            logger.debug("Deleted %d records from vector index for content_type=%s", count, content_type)
        return count

    def delete_by_repo(self, repo: str) -> int:
        """Delete all records matching a repo. Returns count deleted."""
        where_clause: dict[str, Any] = {"repo": {"$eq": repo}}
        existing = self._collection.get(where=where_clause, include=[])
        ids = existing["ids"]
        count = len(ids)
        if count > 0:
            for i in range(0, count, _CHROMA_BATCH_SIZE):
                batch = ids[i : i + _CHROMA_BATCH_SIZE]
                self._collection.delete(ids=batch)
            logger.debug("Deleted %d records from vector index for repo=%s", count, repo)
        return count

    def delete_by_source(self, source_url: str) -> int:
        """Delete all records matching a source URL. Returns count deleted."""
        where_clause: dict[str, Any] = {"source_url": {"$eq": source_url}}
        existing = self._collection.get(where=where_clause, include=[])
        ids = existing["ids"]
        count = len(ids)
        if count > 0:
            for i in range(0, count, _CHROMA_BATCH_SIZE):
                batch = ids[i : i + _CHROMA_BATCH_SIZE]
                self._collection.delete(ids=batch)
            logger.debug("Deleted %d records from vector index for source %s", count, source_url)
        return count

    def search(self, query: IndexQuery) -> list[IndexResult]:
        """Search by embedding similarity. Returns results ranked by score."""
        if query.query_embedding is None:
            logger.warning("vector_search called without query_embedding")
            return []

        needs_post_filter = any(k in query.filters for k in ("path", "capability_tags", "module_path", "yields", "calls"))
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
