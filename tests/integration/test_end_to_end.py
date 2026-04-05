"""End-to-end integration tests for the Pipecat Context Hub.

Tests the full pipeline: ingest → embed → index → retrieve with real
ChromaDB + SQLite + sentence-transformers (no mocks).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

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
    SearchApiInput,
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
        source_url = urlparse(top.citation.source_url)
        assert source_url.scheme == "https"
        assert source_url.hostname == "docs.pipecat.ai"

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
                "JavaScript record 'js1' should be filtered out by language='python'"
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

    async def test_snippet_non_overlapping_range_before_chunk(self, retriever: HybridRetriever):
        """Requesting lines before chunk range should not return unrelated content."""
        # Chunk has lines 1-20; request lines 100-110 which don't overlap.
        result = await retriever.get_code_snippet(
            GetCodeSnippetInput(
                path="examples/lines/bot.py",
                line_start=100,
                line_end=110,
            )
        )
        # No snippet should be returned since the range doesn't overlap
        assert len(result.snippets) == 0


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
        """get_example should return files with the chunk's stored path."""
        result = await retriever.get_example(GetExampleInput(example_id="ex1"))
        assert result.example_id == "ex1"
        assert len(result.files) > 0
        assert result.files[0].path == "examples/real-path/bot.py"


# ---------------------------------------------------------------------------
# Version-aware indexing smoke tests (Phase 1a)
# ---------------------------------------------------------------------------


def _make_versioned_code_record(
    chunk_id: str,
    content: str,
    *,
    pipecat_version_pin: str | None = None,
    repo: str = "pipecat-ai/pipecat",
    path: str = "examples/test/bot.py",
    language: str = "python",
) -> ChunkedRecord:
    """Helper to build code records with pipecat_version_pin metadata."""
    meta: dict[str, Any] = {
        "capability_tags": ["tts"],
        "key_files": [path],
        "language": language,
        "line_start": 1,
        "line_end": 10,
    }
    if pipecat_version_pin is not None:
        meta["pipecat_version_pin"] = pipecat_version_pin
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


def _make_versioned_source_record(
    chunk_id: str,
    content: str,
    *,
    pipecat_version_pin: str | None = None,
    module_path: str = "pipecat.services.test",
    class_name: str | None = None,
    chunk_type: str = "class_overview",
) -> ChunkedRecord:
    """Helper to build source records with pipecat_version_pin metadata."""
    meta: dict[str, Any] = {
        "module_path": module_path,
        "chunk_type": chunk_type,
        "line_start": 1,
        "line_end": 20,
    }
    if class_name is not None:
        meta["class_name"] = class_name
    if pipecat_version_pin is not None:
        meta["pipecat_version_pin"] = pipecat_version_pin
    return ChunkedRecord(
        chunk_id=chunk_id,
        content=content,
        content_type="source",
        source_url=f"https://github.com/pipecat-ai/pipecat/blob/main/src/{module_path.replace('.', '/')}.py",
        repo="pipecat-ai/pipecat",
        path=f"src/{module_path.replace('.', '/')}.py",
        commit_sha="abc123",
        indexed_at=NOW,
        metadata=meta,
    )


class TestVersionPinInSearchExamples:
    """Verify pipecat_version_pin flows through search_examples results."""

    @pytest.fixture(autouse=True)
    async def _seed_versioned(self, embedding_writer: EmbeddingIndexWriter):
        records = [
            _make_versioned_code_record(
                "v1",
                "Voice bot using DailyTransport with TTS and STT services",
                pipecat_version_pin=">=0.0.105",
                path="examples/voice/bot.py",
            ),
            _make_versioned_code_record(
                "v2",
                "Old voice bot pipeline using deprecated patterns",
                pipecat_version_pin="==0.0.85",
                path="examples/old/bot.py",
            ),
            _make_versioned_code_record(
                "v3",
                "Quickstart voice bot example with no version constraint",
                pipecat_version_pin=None,
                path="examples/quickstart/bot.py",
            ),
        ]
        await embedding_writer.upsert(records)

    async def test_version_pin_present_in_hits(self, retriever: HybridRetriever):
        """search_examples should include pipecat_version_pin when available."""
        result = await retriever.search_examples(
            SearchExamplesInput(query="voice bot pipeline")
        )
        assert len(result.hits) > 0
        versioned = [h for h in result.hits if h.pipecat_version_pin is not None]
        assert len(versioned) >= 1, "At least one hit should have pipecat_version_pin"

    async def test_version_pin_exact(self, retriever: HybridRetriever):
        """Verify specific version pins are preserved through the pipeline."""
        result = await retriever.search_examples(
            SearchExamplesInput(query="voice bot pipeline", limit=50)
        )
        pins = {h.example_id: h.pipecat_version_pin for h in result.hits}
        if "v1" in pins:
            assert pins["v1"] == ">=0.0.105"
        if "v2" in pins:
            assert pins["v2"] == "==0.0.85"

    async def test_version_pin_none_for_unconstrained(self, retriever: HybridRetriever):
        """Chunks without version constraint should have pipecat_version_pin=None."""
        result = await retriever.search_examples(
            SearchExamplesInput(query="quickstart bot", limit=50)
        )
        for hit in result.hits:
            if hit.example_id == "v3":
                assert hit.pipecat_version_pin is None

    async def test_various_pin_formats(self, embedding_writer: EmbeddingIndexWriter, retriever: HybridRetriever):
        """Test that different version constraint formats survive round-trip."""
        formats = [
            ("fmt-exact", "==0.0.98", "Bot with exact pin"),
            ("fmt-min", ">=0.0.105", "Bot with minimum pin"),
            ("fmt-range", "<1,>=0.0.93", "Bot with range pin"),
            ("fmt-caret", "^1.7.0", "TypeScript bot with caret range"),
            ("fmt-tilde", "~2.0.0", "TypeScript bot with tilde range"),
            ("fmt-complex", "<0.1,>=0.0.100", "Bot with complex range"),
        ]
        records = [
            _make_versioned_code_record(
                cid, content, pipecat_version_pin=pin,
                path=f"examples/{cid}/bot.py",
            )
            for cid, pin, content in formats
        ]
        await embedding_writer.upsert(records)

        result = await retriever.search_examples(
            SearchExamplesInput(query="bot", limit=50)
        )
        found_pins = {h.example_id: h.pipecat_version_pin for h in result.hits}
        for cid, expected_pin, _ in formats:
            if cid in found_pins:
                assert found_pins[cid] == expected_pin, (
                    f"Version pin mismatch for {cid}: "
                    f"expected {expected_pin!r}, got {found_pins[cid]!r}"
                )


class TestVersionPinInGetCodeSnippet:
    """Verify pipecat_version_pin flows through get_code_snippet results."""

    @pytest.fixture(autouse=True)
    async def _seed_versioned_snippet(self, embedding_writer: EmbeddingIndexWriter):
        records = [
            _make_versioned_code_record(
                "snip1",
                "def create_pipeline():\n    transport = DailyTransport()\n    return Pipeline([transport])\n",
                pipecat_version_pin=">=0.0.105",
                path="examples/snippet/bot.py",
            ),
        ]
        await embedding_writer.upsert(records)

    async def test_snippet_includes_version_pin(self, retriever: HybridRetriever):
        result = await retriever.get_code_snippet(
            GetCodeSnippetInput(intent="create pipeline with DailyTransport")
        )
        if result.snippets:
            snippet = result.snippets[0]
            assert snippet.pipecat_version_pin == ">=0.0.105"


class TestVersionPinInSearchApi:
    """Verify pipecat_version_pin flows through search_api results."""

    @pytest.fixture(autouse=True)
    async def _seed_versioned_api(self, embedding_writer: EmbeddingIndexWriter):
        records = [
            _make_versioned_source_record(
                "api1",
                "# Class: DailyTransport\nModule: pipecat.transports.daily\n\n"
                "## Constructor\ndef __init__(self, room_url, token)\n",
                pipecat_version_pin="0.0.108",
                module_path="pipecat.transports.daily",
                class_name="DailyTransport",
            ),
        ]
        await embedding_writer.upsert(records)

    async def test_api_hit_includes_version_pin(self, retriever: HybridRetriever):
        result = await retriever.search_api(
            SearchApiInput(query="DailyTransport")
        )
        if result.hits:
            for hit in result.hits:
                if hit.chunk_id == "api1":
                    assert hit.pipecat_version_pin == "0.0.108"


class TestVersionPinIndexRoundTrip:
    """Verify version pin survives full index round-trip (embed→store→retrieve)."""

    async def test_round_trip_preserves_pin(
        self,
        embedding_writer: EmbeddingIndexWriter,
        index_store: IndexStore,
        embedding_service: EmbeddingService,
    ):
        record = _make_versioned_code_record(
            "rt1",
            "Round-trip test: voice bot with version pin",
            pipecat_version_pin=">=0.0.100,<0.1",
            path="examples/roundtrip/bot.py",
        )
        await embedding_writer.upsert([record])

        from pipecat_context_hub.shared.types import IndexQuery

        query_vec = embedding_service.embed_query("round-trip voice bot")
        results = await index_store.vector_search(
            IndexQuery(query_text="round-trip voice bot", query_embedding=query_vec, limit=5)
        )
        assert len(results) > 0
        rt_result = next((r for r in results if r.chunk.chunk_id == "rt1"), None)
        assert rt_result is not None
        assert rt_result.chunk.metadata.get("pipecat_version_pin") == ">=0.0.100,<0.1"

    async def test_round_trip_absent_pin(
        self,
        embedding_writer: EmbeddingIndexWriter,
        index_store: IndexStore,
        embedding_service: EmbeddingService,
    ):
        """Chunks without version pin should not have the field after round-trip."""
        record = _make_versioned_code_record(
            "rt2",
            "Round-trip test: no version pin on this example",
            pipecat_version_pin=None,
            path="examples/no-pin/bot.py",
        )
        await embedding_writer.upsert([record])

        from pipecat_context_hub.shared.types import IndexQuery

        query_vec = embedding_service.embed_query("no version pin example")
        results = await index_store.vector_search(
            IndexQuery(query_text="no version pin", query_embedding=query_vec, limit=5)
        )
        rt_result = next((r for r in results if r.chunk.chunk_id == "rt2"), None)
        assert rt_result is not None
        assert rt_result.chunk.metadata.get("pipecat_version_pin") is None


# ---------------------------------------------------------------------------
# Deprecation check tool smoke tests (Phase 1b)
# ---------------------------------------------------------------------------


class TestCheckDeprecationE2E:
    """End-to-end tests for the check_deprecation MCP tool."""

    def _make_retriever_with_deprecation_map(
        self, index_store: IndexStore, embedding_service: EmbeddingService
    ) -> HybridRetriever:
        from pipecat_context_hub.services.ingest.deprecation_map import (
            DeprecationEntry,
            DeprecationMap,
        )

        retriever = HybridRetriever(index_store, embedding_service)
        retriever.deprecation_map = DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                    deprecated_in="0.0.100",
                    note="Use pipecat.services.xai.llm instead",
                ),
                "pipecat.services.cartesia": DeprecationEntry(
                    old_path="pipecat.services.cartesia",
                    new_path="pipecat.services.cartesia.stt, pipecat.services.cartesia.tts",
                    deprecated_in="0.0.99",
                ),
                "pipecat.services.lmnt": DeprecationEntry(
                    old_path="pipecat.services.lmnt",
                    new_path="pipecat.services.lmnt.tts",
                    removed_in="0.0.110",
                    note="Module removed in 0.0.110",
                ),
            },
            pipecat_commit_sha="test123",
        )
        return retriever

    async def test_deprecated_exact_match(
        self, index_store: IndexStore, embedding_service: EmbeddingService
    ):
        """check_deprecation for a known deprecated module returns full info."""
        from pipecat_context_hub.server.tools.check_deprecation import handle_check_deprecation
        import json

        retriever = self._make_retriever_with_deprecation_map(index_store, embedding_service)
        result_json = await handle_check_deprecation(
            {"symbol": "pipecat.services.grok"}, retriever.deprecation_map
        )
        result = json.loads(result_json)
        assert result["deprecated"] is True
        assert result["replacement"] == "pipecat.services.xai.llm"
        assert result["deprecated_in"] == "0.0.100"

    async def test_deprecated_child_match(
        self, index_store: IndexStore, embedding_service: EmbeddingService
    ):
        """check_deprecation for a child path matches the parent entry."""
        from pipecat_context_hub.server.tools.check_deprecation import handle_check_deprecation
        import json

        retriever = self._make_retriever_with_deprecation_map(index_store, embedding_service)
        result_json = await handle_check_deprecation(
            {"symbol": "pipecat.services.grok.llm"}, retriever.deprecation_map
        )
        result = json.loads(result_json)
        assert result["deprecated"] is True
        assert "xai" in result["replacement"]

    async def test_not_deprecated(
        self, index_store: IndexStore, embedding_service: EmbeddingService
    ):
        """check_deprecation for a non-deprecated symbol returns false."""
        from pipecat_context_hub.server.tools.check_deprecation import handle_check_deprecation
        import json

        retriever = self._make_retriever_with_deprecation_map(index_store, embedding_service)
        result_json = await handle_check_deprecation(
            {"symbol": "DailyTransport"}, retriever.deprecation_map
        )
        result = json.loads(result_json)
        assert result["deprecated"] is False
        assert result["replacement"] is None

    async def test_removed_module(
        self, index_store: IndexStore, embedding_service: EmbeddingService
    ):
        """check_deprecation for a removed module shows removed_in."""
        from pipecat_context_hub.server.tools.check_deprecation import handle_check_deprecation
        import json

        retriever = self._make_retriever_with_deprecation_map(index_store, embedding_service)
        result_json = await handle_check_deprecation(
            {"symbol": "pipecat.services.lmnt"}, retriever.deprecation_map
        )
        result = json.loads(result_json)
        assert result["deprecated"] is True
        assert result["removed_in"] == "0.0.110"

    async def test_bracket_expanded_match(
        self, index_store: IndexStore, embedding_service: EmbeddingService
    ):
        """check_deprecation for cartesia returns both stt and tts replacements."""
        from pipecat_context_hub.server.tools.check_deprecation import handle_check_deprecation
        import json

        retriever = self._make_retriever_with_deprecation_map(index_store, embedding_service)
        result_json = await handle_check_deprecation(
            {"symbol": "pipecat.services.cartesia"}, retriever.deprecation_map
        )
        result = json.loads(result_json)
        assert result["deprecated"] is True
        assert "stt" in result["replacement"]
        assert "tts" in result["replacement"]

    async def test_no_map_available(
        self, index_store: IndexStore, embedding_service: EmbeddingService
    ):
        """check_deprecation with no map returns not deprecated + note."""
        from pipecat_context_hub.server.tools.check_deprecation import handle_check_deprecation
        import json

        result_json = await handle_check_deprecation(
            {"symbol": "pipecat.services.grok"}, None
        )
        result = json.loads(result_json)
        assert result["deprecated"] is False
        assert "not available" in (result.get("note") or "")


# ---------------------------------------------------------------------------
# Version-aware retrieval E2E tests (Phase 2)
# ---------------------------------------------------------------------------


class TestVersionAwareRetrieval:
    """E2E tests for version-aware scoring and filtering."""

    @pytest.fixture(autouse=True)
    async def _seed_versioned_examples(self, embedding_writer: EmbeddingIndexWriter):
        records = [
            _make_versioned_code_record(
                "new1",
                "Voice bot using new DailyTransport API with TTS pipeline",
                pipecat_version_pin=">=0.0.105",
                path="examples/new-api/bot.py",
            ),
            _make_versioned_code_record(
                "old1",
                "Voice bot using old DailyTransport API with TTS pipeline",
                pipecat_version_pin="==0.0.85",
                path="examples/old-api/bot.py",
            ),
            _make_versioned_code_record(
                "nopin1",
                "Voice bot pipeline with no version constraint at all",
                pipecat_version_pin=None,
                path="examples/nopin/bot.py",
            ),
        ]
        await embedding_writer.upsert(records)

    async def test_version_compatibility_annotated(self, retriever: HybridRetriever):
        """Results include version_compatibility when pipecat_version is passed."""
        result = await retriever.search_examples(
            SearchExamplesInput(query="voice bot TTS pipeline", pipecat_version="0.0.95")
        )
        assert len(result.hits) > 0
        for hit in result.hits:
            if hit.example_id == "new1":
                assert hit.version_compatibility == "newer_required"
            elif hit.example_id == "old1":
                # ==0.0.85 with user on 0.0.95 → user has passed that version
                assert hit.version_compatibility == "older_targeted"
            elif hit.example_id == "nopin1":
                assert hit.version_compatibility == "unknown"

    async def test_no_version_no_annotation(self, retriever: HybridRetriever):
        """Without pipecat_version, version_compatibility is None."""
        result = await retriever.search_examples(
            SearchExamplesInput(query="voice bot TTS pipeline")
        )
        assert len(result.hits) > 0
        for hit in result.hits:
            assert hit.version_compatibility is None

    async def test_compatible_only_filter(self, retriever: HybridRetriever):
        """version_filter='compatible_only' excludes newer_required results."""
        result = await retriever.search_examples(
            SearchExamplesInput(
                query="voice bot TTS pipeline",
                pipecat_version="0.0.110",
                version_filter="compatible_only",
            )
        )
        for hit in result.hits:
            assert hit.version_compatibility in ("compatible", "older_targeted", "unknown"), (
                f"Hit {hit.example_id} has version_compatibility={hit.version_compatibility}"
            )
