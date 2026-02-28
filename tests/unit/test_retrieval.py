"""Unit tests for the retrieval service (T5).

Tests cover:
- Reciprocal Rank Fusion scoring
- Code-intent heuristics (symbol boost, staleness penalty)
- Full rerank pipeline
- Citation assembly
- Evidence report generation
- HybridRetriever protocol methods (using mock IndexReader)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest

from pipecat_context_hub.services.retrieval.evidence import (
    assemble_evidence,
    build_citation,
    build_known_items,
    build_single_item_evidence,
    build_unknown_items,
)
from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever
from pipecat_context_hub.services.retrieval.rerank import (
    DEFAULT_RRF_K,
    STALENESS_PENALTY,
    SYMBOL_MATCH_BOOST,
    _extract_query_symbols,
    apply_code_intent_heuristics,
    reciprocal_rank_fusion,
    rerank,
)
from pipecat_context_hub.shared.types import (
    ChunkedRecord,
    GetCodeSnippetInput,
    GetDocInput,
    GetExampleInput,
    IndexResult,
    SearchDocsInput,
    SearchExamplesInput,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 2, 17, tzinfo=timezone.utc)


def _make_chunk(
    chunk_id: str,
    content: str = "sample content",
    content_type: Literal["doc", "code", "readme"] = "doc",
    repo: str | None = "pipecat-ai/pipecat",
    path: str = "docs/test.md",
    commit_sha: str | None = "abc123",
    indexed_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id=chunk_id,
        content=content,
        content_type=content_type,
        source_url=f"https://example.com/{path}",
        repo=repo,
        path=path,
        commit_sha=commit_sha,
        indexed_at=indexed_at or NOW,
        metadata=metadata or {},
    )


def _make_result(
    chunk_id: str,
    score: float = 0.8,
    match_type: Literal["vector", "keyword"] = "vector",
    content: str = "sample content",
    content_type: Literal["doc", "code", "readme"] = "doc",
    indexed_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    repo: str | None = "pipecat-ai/pipecat",
    path: str = "docs/test.md",
) -> IndexResult:
    chunk = _make_chunk(
        chunk_id=chunk_id,
        content=content,
        content_type=content_type,
        indexed_at=indexed_at,
        metadata=metadata,
        repo=repo,
        path=path,
    )
    return IndexResult(chunk=chunk, score=score, match_type=match_type)


def _mock_index_reader(
    vector_results: list[IndexResult] | None = None,
    keyword_results: list[IndexResult] | None = None,
) -> AsyncMock:
    """Create a mock IndexReader with configurable results."""
    mock = AsyncMock()
    mock.vector_search = AsyncMock(return_value=vector_results or [])
    mock.keyword_search = AsyncMock(return_value=keyword_results or [])
    return mock


# ===========================================================================
# Rerank module tests
# ===========================================================================


class TestReciprocalRankFusion:
    """Tests for reciprocal_rank_fusion()."""

    def test_single_list(self):
        """RRF with a single list normalizes to 0–1 (rank 1 → 1.0)."""
        r1 = _make_result("a", score=0.9)
        r2 = _make_result("b", score=0.7)
        scores = reciprocal_rank_fusion([[r1, r2]])

        # 1 list: max = 1/(k+1).  Rank 1 normalizes to 1.0.
        assert scores["a"] == pytest.approx(1.0)
        # Rank 2: (1/(k+2)) / (1/(k+1)) = (k+1)/(k+2)
        assert scores["b"] == pytest.approx((DEFAULT_RRF_K + 1) / (DEFAULT_RRF_K + 2))

    def test_two_lists_overlap(self):
        """RRF with overlapping results sums and normalizes scores."""
        r1 = _make_result("a", score=0.9)
        r2 = _make_result("b", score=0.7)
        r3 = _make_result("a", score=0.8, match_type="keyword")

        scores = reciprocal_rank_fusion([[r1, r2], [r3]])

        # "a" rank 1 in both lists → 2/(k+1) / (2/(k+1)) = 1.0
        assert scores["a"] == pytest.approx(1.0)
        # "b" rank 2 in list 1 only → (1/(k+2)) / (2/(k+1)) = (k+1) / (2*(k+2))
        assert scores["b"] == pytest.approx(
            (DEFAULT_RRF_K + 1) / (2 * (DEFAULT_RRF_K + 2))
        )

    def test_custom_k(self):
        """RRF with custom k normalizes rank 1 to 1.0."""
        r1 = _make_result("a", score=0.9)
        scores = reciprocal_rank_fusion([[r1]], k=10)
        assert scores["a"] == pytest.approx(1.0)

    def test_empty_lists(self):
        """RRF with empty lists returns empty dict."""
        scores = reciprocal_rank_fusion([[], []])
        assert scores == {}


class TestExtractQuerySymbols:
    """Tests for _extract_query_symbols()."""

    def test_camel_case(self):
        symbols = _extract_query_symbols("how to use PipelineRunner")
        assert "PipelineRunner" in symbols

    def test_snake_case(self):
        symbols = _extract_query_symbols("use the pipeline_runner function")
        assert "pipeline_runner" in symbols

    def test_dotted(self):
        symbols = _extract_query_symbols("call pipecat.pipeline.run")
        assert "pipecat.pipeline.run" in symbols

    def test_no_symbols(self):
        symbols = _extract_query_symbols("how to create a bot")
        assert symbols == []


class TestCodeIntentHeuristics:
    """Tests for apply_code_intent_heuristics()."""

    def test_symbol_boost(self):
        """Results containing a query symbol get boosted."""
        r1 = _make_result("a", score=0.8, content="class PipelineRunner: pass")
        rrf_scores = {"a": 0.5}

        results = apply_code_intent_heuristics(
            [r1], rrf_scores, "use PipelineRunner", now=NOW
        )
        assert results[0].score == pytest.approx(0.5 + SYMBOL_MATCH_BOOST)

    def test_no_symbol_no_boost(self):
        """Results without symbol match don't get boosted."""
        r1 = _make_result("a", score=0.8, content="just some docs")
        rrf_scores = {"a": 0.5}

        results = apply_code_intent_heuristics(
            [r1], rrf_scores, "use PipelineRunner", now=NOW
        )
        assert results[0].score == pytest.approx(0.5)

    def test_staleness_penalty(self):
        """Old results get penalized."""
        old_date = NOW - timedelta(days=120)
        r1 = _make_result("a", score=0.8, indexed_at=old_date)
        rrf_scores = {"a": 0.5}

        results = apply_code_intent_heuristics(
            [r1], rrf_scores, "some query", now=NOW
        )
        assert results[0].score == pytest.approx(0.5 - STALENESS_PENALTY)

    def test_fresh_no_penalty(self):
        """Recent results don't get penalized."""
        recent_date = NOW - timedelta(days=10)
        r1 = _make_result("a", score=0.8, indexed_at=recent_date)
        rrf_scores = {"a": 0.5}

        results = apply_code_intent_heuristics(
            [r1], rrf_scores, "some query", now=NOW
        )
        assert results[0].score == pytest.approx(0.5)

    def test_sort_order(self):
        """Results are sorted by adjusted score descending."""
        r1 = _make_result("a", score=0.5)
        r2 = _make_result("b", score=0.9, content="class PipelineRunner: pass")
        rrf_scores = {"a": 0.3, "b": 0.2}

        results = apply_code_intent_heuristics(
            [r1, r2], rrf_scores, "use PipelineRunner", now=NOW
        )
        # b gets symbol boost: 0.2 + 0.15 = 0.35 > a's 0.3
        assert results[0].chunk.chunk_id == "b"
        assert results[1].chunk.chunk_id == "a"


class TestRerank:
    """Tests for the full rerank() pipeline."""

    def test_merges_deduplicates(self):
        """Rerank merges and deduplicates results from both lists."""
        v1 = _make_result("a", score=0.9, match_type="vector")
        v2 = _make_result("b", score=0.7, match_type="vector")
        k1 = _make_result("a", score=0.8, match_type="keyword")
        k2 = _make_result("c", score=0.6, match_type="keyword")

        results = rerank([v1, v2], [k1, k2], "test query", now=NOW)

        # Should have 3 unique results: a, b, c
        ids = [r.chunk.chunk_id for r in results]
        assert len(ids) == 3
        assert set(ids) == {"a", "b", "c"}

    def test_empty_inputs(self):
        """Rerank with empty inputs returns empty."""
        results = rerank([], [], "test", now=NOW)
        assert results == []


# ===========================================================================
# Evidence module tests
# ===========================================================================


class TestBuildCitation:
    """Tests for build_citation()."""

    def test_builds_from_result(self):
        """Citation is built correctly from an IndexResult."""
        result = _make_result(
            "test-chunk",
            content="test",
            repo="pipecat-ai/pipecat",
            path="docs/guide.md",
        )
        result.chunk.commit_sha = "sha123"
        result.chunk.metadata = {"section": "Overview", "line_range": [1, 10]}

        citation = build_citation(result)

        assert citation.source_url == result.chunk.source_url
        assert citation.repo == "pipecat-ai/pipecat"
        assert citation.path == "docs/guide.md"
        assert citation.commit_sha == "sha123"
        assert citation.section == "Overview"
        assert citation.line_range == (1, 10)
        assert citation.indexed_at == result.chunk.indexed_at

    def test_minimal_metadata(self):
        """Citation works with minimal metadata."""
        result = _make_result("test", repo=None)
        citation = build_citation(result)
        assert citation.repo is None
        assert citation.section is None
        assert citation.line_range is None


class TestBuildKnownItems:
    """Tests for build_known_items()."""

    def test_creates_known_per_result(self):
        """Each result becomes a KnownItem."""
        results = [
            _make_result("a", score=0.8, content="Fact A about pipecat"),
            _make_result("b", score=0.6, content="Fact B about pipelines"),
        ]
        known = build_known_items(results)
        assert len(known) == 2
        assert known[0].statement == "Fact A about pipecat"
        assert len(known[0].citations) == 1
        assert known[0].confidence == 0.8

    def test_truncates_long_content(self):
        """Long content is truncated in the statement."""
        long_content = "x" * 300
        results = [_make_result("a", content=long_content)]
        known = build_known_items(results)
        assert known[0].statement.endswith("...")
        assert len(known[0].statement) == 203  # 200 + "..."


class TestBuildUnknownItems:
    """Tests for build_unknown_items()."""

    def test_no_results(self):
        """No results generates an unknown item."""
        unknowns = build_unknown_items("test query", [])
        assert len(unknowns) == 1
        assert "No content found" in unknowns[0].question

    def test_low_score_results(self):
        """All-low-score results generate an unknown item."""
        results = [_make_result("a", score=0.05)]
        unknowns = build_unknown_items("test query", results)
        assert len(unknowns) == 1
        assert "Low relevance" in unknowns[0].question

    def test_good_results_no_unknowns(self):
        """Good results produce no unknowns."""
        results = [_make_result("a", score=0.8)]
        unknowns = build_unknown_items("test query", results)
        assert len(unknowns) == 0


class TestAssembleEvidence:
    """Tests for assemble_evidence()."""

    def test_full_evidence_report(self):
        """Assembles a complete EvidenceReport."""
        results = [
            _make_result("a", score=0.8),
            _make_result("b", score=0.7),
            _make_result("c", score=0.6),
        ]
        report = assemble_evidence("test query", results)

        assert len(report.known) == 3
        assert report.confidence > 0.0
        assert report.confidence_rationale != ""
        assert isinstance(report.next_retrieval_queries, list)

    def test_empty_results_report(self):
        """Empty results produce zero confidence."""
        report = assemble_evidence("test query", [])
        assert report.confidence == 0.0
        assert len(report.unknown) == 1

    def test_filters_in_next_queries(self):
        """Repo filter generates a 'search across all repos' suggestion."""
        results = [_make_result("a", score=0.8)]
        report = assemble_evidence("test", results, {"repo": "pipecat-ai/pipecat"})
        assert any("all repos" in q for q in report.next_retrieval_queries)


class TestBuildSingleItemEvidence:
    """Tests for build_single_item_evidence()."""

    def test_found_item(self):
        """Found item produces high confidence."""
        result = _make_result("doc-123")
        report = build_single_item_evidence(result, "doc-123", "document")
        assert report.confidence == 1.0
        assert len(report.known) == 1
        assert len(report.unknown) == 0

    def test_not_found_item(self):
        """Not-found item produces zero confidence."""
        report = build_single_item_evidence(None, "missing-id", "document")
        assert report.confidence == 0.0
        assert len(report.known) == 0
        assert len(report.unknown) == 1
        assert "not found" in report.unknown[0].question.lower()


# ===========================================================================
# HybridRetriever tests (uses mock IndexReader)
# ===========================================================================


class TestHybridRetrieverSearchDocs:
    """Tests for HybridRetriever.search_docs()."""

    async def test_basic_search(self):
        """search_docs returns hits and evidence."""
        r1 = _make_result("doc-1", score=0.8, content="Getting started with pipecat")
        r2 = _make_result("doc-2", score=0.6, content="Advanced pipeline configuration")

        mock_reader = _mock_index_reader(vector_results=[r1], keyword_results=[r2])
        retriever = HybridRetriever(mock_reader)

        output = await retriever.search_docs(
            SearchDocsInput(query="pipecat getting started")
        )

        assert len(output.hits) > 0
        assert output.evidence is not None
        assert output.evidence.confidence > 0.0
        # All hits have citations
        for hit in output.hits:
            assert hit.citation.source_url != ""

    async def test_with_area_filter(self):
        """search_docs passes area filter to the index."""
        mock_reader = _mock_index_reader()
        retriever = HybridRetriever(mock_reader)

        await retriever.search_docs(
            SearchDocsInput(query="test", area="guides")
        )

        # Verify the query included content_type and area filters
        call_args = mock_reader.vector_search.call_args
        query = call_args[0][0]
        assert query.filters["content_type"] == "doc"
        assert query.filters["area"] == "guides"

    async def test_empty_results(self):
        """search_docs with no results returns empty hits and low confidence."""
        mock_reader = _mock_index_reader()
        retriever = HybridRetriever(mock_reader)

        output = await retriever.search_docs(
            SearchDocsInput(query="nonexistent topic")
        )

        assert len(output.hits) == 0
        assert output.evidence.confidence == 0.0


class TestHybridRetrieverGetDoc:
    """Tests for HybridRetriever.get_doc()."""

    async def test_found(self):
        """get_doc returns the document when found."""
        r1 = _make_result(
            "doc-123",
            content="# Guide\n\nThis is the guide content.",
            metadata={"title": "Getting Started", "sections": ["Guide"]},
        )
        mock_reader = _mock_index_reader(keyword_results=[r1])
        retriever = HybridRetriever(mock_reader)

        output = await retriever.get_doc(GetDocInput(doc_id="doc-123"))

        assert output.doc_id == "doc-123"
        assert output.title == "Getting Started"
        assert output.content != ""
        assert output.evidence.confidence == 1.0

    async def test_not_found(self):
        """get_doc returns empty content when document not found."""
        mock_reader = _mock_index_reader()
        retriever = HybridRetriever(mock_reader)

        output = await retriever.get_doc(GetDocInput(doc_id="missing"))

        assert output.doc_id == "missing"
        assert output.title == "Not Found"
        assert output.content == ""
        assert output.evidence.confidence == 0.0

    async def test_section_filter(self):
        """get_doc with section extracts the requested section."""
        content = "# Overview\n\nIntro text.\n\n## Details\n\nDetailed content here."
        r1 = _make_result(
            "doc-sec",
            content=content,
            metadata={"title": "Doc", "sections": ["Overview", "Details"]},
        )
        mock_reader = _mock_index_reader(keyword_results=[r1])
        retriever = HybridRetriever(mock_reader)

        output = await retriever.get_doc(
            GetDocInput(doc_id="doc-sec", section="Details")
        )

        assert "Detailed content" in output.content


class TestHybridRetrieverSearchExamples:
    """Tests for HybridRetriever.search_examples()."""

    async def test_basic_search(self):
        """search_examples returns hits with example metadata."""
        r1 = _make_result(
            "ex-1",
            score=0.9,
            content="TTS example using ElevenLabs",
            content_type="code",
            repo="pipecat-ai/pipecat",
            metadata={"capability_tags": ["tts"], "key_files": ["main.py"]},
        )
        mock_reader = _mock_index_reader(vector_results=[r1])
        retriever = HybridRetriever(mock_reader)

        output = await retriever.search_examples(
            SearchExamplesInput(query="tts example")
        )

        assert len(output.hits) > 0
        assert output.hits[0].capability_tags == ["tts"]
        assert output.evidence is not None

    async def test_with_filters(self):
        """search_examples passes all filters to the index."""
        mock_reader = _mock_index_reader()
        retriever = HybridRetriever(mock_reader)

        await retriever.search_examples(
            SearchExamplesInput(
                query="test",
                repo="pipecat-ai/pipecat",
                tags=["tts"],
                foundational_class="01-say-one-thing",
            )
        )

        call_args = mock_reader.vector_search.call_args
        query = call_args[0][0]
        assert query.filters["repo"] == "pipecat-ai/pipecat"
        assert query.filters["capability_tags"] == ["tts"]
        assert query.filters["foundational_class"] == "01-say-one-thing"


class TestHybridRetrieverGetExample:
    """Tests for HybridRetriever.get_example()."""

    async def test_found(self):
        """get_example returns example data when found."""
        r1 = _make_result(
            "ex-123",
            content="async def main(): pass",
            content_type="code",
            metadata={
                "key_files": ["main.py"],
                "detected_symbols": ["main"],
                "language": "python",
            },
        )
        mock_reader = _mock_index_reader(keyword_results=[r1])
        retriever = HybridRetriever(mock_reader)

        output = await retriever.get_example(GetExampleInput(example_id="ex-123"))

        assert output.example_id == "ex-123"
        assert len(output.files) > 0
        assert output.evidence.confidence == 1.0

    async def test_not_found(self):
        """get_example returns empty data when not found."""
        mock_reader = _mock_index_reader()
        retriever = HybridRetriever(mock_reader)

        output = await retriever.get_example(
            GetExampleInput(example_id="missing-ex")
        )

        assert output.example_id == "missing-ex"
        assert len(output.files) == 0
        assert output.evidence.confidence == 0.0


class TestHybridRetrieverGetCodeSnippet:
    """Tests for HybridRetriever.get_code_snippet()."""

    async def test_by_intent(self):
        """get_code_snippet by intent returns matching snippets."""
        r1 = _make_result(
            "snippet-1",
            content="def create_pipeline():\n    return Pipeline()",
            content_type="code",
            metadata={"line_start": 1, "line_end": 2, "language": "python"},
        )
        mock_reader = _mock_index_reader(vector_results=[r1])
        retriever = HybridRetriever(mock_reader)

        output = await retriever.get_code_snippet(
            GetCodeSnippetInput(intent="create a pipeline")
        )

        assert len(output.snippets) > 0
        assert output.snippets[0].language == "python"
        assert output.evidence is not None

    async def test_by_symbol(self):
        """get_code_snippet by symbol searches for the symbol."""
        r1 = _make_result(
            "snippet-sym",
            content="class PipelineRunner:\n    pass",
            content_type="code",
            metadata={"line_start": 10, "line_end": 11},
        )
        mock_reader = _mock_index_reader(vector_results=[r1], keyword_results=[r1])
        retriever = HybridRetriever(mock_reader)

        output = await retriever.get_code_snippet(
            GetCodeSnippetInput(symbol="PipelineRunner")
        )

        assert len(output.snippets) > 0

    async def test_by_path_and_line(self):
        """get_code_snippet by path+line_start works."""
        r1 = _make_result(
            "snippet-path",
            content="import os\nprint('hello')",
            content_type="code",
            path="src/main.py",
            metadata={"line_start": 5, "line_end": 6},
        )
        mock_reader = _mock_index_reader(vector_results=[r1])
        retriever = HybridRetriever(mock_reader)

        output = await retriever.get_code_snippet(
            GetCodeSnippetInput(path="src/main.py", line_start=5)
        )

        assert len(output.snippets) > 0
        assert output.snippets[0].path == "src/main.py"

    async def test_max_lines_truncation(self):
        """Snippets exceeding max_lines are truncated."""
        long_code = "\n".join(f"line {i}" for i in range(100))
        r1 = _make_result(
            "snippet-long",
            content=long_code,
            content_type="code",
            metadata={"line_start": 1, "line_end": 100},
        )
        mock_reader = _mock_index_reader(vector_results=[r1])
        retriever = HybridRetriever(mock_reader)

        output = await retriever.get_code_snippet(
            GetCodeSnippetInput(intent="long code", max_lines=5)
        )

        assert len(output.snippets) > 0
        snippet_lines = output.snippets[0].content.splitlines()
        assert len(snippet_lines) <= 5


class TestHybridRetrieverProtocol:
    """Verify HybridRetriever satisfies the Retriever protocol."""

    def test_implements_protocol(self):
        """HybridRetriever has all methods required by the Retriever protocol."""
        mock_reader = _mock_index_reader()
        retriever = HybridRetriever(mock_reader)

        assert hasattr(retriever, "search_docs")
        assert hasattr(retriever, "get_doc")
        assert hasattr(retriever, "search_examples")
        assert hasattr(retriever, "get_example")
        assert hasattr(retriever, "get_code_snippet")

    def test_configurable_weights(self):
        """HybridRetriever accepts configurable weights."""
        mock_reader = _mock_index_reader()
        retriever = HybridRetriever(
            mock_reader,
            rrf_k=30,
            vector_weight=0.7,
            keyword_weight=0.3,
        )
        assert retriever._rrf_k == 30
        assert retriever._vector_weight == 0.7
        assert retriever._keyword_weight == 0.3
