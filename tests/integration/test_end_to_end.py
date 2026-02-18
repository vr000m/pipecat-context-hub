"""End-to-end integration tests for the Pipecat Context Hub.

Tests the full pipeline: ingest → embed → index → retrieve with real
ChromaDB + SQLite + sentence-transformers (no mocks).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipecat_context_hub.services.embedding import (
    EmbeddingIndexWriter,
    EmbeddingService,
)
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever
from pipecat_context_hub.shared.types import (
    ChunkedRecord,
    GetCodeSnippetInput,
    GetDocInput,
    GetExampleInput,
    SearchDocsInput,
    SearchExamplesInput,
)

NOW = datetime.now(tz=timezone.utc)


def _make_doc_record(chunk_id: str, content: str, path: str = "/docs/test") -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id=chunk_id,
        content=content,
        content_type="doc",
        source_url=f"https://docs.pipecat.ai{path}",
        repo=None,
        path=path,
        indexed_at=NOW,
        metadata={"title": f"Doc: {chunk_id}"},
    )


def _make_code_record(
    chunk_id: str,
    content: str,
    repo: str = "pipecat-ai/pipecat",
    path: str = "examples/hello/bot.py",
) -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id=chunk_id,
        content=content,
        content_type="code",
        source_url=f"https://github.com/{repo}/blob/main/{path}",
        repo=repo,
        path=path,
        commit_sha="abc123",
        indexed_at=NOW,
        metadata={
            "repo": repo,
            "commit_sha": "abc123",
            "capability_tags": ["tts", "stt"],
            "foundational_class": "hello-world",
            "key_files": [path],
            "line_start": 1,
            "line_end": 10,
        },
    )


# ---------------------------------------------------------------------------
# Embedding service tests
# ---------------------------------------------------------------------------


class TestEmbeddingService:
    def test_embed_single_query(self, embedding_service: EmbeddingService):
        vec = embedding_service.embed_query("hello world")
        assert isinstance(vec, list)
        assert len(vec) == 384  # all-MiniLM-L6-v2 dimension

    def test_embed_batch(self, embedding_service: EmbeddingService):
        vecs = embedding_service.embed_texts(["hello", "world"])
        assert len(vecs) == 2
        assert all(len(v) == 384 for v in vecs)

    def test_embed_records(self, embedding_service: EmbeddingService):
        records = [_make_doc_record("d1", "Pipecat is a framework for voice AI")]
        assert records[0].embedding is None
        embedding_service.embed_records(records)
        assert records[0].embedding is not None
        assert len(records[0].embedding) == 384

    def test_embed_records_skips_existing(self, embedding_service: EmbeddingService):
        records = [_make_doc_record("d1", "test")]
        records[0].embedding = [0.0] * 384
        embedding_service.embed_records(records)
        # Should not overwrite existing embedding
        assert records[0].embedding == [0.0] * 384


# ---------------------------------------------------------------------------
# Index store round-trip tests
# ---------------------------------------------------------------------------


class TestIndexRoundTrip:
    async def test_upsert_and_keyword_search(
        self,
        embedding_writer: EmbeddingIndexWriter,
        index_store: IndexStore,
    ):
        records = [
            _make_doc_record("d1", "Pipecat is a framework for building voice AI bots"),
            _make_doc_record("d2", "WebSocket transport enables real-time communication"),
        ]
        count = await embedding_writer.upsert(records)
        assert count == 2

        from pipecat_context_hub.shared.types import IndexQuery

        results = await index_store.keyword_search(
            IndexQuery(query_text="voice AI bots", limit=5)
        )
        assert len(results) > 0
        assert results[0].chunk.chunk_id == "d1"

    async def test_upsert_and_vector_search(
        self,
        embedding_writer: EmbeddingIndexWriter,
        index_store: IndexStore,
        embedding_service: EmbeddingService,
    ):
        records = [
            _make_doc_record("d1", "Pipecat is a framework for building voice AI bots"),
            _make_doc_record("d2", "WebSocket transport enables real-time communication"),
        ]
        await embedding_writer.upsert(records)

        from pipecat_context_hub.shared.types import IndexQuery

        query_vec = embedding_service.embed_query("voice AI framework")
        results = await index_store.vector_search(
            IndexQuery(
                query_text="voice AI framework",
                query_embedding=query_vec,
                limit=5,
            )
        )
        assert len(results) > 0
        # Voice AI doc should rank higher than WebSocket doc
        assert results[0].chunk.chunk_id == "d1"

    async def test_delete_by_source(
        self,
        embedding_writer: EmbeddingIndexWriter,
        index_store: IndexStore,
    ):
        records = [_make_doc_record("d1", "test content")]
        await embedding_writer.upsert(records)

        deleted = await index_store.delete_by_source("https://docs.pipecat.ai/docs/test")
        assert deleted == 1


# ---------------------------------------------------------------------------
# Hybrid retriever end-to-end tests
# ---------------------------------------------------------------------------


class TestHybridRetrieverE2E:
    @pytest.fixture(autouse=True)
    async def _seed_index(self, embedding_writer: EmbeddingIndexWriter):
        """Seed the index with test data before each test."""
        docs = [
            _make_doc_record("d1", "Pipecat is a framework for building voice AI bots"),
            _make_doc_record(
                "d2",
                "To create a Pipecat bot, you need a pipeline with transport, "
                "speech-to-text, LLM, and text-to-speech services",
            ),
            _make_doc_record("d3", "WebSocket transport connects RTVI frontend to Pipecat backend"),
        ]
        code = [
            _make_code_record(
                "c1",
                'async def main():\n    transport = DailyTransport()\n    stt = DeepgramSTT()\n'
                '    llm = OpenAILLM()\n    tts = CartesiaTTS()\n    pipeline = Pipeline('
                '[transport.input(), stt, llm, tts, transport.output()])\n',
            ),
            _make_code_record(
                "c2",
                "# Wake word detection example\n"
                "from pipecat.processors import WakeWordProcessor\n"
                'wake = WakeWordProcessor(keyword="hey pipecat")\n',
                path="examples/wake-word/bot.py",
            ),
        ]
        await embedding_writer.upsert(docs + code)

    async def test_search_docs(self, retriever: HybridRetriever):
        result = await retriever.search_docs(
            SearchDocsInput(query="how to create a Pipecat bot")
        )
        assert len(result.hits) > 0
        assert result.evidence.confidence > 0.0
        # Top hit should be about creating a bot
        top = result.hits[0]
        assert top.citation.source_url.startswith("https://docs.pipecat.ai")

    async def test_search_examples(self, retriever: HybridRetriever):
        result = await retriever.search_examples(
            SearchExamplesInput(query="voice bot pipeline")
        )
        assert len(result.hits) > 0
        assert result.evidence.confidence > 0.0
        # Should return code results
        top = result.hits[0]
        assert top.repo == "pipecat-ai/pipecat"

    async def test_get_doc_found(self, retriever: HybridRetriever):
        result = await retriever.get_doc(GetDocInput(doc_id="d1"))
        assert result.doc_id == "d1"
        assert "Pipecat" in result.content

    async def test_get_doc_not_found(self, retriever: HybridRetriever):
        result = await retriever.get_doc(GetDocInput(doc_id="nonexistent"))
        assert result.title == "Not Found"
        assert result.evidence.confidence == 0.0

    async def test_get_example_found(self, retriever: HybridRetriever):
        result = await retriever.get_example(GetExampleInput(example_id="c1"))
        assert result.example_id == "c1"
        assert len(result.files) > 0

    async def test_get_code_snippet_by_intent(self, retriever: HybridRetriever):
        result = await retriever.get_code_snippet(
            GetCodeSnippetInput(intent="wake word detection")
        )
        assert result.evidence is not None

    async def test_evidence_report_structure(self, retriever: HybridRetriever):
        result = await retriever.search_docs(
            SearchDocsInput(query="Pipecat framework")
        )
        ev = result.evidence
        assert ev.confidence >= 0.0
        assert ev.confidence <= 1.0
        assert isinstance(ev.confidence_rationale, str)
        assert isinstance(ev.known, list)
        assert isinstance(ev.unknown, list)
        assert isinstance(ev.next_retrieval_queries, list)

    async def test_empty_query_returns_evidence(self, retriever: HybridRetriever):
        result = await retriever.search_docs(
            SearchDocsInput(query="xyzzy nonexistent topic 12345")
        )
        # Should still return a valid evidence report even with no hits
        assert result.evidence is not None
        assert result.evidence.confidence_rationale != ""
