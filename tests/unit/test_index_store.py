"""Tests for the index store: VectorIndex, FTSIndex, and unified IndexStore."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pytest

from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.services.index.vector import VectorIndex, _CHROMA_BATCH_SIZE
from pipecat_context_hub.shared.config import StorageConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

EMBEDDING_DIM = 384


def _random_embedding(seed: int = 0) -> list[float]:
    """Generate a deterministic pseudo-random embedding vector."""
    rng = random.Random(seed)
    return [rng.gauss(0, 1) for _ in range(EMBEDDING_DIM)]


def _make_record(
    chunk_id: str = "chunk-1",
    content: str = "Pipecat is a framework for building voice agents.",
    content_type: Literal["doc", "code", "readme", "source"] = "doc",
    source_url: str = "https://docs.pipecat.ai/intro",
    repo: str | None = "pipecat-ai/pipecat",
    path: str = "/docs/intro.md",
    embedding: list[float] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ChunkedRecord:
    if embedding is None:
        embedding = _random_embedding(hash(chunk_id) % 10000)
    return ChunkedRecord(
        chunk_id=chunk_id,
        content=content,
        content_type=content_type,
        source_url=source_url,
        repo=repo,
        path=path,
        indexed_at=datetime.now(tz=timezone.utc),
        embedding=embedding,
        metadata=metadata or {},
    )


def _make_records(count: int, source_url: str = "https://docs.pipecat.ai/intro") -> list[ChunkedRecord]:
    """Create multiple unique records."""
    return [
        _make_record(
            chunk_id=f"chunk-{i}",
            content=f"Content for chunk {i} about pipecat voice agents.",
            source_url=source_url,
            path=f"/docs/page{i}.md",
            embedding=_random_embedding(i),
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# VectorIndex tests
# ---------------------------------------------------------------------------


class TestVectorIndex:
    @pytest.fixture()
    def vector_index(self, tmp_path: Path) -> VectorIndex:
        return VectorIndex(tmp_path / "chroma")

    def test_upsert_and_search(self, vector_index: VectorIndex):
        records = _make_records(3)
        count = vector_index.upsert(records)
        assert count == 3

        query = IndexQuery(
            query_text="pipecat",
            query_embedding=records[0].embedding,
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 3
        assert all(isinstance(r, IndexResult) for r in results)
        assert all(r.match_type == "vector" for r in results)
        # First result should be the most similar (same embedding)
        assert results[0].chunk.chunk_id == "chunk-0"
        assert results[0].score > 0

    def test_upsert_empty(self, vector_index: VectorIndex):
        count = vector_index.upsert([])
        assert count == 0

    def test_upsert_no_embedding(self, vector_index: VectorIndex):
        record = _make_record()
        record.embedding = None
        count = vector_index.upsert([record])
        assert count == 0

    def test_upsert_idempotent(self, vector_index: VectorIndex):
        record = _make_record(chunk_id="idem-1", content="original content")
        vector_index.upsert([record])

        # Upsert again with updated content
        updated = _make_record(chunk_id="idem-1", content="updated content")
        vector_index.upsert([updated])

        query = IndexQuery(
            query_text="test",
            query_embedding=updated.embedding,
            limit=10,
        )
        results = vector_index.search(query)
        # Should have exactly 1, not 2
        assert len(results) == 1
        assert results[0].chunk.content == "updated content"

    def test_delete_by_source(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="a1", source_url="https://a.com"),
            _make_record(chunk_id="a2", source_url="https://a.com"),
            _make_record(chunk_id="b1", source_url="https://b.com"),
        ]
        vector_index.upsert(records)

        deleted = vector_index.delete_by_source("https://a.com")
        assert deleted == 2

        # Only b1 should remain
        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "b1"

    def test_delete_nonexistent_source(self, vector_index: VectorIndex):
        deleted = vector_index.delete_by_source("https://nonexistent.com")
        assert deleted == 0

    def test_delete_by_content_type(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="d1", content_type="doc"),
            _make_record(chunk_id="d2", content_type="doc"),
            _make_record(chunk_id="c1", content_type="code"),
        ]
        vector_index.upsert(records)

        deleted = vector_index.delete_by_content_type("doc")
        assert deleted == 2

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content_type == "code"

    def test_delete_by_repo(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="r1", repo="pipecat-ai/pipecat", content_type="code"),
            _make_record(chunk_id="r2", repo="pipecat-ai/pipecat", content_type="source"),
            _make_record(chunk_id="r3", repo="pipecat-ai/pipecat-examples"),
        ]
        vector_index.upsert(records)

        deleted = vector_index.delete_by_repo("pipecat-ai/pipecat")
        assert deleted == 2

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.repo == "pipecat-ai/pipecat-examples"

    def test_delete_by_repo_nonexistent(self, vector_index: VectorIndex):
        deleted = vector_index.delete_by_repo("nonexistent/repo")
        assert deleted == 0

    def test_search_without_embedding(self, vector_index: VectorIndex):
        query = IndexQuery(query_text="test", limit=10)
        results = vector_index.search(query)
        assert results == []

    def test_filter_by_repo(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="r1", repo="pipecat-ai/pipecat"),
            _make_record(chunk_id="r2", repo="pipecat-ai/pipecat-examples"),
        ]
        vector_index.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"repo": "pipecat-ai/pipecat"},
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.repo == "pipecat-ai/pipecat"

    def test_filter_by_content_type(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="d1", content_type="doc"),
            _make_record(chunk_id="c1", content_type="code"),
        ]
        vector_index.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"content_type": "code"},
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content_type == "code"

    def test_filter_by_path_prefix(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="p1", path="/docs/guides/setup.md"),
            _make_record(chunk_id="p2", path="/docs/api/ref.md"),
            _make_record(chunk_id="p3", path="/examples/basic.py"),
        ]
        vector_index.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"path": "/docs/"},
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 2
        paths = {r.chunk.path for r in results}
        assert paths == {"/docs/guides/setup.md", "/docs/api/ref.md"}

    def test_filter_by_capability_tags(self, vector_index: VectorIndex):
        records = [
            _make_record(chunk_id="t1", metadata={"capability_tags": ["tts", "stt"]}),
            _make_record(chunk_id="t2", metadata={"capability_tags": ["vision"]}),
        ]
        vector_index.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"capability_tags": "tts"},
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "t1"

    def test_clear(self, vector_index: VectorIndex):
        """clear() should drop all records so searches return nothing."""
        vector_index.upsert(_make_records(5))
        vector_index.clear()

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        results = vector_index.search(query)
        assert results == []

    def test_clear_then_upsert(self, vector_index: VectorIndex):
        """Index should be usable after clear()."""
        vector_index.upsert(_make_records(3))
        vector_index.clear()

        new_records = [_make_record(chunk_id="new-1", content="fresh data")]
        vector_index.upsert(new_records)

        query = IndexQuery(
            query_text="test",
            query_embedding=new_records[0].embedding,
            limit=10,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "new-1"

    def test_persistence(self, tmp_path: Path):
        """Verify data survives creating a new VectorIndex on the same path."""
        chroma_path = tmp_path / "chroma"
        idx1 = VectorIndex(chroma_path)
        records = _make_records(2)
        idx1.upsert(records)

        # Create a new index pointing at the same directory
        idx2 = VectorIndex(chroma_path)
        query = IndexQuery(
            query_text="test",
            query_embedding=records[0].embedding,
            limit=10,
        )
        results = idx2.search(query)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# ChromaDB batch stress tests
# ---------------------------------------------------------------------------


class TestVectorIndexBatchStress:
    """Stress tests for ChromaDB batch operations above _CHROMA_BATCH_SIZE.

    These verify that upsert, delete_by_content_type, and delete_by_source
    work correctly when record counts exceed ChromaDB's per-call limit.
    """

    # Just over the batch size to trigger multi-batch code paths.
    OVER_LIMIT = _CHROMA_BATCH_SIZE + 100

    @pytest.fixture()
    def vector_index(self, tmp_path: Path) -> VectorIndex:
        return VectorIndex(tmp_path / "chroma")

    @pytest.mark.benchmark
    def test_upsert_above_batch_size(self, vector_index: VectorIndex):
        """Upsert more records than _CHROMA_BATCH_SIZE without error."""
        records = _make_records(self.OVER_LIMIT)
        count = vector_index.upsert(records)
        assert count == self.OVER_LIMIT
        assert vector_index._collection.count() == self.OVER_LIMIT

    @pytest.mark.benchmark
    def test_delete_by_content_type_above_batch_size(self, vector_index: VectorIndex):
        """Delete more records than _CHROMA_BATCH_SIZE by content_type."""
        records = [
            _make_record(
                chunk_id=f"doc-{i}",
                content=f"doc content {i}",
                content_type="doc",
                embedding=_random_embedding(i),
            )
            for i in range(self.OVER_LIMIT)
        ]
        # Add a few code records that should survive the delete
        survivors = [
            _make_record(
                chunk_id=f"code-{i}",
                content=f"code content {i}",
                content_type="code",
                embedding=_random_embedding(self.OVER_LIMIT + i),
            )
            for i in range(3)
        ]
        vector_index.upsert(records + survivors)
        assert vector_index._collection.count() == self.OVER_LIMIT + 3

        deleted = vector_index.delete_by_content_type("doc")
        assert deleted == self.OVER_LIMIT
        assert vector_index._collection.count() == 3

    @pytest.mark.benchmark
    def test_delete_by_source_above_batch_size(self, vector_index: VectorIndex):
        """Delete more records than _CHROMA_BATCH_SIZE by source URL."""
        source_url = "https://example.com/big-source"
        records = [
            _make_record(
                chunk_id=f"src-{i}",
                content=f"source content {i}",
                source_url=source_url,
                embedding=_random_embedding(i),
            )
            for i in range(self.OVER_LIMIT)
        ]
        other = _make_record(
            chunk_id="other-1",
            content="other content",
            source_url="https://example.com/other",
            embedding=_random_embedding(self.OVER_LIMIT + 1),
        )
        vector_index.upsert(records + [other])
        assert vector_index._collection.count() == self.OVER_LIMIT + 1

        deleted = vector_index.delete_by_source(source_url)
        assert deleted == self.OVER_LIMIT
        assert vector_index._collection.count() == 1

    @pytest.mark.benchmark
    def test_upsert_then_search_above_batch_size(self, vector_index: VectorIndex):
        """Records upserted in multiple batches are all searchable."""
        records = _make_records(self.OVER_LIMIT)
        vector_index.upsert(records)

        # Search for a record from the second batch
        target_idx = _CHROMA_BATCH_SIZE + 50
        query = IndexQuery(
            query_text="test",
            query_embedding=records[target_idx].embedding,
            limit=1,
        )
        results = vector_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == f"chunk-{target_idx}"


# ---------------------------------------------------------------------------
# FTSIndex tests
# ---------------------------------------------------------------------------


class TestFTSIndex:
    @pytest.fixture()
    def fts_index(self, tmp_path: Path) -> FTSIndex:
        return FTSIndex(tmp_path / "metadata.db")

    def test_upsert_and_search(self, fts_index: FTSIndex):
        records = _make_records(3)
        count = fts_index.upsert(records)
        assert count == 3

        query = IndexQuery(query_text="pipecat voice", limit=10)
        results = fts_index.search(query)
        assert len(results) == 3
        assert all(r.match_type == "keyword" for r in results)
        assert all(r.score > 0 for r in results)

    def test_upsert_empty(self, fts_index: FTSIndex):
        count = fts_index.upsert([])
        assert count == 0

    def test_upsert_idempotent(self, fts_index: FTSIndex):
        record = _make_record(chunk_id="idem-1", content="original pipecat content")
        fts_index.upsert([record])

        updated = _make_record(chunk_id="idem-1", content="updated pipecat content")
        fts_index.upsert([updated])

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content == "updated pipecat content"

    def test_delete_by_source(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="a1", source_url="https://a.com", content="pipecat a1"),
            _make_record(chunk_id="a2", source_url="https://a.com", content="pipecat a2"),
            _make_record(chunk_id="b1", source_url="https://b.com", content="pipecat b1"),
        ]
        fts_index.upsert(records)

        deleted = fts_index.delete_by_source("https://a.com")
        assert deleted == 2

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "b1"

    def test_delete_nonexistent_source(self, fts_index: FTSIndex):
        deleted = fts_index.delete_by_source("https://nonexistent.com")
        assert deleted == 0

    def test_delete_by_content_type(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="d1", content_type="doc", content="pipecat doc one"),
            _make_record(chunk_id="d2", content_type="doc", content="pipecat doc two"),
            _make_record(chunk_id="c1", content_type="code", content="pipecat code one"),
        ]
        fts_index.upsert(records)

        deleted = fts_index.delete_by_content_type("doc")
        assert deleted == 2

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content_type == "code"

    def test_delete_by_repo(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="r1", repo="pipecat-ai/pipecat", content_type="code", content="pipecat code r1"),
            _make_record(chunk_id="r2", repo="pipecat-ai/pipecat", content_type="source", content="pipecat source r2"),
            _make_record(chunk_id="r3", repo="pipecat-ai/pipecat-examples", content="pipecat examples r3"),
        ]
        fts_index.upsert(records)

        deleted = fts_index.delete_by_repo("pipecat-ai/pipecat")
        assert deleted == 2

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.repo == "pipecat-ai/pipecat-examples"

    def test_delete_by_repo_nonexistent(self, fts_index: FTSIndex):
        deleted = fts_index.delete_by_repo("nonexistent/repo")
        assert deleted == 0

    def test_search_empty_query(self, fts_index: FTSIndex):
        fts_index.upsert(_make_records(1))
        query = IndexQuery(query_text="", limit=10)
        results = fts_index.search(query)
        assert results == []

    def test_search_whitespace_query(self, fts_index: FTSIndex):
        fts_index.upsert(_make_records(1))
        query = IndexQuery(query_text="   ", limit=10)
        results = fts_index.search(query)
        assert results == []

    def test_filter_by_repo(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="r1", repo="pipecat-ai/pipecat", content="pipecat core"),
            _make_record(chunk_id="r2", repo="pipecat-ai/examples", content="pipecat examples"),
        ]
        fts_index.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"repo": "pipecat-ai/pipecat"},
            limit=10,
        )
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.repo == "pipecat-ai/pipecat"

    def test_filter_by_content_type(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="d1", content_type="doc", content="pipecat doc"),
            _make_record(chunk_id="c1", content_type="code", content="pipecat code"),
        ]
        fts_index.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"content_type": "code"},
            limit=10,
        )
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.content_type == "code"

    def test_filter_by_path_prefix(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id="p1", path="/docs/guides/setup.md", content="pipecat setup"),
            _make_record(chunk_id="p2", path="/docs/api/ref.md", content="pipecat api"),
            _make_record(chunk_id="p3", path="/examples/basic.py", content="pipecat example"),
        ]
        fts_index.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"path": "/docs/"},
            limit=10,
        )
        results = fts_index.search(query)
        assert len(results) == 2
        paths = {r.chunk.path for r in results}
        assert paths == {"/docs/guides/setup.md", "/docs/api/ref.md"}

    def test_filter_by_capability_tags(self, fts_index: FTSIndex):
        records = [
            _make_record(
                chunk_id="t1",
                content="pipecat with tts",
                metadata={"capability_tags": ["tts", "stt"]},
            ),
            _make_record(
                chunk_id="t2",
                content="pipecat with vision",
                metadata={"capability_tags": ["vision"]},
            ),
        ]
        fts_index.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"capability_tags": "tts"},
            limit=10,
        )
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "t1"

    def test_clear(self, fts_index: FTSIndex):
        """clear() should drop all records so searches return nothing."""
        fts_index.upsert(_make_records(5))
        fts_index.clear()

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert results == []

    def test_clear_then_upsert(self, fts_index: FTSIndex):
        """Index should be usable after clear()."""
        fts_index.upsert(_make_records(3))
        fts_index.clear()

        new_records = [_make_record(chunk_id="new-1", content="fresh pipecat data")]
        fts_index.upsert(new_records)

        query = IndexQuery(query_text="pipecat", limit=10)
        results = fts_index.search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "new-1"

    def test_persistence(self, tmp_path: Path):
        """Verify data survives creating a new FTSIndex on the same path."""
        db_path = tmp_path / "metadata.db"
        idx1 = FTSIndex(db_path)
        idx1.upsert([_make_record(content="pipecat persists")])
        idx1.close()

        idx2 = FTSIndex(db_path)
        query = IndexQuery(query_text="pipecat", limit=10)
        results = idx2.search(query)
        assert len(results) == 1
        idx2.close()

    def test_result_limit(self, fts_index: FTSIndex):
        records = [
            _make_record(chunk_id=f"lim-{i}", content=f"pipecat content {i}")
            for i in range(5)
        ]
        fts_index.upsert(records)

        query = IndexQuery(query_text="pipecat", limit=2)
        results = fts_index.search(query)
        assert len(results) == 2

    def test_delete_metadata(self, fts_index: FTSIndex):
        fts_index.set_metadata("repo:org/repo:commit_sha", "abc123")
        assert fts_index.get_metadata("repo:org/repo:commit_sha") == "abc123"

        fts_index.delete_metadata("repo:org/repo:commit_sha")
        assert fts_index.get_metadata("repo:org/repo:commit_sha") is None

    def test_delete_metadata_nonexistent(self, fts_index: FTSIndex):
        """Deleting a nonexistent key should not raise."""
        fts_index.delete_metadata("nonexistent:key")


# ---------------------------------------------------------------------------
# Unified IndexStore tests
# ---------------------------------------------------------------------------


class TestIndexStore:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> IndexStore:
        config = StorageConfig(data_dir=tmp_path / "hub-data")
        return IndexStore(config)

    @pytest.mark.asyncio
    async def test_upsert_and_vector_search(self, store: IndexStore):
        records = _make_records(3)
        count = await store.upsert(records)
        assert count == 3

        query = IndexQuery(
            query_text="pipecat",
            query_embedding=records[0].embedding,
            limit=10,
        )
        results = await store.vector_search(query)
        assert len(results) == 3
        assert all(r.match_type == "vector" for r in results)

    @pytest.mark.asyncio
    async def test_upsert_and_keyword_search(self, store: IndexStore):
        records = _make_records(3)
        await store.upsert(records)

        query = IndexQuery(query_text="pipecat voice", limit=10)
        results = await store.keyword_search(query)
        assert len(results) == 3
        assert all(r.match_type == "keyword" for r in results)

    @pytest.mark.asyncio
    async def test_delete_by_content_type_both_indexes(self, store: IndexStore):
        records = [
            _make_record(chunk_id="d1", content_type="doc", content="pipecat doc"),
            _make_record(chunk_id="d2", content_type="doc", content="pipecat doc two"),
            _make_record(chunk_id="c1", content_type="code", content="pipecat code"),
        ]
        await store.upsert(records)

        deleted = await store.delete_by_content_type("doc")
        assert deleted == 2

        # Vector search should only find code
        vq = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.content_type == "code"

        # Keyword search should also only find code
        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1
        assert k_results[0].chunk.content_type == "code"

    @pytest.mark.asyncio
    async def test_delete_by_source_both_indexes(self, store: IndexStore):
        records = [
            _make_record(chunk_id="a1", source_url="https://a.com", content="pipecat a"),
            _make_record(chunk_id="b1", source_url="https://b.com", content="pipecat b"),
        ]
        await store.upsert(records)

        deleted = await store.delete_by_source("https://a.com")
        assert deleted == 1

        # Vector search should only find b1
        vq = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.chunk_id == "b1"

        # Keyword search should also only find b1
        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1
        assert k_results[0].chunk.chunk_id == "b1"

    @pytest.mark.asyncio
    async def test_delete_by_repo_both_indexes(self, store: IndexStore):
        records = [
            _make_record(chunk_id="r1", repo="pipecat-ai/pipecat", content_type="code", content="pipecat code"),
            _make_record(chunk_id="r2", repo="pipecat-ai/pipecat", content_type="source", content="pipecat source"),
            _make_record(chunk_id="r3", repo="pipecat-ai/pipecat-examples", content="pipecat examples"),
        ]
        await store.upsert(records)

        deleted = await store.delete_by_repo("pipecat-ai/pipecat")
        assert deleted == 2

        # Vector search should only find pipecat-examples
        vq = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.repo == "pipecat-ai/pipecat-examples"

        # Keyword search should also only find pipecat-examples
        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1
        assert k_results[0].chunk.repo == "pipecat-ai/pipecat-examples"

    @pytest.mark.asyncio
    async def test_upsert_idempotent(self, store: IndexStore):
        record = _make_record(chunk_id="idem-1", content="original pipecat content")
        await store.upsert([record])

        updated = _make_record(chunk_id="idem-1", content="updated pipecat content")
        await store.upsert([updated])

        # Vector should have 1 record
        vq = IndexQuery(
            query_text="test",
            query_embedding=updated.embedding,
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.content == "updated pipecat content"

        # Keyword should have 1 record
        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1
        assert k_results[0].chunk.content == "updated pipecat content"

    @pytest.mark.asyncio
    async def test_clear(self, store: IndexStore):
        """clear() should drop all records from both indexes."""
        records = _make_records(5)
        await store.upsert(records)
        store.clear()

        vq = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            limit=10,
        )
        assert await store.vector_search(vq) == []

        kq = IndexQuery(query_text="pipecat", limit=10)
        assert await store.keyword_search(kq) == []

    @pytest.mark.asyncio
    async def test_clear_then_upsert(self, store: IndexStore):
        """Index should be usable after clear()."""
        await store.upsert(_make_records(3))
        store.clear()

        new_records = [_make_record(chunk_id="new-1", content="fresh pipecat data")]
        await store.upsert(new_records)

        vq = IndexQuery(
            query_text="test",
            query_embedding=new_records[0].embedding,
            limit=10,
        )
        v_results = await store.vector_search(vq)
        assert len(v_results) == 1
        assert v_results[0].chunk.chunk_id == "new-1"

        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store.keyword_search(kq)
        assert len(k_results) == 1

    def test_satisfies_writer_protocol(self, store: IndexStore):
        """Verify IndexStore has all IndexWriter methods."""
        assert callable(getattr(store, "upsert", None))
        assert callable(getattr(store, "delete_by_source", None))

    def test_satisfies_reader_protocol(self, store: IndexStore):
        """Verify IndexStore has all IndexReader methods."""
        assert callable(getattr(store, "vector_search", None))
        assert callable(getattr(store, "keyword_search", None))

    @pytest.mark.asyncio
    async def test_persistence(self, tmp_path: Path):
        """Verify data survives creating a new IndexStore on the same path."""
        config = StorageConfig(data_dir=tmp_path / "persist-data")
        store1 = IndexStore(config)
        records = _make_records(2)
        await store1.upsert(records)
        store1.close()

        store2 = IndexStore(config)
        vq = IndexQuery(
            query_text="test",
            query_embedding=records[0].embedding,
            limit=10,
        )
        v_results = await store2.vector_search(vq)
        assert len(v_results) == 2

        kq = IndexQuery(query_text="pipecat", limit=10)
        k_results = await store2.keyword_search(kq)
        assert len(k_results) == 2
        store2.close()

    @pytest.mark.asyncio
    async def test_metadata_filters_vector(self, store: IndexStore):
        """Test that metadata filters work in vector search."""
        records = [
            _make_record(
                chunk_id="f1",
                repo="pipecat-ai/pipecat",
                content_type="doc",
                path="/docs/guide.md",
            ),
            _make_record(
                chunk_id="f2",
                repo="pipecat-ai/pipecat-examples",
                content_type="code",
                path="/examples/bot.py",
            ),
        ]
        await store.upsert(records)

        query = IndexQuery(
            query_text="test",
            query_embedding=_random_embedding(0),
            filters={"repo": "pipecat-ai/pipecat", "content_type": "doc"},
            limit=10,
        )
        results = await store.vector_search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "f1"

    @pytest.mark.asyncio
    async def test_metadata_filters_keyword(self, store: IndexStore):
        """Test that metadata filters work in keyword search."""
        records = [
            _make_record(
                chunk_id="f1",
                repo="pipecat-ai/pipecat",
                content_type="doc",
                path="/docs/guide.md",
                content="pipecat guide content",
            ),
            _make_record(
                chunk_id="f2",
                repo="pipecat-ai/pipecat-examples",
                content_type="code",
                path="/examples/bot.py",
                content="pipecat example code",
            ),
        ]
        await store.upsert(records)

        query = IndexQuery(
            query_text="pipecat",
            filters={"repo": "pipecat-ai/pipecat", "content_type": "doc"},
            limit=10,
        )
        results = await store.keyword_search(query)
        assert len(results) == 1
        assert results[0].chunk.chunk_id == "f1"
