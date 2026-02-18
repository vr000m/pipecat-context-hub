"""End-to-end integration tests for the Pipecat Context Hub.

Tests the full pipeline: ingest → embed → index → retrieve with real
ChromaDB + SQLite + sentence-transformers (no mocks).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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


# ---------------------------------------------------------------------------
# Regression tests for filter semantics (P1/P2 findings)
# ---------------------------------------------------------------------------


def _make_code_record_with_meta(
    chunk_id: str,
    content: str,
    *,
    language: str | None = None,
    foundational_class: str | None = None,
    execution_mode: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    repo: str = "pipecat-ai/pipecat",
    path: str = "examples/test/bot.py",
) -> ChunkedRecord:
    """Helper to build code records with varied metadata for filter tests."""
    meta: dict[str, Any] = {
        "capability_tags": ["tts"],
        "key_files": [path],
    }
    if language is not None:
        meta["language"] = language
    if foundational_class is not None:
        meta["foundational_class"] = foundational_class
    if execution_mode is not None:
        meta["execution_mode"] = execution_mode
    if line_start is not None:
        meta["line_start"] = line_start
    if line_end is not None:
        meta["line_end"] = line_end
    return ChunkedRecord(
        chunk_id=chunk_id,
        content=content,
        content_type="code",
        source_url=f"https://github.com/{repo}/blob/main/{path}",
        repo=repo,
        path=path,
        commit_sha="abc123",
        indexed_at=NOW,
        metadata=meta,
    )


class TestFilterSemantics:
    """Regression tests for search_examples filter enforcement.

    Validates that language, foundational_class, and execution_mode
    filters actually narrow results (P1 finding).
    """

    @pytest.fixture(autouse=True)
    async def _seed_mixed_records(self, embedding_writer: EmbeddingIndexWriter):
        """Seed index with records that differ on filterable metadata."""
        records = [
            _make_code_record_with_meta(
                "py1",
                "Python voice bot using DailyTransport and DeepgramSTT pipeline",
                language="python",
                foundational_class="hello-world",
                execution_mode="local",
                path="examples/py-bot/bot.py",
            ),
            _make_code_record_with_meta(
                "js1",
                "JavaScript voice bot using DailyTransport and DeepgramSTT pipeline",
                language="javascript",
                foundational_class="hello-world",
                execution_mode="local",
                path="examples/js-bot/bot.js",
            ),
            _make_code_record_with_meta(
                "wake1",
                "Wake word detection example using WakeWordProcessor pipeline",
                language="python",
                foundational_class="wake-word",
                execution_mode="cloud",
                path="examples/wake/bot.py",
            ),
        ]
        await embedding_writer.upsert(records)

    async def test_language_filter_narrows_results(self, retriever: HybridRetriever):
        """search_examples(language='python') should exclude javascript records."""
        result = await retriever.search_examples(
            SearchExamplesInput(query="voice bot pipeline", language="python")
        )
        for hit in result.hits:
            # All returned hits should be from python files
            assert hit.example_id != "js1", (
                f"JavaScript record 'js1' should be filtered out by language='python'"
            )

    async def test_foundational_class_filter_narrows_results(self, retriever: HybridRetriever):
        """search_examples(foundational_class='wake-word') should only return wake-word examples."""
        result = await retriever.search_examples(
            SearchExamplesInput(query="voice bot", foundational_class="wake-word")
        )
        for hit in result.hits:
            assert hit.example_id not in ("py1", "js1"), (
                f"Record '{hit.example_id}' (hello-world) should be filtered out "
                f"by foundational_class='wake-word'"
            )

    async def test_execution_mode_filter_narrows_results(self, retriever: HybridRetriever):
        """search_examples(execution_mode='cloud') should only return cloud examples."""
        result = await retriever.search_examples(
            SearchExamplesInput(query="voice bot pipeline", execution_mode="cloud")
        )
        for hit in result.hits:
            assert hit.example_id not in ("py1", "js1"), (
                f"Record '{hit.example_id}' (local) should be filtered out "
                f"by execution_mode='cloud'"
            )


class TestCodeSnippetLineRange:
    """Regression tests for get_code_snippet path+line_start (P1 finding).

    Validates that line metadata is persisted and line-range extraction works.
    """

    @pytest.fixture(autouse=True)
    async def _seed_code_with_lines(self, embedding_writer: EmbeddingIndexWriter):
        """Seed a code record with known line metadata."""
        lines = [f"line {i}: code content here" for i in range(1, 21)]
        content = "\n".join(lines)
        records = [
            _make_code_record_with_meta(
                "lines1",
                content,
                language="python",
                line_start=1,
                line_end=20,
                path="examples/lines/bot.py",
            ),
        ]
        await embedding_writer.upsert(records)

    async def test_snippet_by_path_returns_content(self, retriever: HybridRetriever):
        """get_code_snippet(path+line_start) should return matching snippet."""
        result = await retriever.get_code_snippet(
            GetCodeSnippetInput(
                path="examples/lines/bot.py",
                line_start=5,
                line_end=10,
            )
        )
        # Should find the record and return snippet content
        assert len(result.snippets) > 0
        # The returned snippet should contain the requested lines
        snippet = result.snippets[0]
        assert "line 5" in snippet.content
        assert snippet.path == "examples/lines/bot.py"

    async def test_snippet_line_range_trimmed(self, retriever: HybridRetriever):
        """Snippet should be trimmed to requested line range, not full chunk."""
        result = await retriever.get_code_snippet(
            GetCodeSnippetInput(
                path="examples/lines/bot.py",
                line_start=5,
                line_end=8,
            )
        )
        if result.snippets:
            snippet = result.snippets[0]
            lines = snippet.content.splitlines()
            # Should have at most 4 lines (5,6,7,8)
            assert len(lines) <= 4


class TestGetExamplePathCorrectness:
    """Regression test for get_example path mislabelling (P2 finding).

    Validates that get_example returns the chunk's actual path,
    not the caller-supplied input.path.
    """

    @pytest.fixture(autouse=True)
    async def _seed_example(self, embedding_writer: EmbeddingIndexWriter):
        records = [
            _make_code_record_with_meta(
                "ex1",
                "Example bot code for voice AI",
                language="python",
                path="examples/real-path/bot.py",
            ),
        ]
        await embedding_writer.upsert(records)

    async def test_file_path_matches_chunk_not_input(self, retriever: HybridRetriever):
        """get_example should return files with the chunk's path, not input.path."""
        result = await retriever.get_example(
            GetExampleInput(example_id="ex1", path="examples/wrong-path/other.py")
        )
        assert result.example_id == "ex1"
        assert len(result.files) > 0
        # The file path should be the chunk's real path, not the input path
        assert result.files[0].path == "examples/real-path/bot.py"
        assert result.files[0].path != "examples/wrong-path/other.py"
