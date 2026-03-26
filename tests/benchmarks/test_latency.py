"""Latency benchmarks for retrieval pipeline components and MCP tools.

Run with:  uv run pytest tests/benchmarks/ -v -s
Selective: uv run pytest -m benchmark -v -s
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Literal, TypedDict

import pytest

from pipecat_context_hub.services.retrieval.evidence import assemble_evidence
from pipecat_context_hub.services.retrieval.rerank import rerank
from pipecat_context_hub.shared.types import (
    ChunkedRecord,
    GetCodeSnippetInput,
    GetDocInput,
    GetExampleInput,
    IndexQuery,
    IndexResult,
    SearchDocsInput,
    SearchExamplesInput,
)

NOW = datetime.now(tz=timezone.utc)

# Number of timed iterations per benchmark (first is warmup, discarded).
_ITERATIONS = 5


class LatencyStats(TypedDict):
    warmup_ms: float
    min_ms: float
    median_ms: float
    max_ms: float
    runs: list[float]


def _measure(fn: Callable[[], object], iterations: int = _ITERATIONS) -> LatencyStats:
    """Run *fn* multiple times, return timing stats in milliseconds.

    First iteration is treated as warmup and excluded from stats.
    """
    times: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        elapsed_ms = (time.perf_counter() - start) * 1000
        times.append(elapsed_ms)

    # Discard warmup (first run)
    measured = times[1:]
    return {
        "warmup_ms": times[0],
        "min_ms": min(measured),
        "median_ms": statistics.median(measured),
        "max_ms": max(measured),
        "runs": measured,
    }


async def _measure_async(
    fn: Callable[[], Awaitable[object]], iterations: int = _ITERATIONS
) -> LatencyStats:
    """Async variant of _measure."""
    times: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        await fn()
        elapsed_ms = (time.perf_counter() - start) * 1000
        times.append(elapsed_ms)

    measured = times[1:]
    return {
        "warmup_ms": times[0],
        "min_ms": min(measured),
        "median_ms": statistics.median(measured),
        "max_ms": max(measured),
        "runs": measured,
    }


def _report(name: str, stats: LatencyStats) -> None:
    """Print a single benchmark result line."""
    print(
        f"  {name:<35} "
        f"median={stats['median_ms']:7.1f}ms  "
        f"min={stats['min_ms']:7.1f}ms  "
        f"max={stats['max_ms']:7.1f}ms  "
        f"warmup={stats['warmup_ms']:7.1f}ms"
    )


# ---------------------------------------------------------------------------
# Component benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestComponentLatency:
    """Benchmark individual pipeline stages."""

    def test_embed_query_latency(self, bench_embedding_service):
        """EmbeddingService.embed_query() should be <100ms after warmup."""
        # Warmup the model (first call triggers lazy load)
        bench_embedding_service.embed_query("warmup")

        stats = _measure(
            lambda: bench_embedding_service.embed_query("how to build a pipecat voice bot")
        )
        _report("embed_query", stats)
        assert stats["median_ms"] < 100, f"embed_query too slow: {stats['median_ms']:.1f}ms"

    async def test_vector_search_latency(self, bench_seeded_store, bench_embedding_service):
        """VectorIndex.search() should be <200ms over 100 records."""
        embedding = bench_embedding_service.embed_query("pipecat pipeline TTS")
        query = IndexQuery(
            query_text="pipecat pipeline TTS",
            query_embedding=embedding,
            filters={"content_type": "code"},
            limit=10,
        )

        stats = await _measure_async(lambda: bench_seeded_store.vector_search(query))
        _report("vector_search", stats)
        assert stats["median_ms"] < 200, f"vector_search too slow: {stats['median_ms']:.1f}ms"

    async def test_keyword_search_latency(self, bench_seeded_store):
        """FTSIndex.search() should be <100ms over 100 records."""
        query = IndexQuery(
            query_text="pipecat pipeline voice bot",
            filters={"content_type": "doc"},
            limit=10,
        )

        stats = await _measure_async(lambda: bench_seeded_store.keyword_search(query))
        _report("keyword_search", stats)
        assert stats["median_ms"] < 100, f"keyword_search too slow: {stats['median_ms']:.1f}ms"

    def test_rerank_latency(self):
        """rerank() with 20+20 results should be <10ms."""
        def _make_result(i: int, match_type: Literal["vector", "keyword"]) -> IndexResult:
            return IndexResult(
                chunk=ChunkedRecord(
                    chunk_id=f"chunk-{match_type}-{i}",
                    content=f"from pipecat.pipeline import Pipeline\n# step {i}",
                    content_type="code",
                    source_url=f"https://example.com/{i}",
                    path=f"example/{i}.py",
                    indexed_at=NOW,
                ),
                score=1.0 - (i * 0.04),
                match_type=match_type,
            )

        vector_results = [_make_result(i, "vector") for i in range(20)]
        keyword_results = [_make_result(i, "keyword") for i in range(20)]

        stats = _measure(
            lambda: rerank(vector_results, keyword_results, "pipecat Pipeline setup")
        )
        _report("rerank", stats)
        assert stats["median_ms"] < 10, f"rerank too slow: {stats['median_ms']:.1f}ms"

    def test_evidence_assembly_latency(self):
        """assemble_evidence() with 10 results should be <10ms."""
        results = [
            IndexResult(
                chunk=ChunkedRecord(
                    chunk_id=f"chunk-{i}",
                    content=f"Documentation about feature {i}. " * 20,
                    content_type="doc",
                    source_url=f"https://docs.pipecat.ai/page-{i}",
                    path=f"/docs/page-{i}",
                    indexed_at=NOW,
                ),
                score=0.9 - (i * 0.05),
                match_type="vector",
            )
            for i in range(10)
        ]

        stats = _measure(
            lambda: assemble_evidence("pipecat voice bot", results, {"content_type": "doc"})
        )
        _report("assemble_evidence", stats)
        assert stats["median_ms"] < 10, f"assemble_evidence too slow: {stats['median_ms']:.1f}ms"


# ---------------------------------------------------------------------------
# End-to-end tool benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestToolLatency:
    """Benchmark the 5 MCP tool methods end-to-end."""

    async def test_search_docs_latency(self, bench_retriever):
        """search_docs full path should be <500ms."""
        inp = SearchDocsInput(query="how to configure TTS in pipecat", limit=5)
        stats = await _measure_async(lambda: bench_retriever.search_docs(inp))
        _report("search_docs", stats)
        assert stats["median_ms"] < 500, f"search_docs too slow: {stats['median_ms']:.1f}ms"

    async def test_search_examples_latency(self, bench_retriever):
        """search_examples full path should be <500ms."""
        inp = SearchExamplesInput(query="voice bot with ElevenLabs TTS", limit=5)
        stats = await _measure_async(lambda: bench_retriever.search_examples(inp))
        _report("search_examples", stats)
        assert stats["median_ms"] < 500, f"search_examples too slow: {stats['median_ms']:.1f}ms"

    async def test_get_doc_latency(self, bench_retriever, bench_seeded_store):
        """get_doc direct lookup should be <100ms."""
        # Find a real doc_id from the seeded store
        query = IndexQuery(
            query_text="getting started",
            filters={"content_type": "doc"},
            limit=1,
        )
        results = await bench_seeded_store.keyword_search(query)
        assert results, "Need at least one doc in seeded store"
        doc_id = results[0].chunk.chunk_id

        inp = GetDocInput(doc_id=doc_id)
        stats = await _measure_async(lambda: bench_retriever.get_doc(inp))
        _report("get_doc", stats)
        assert stats["median_ms"] < 100, f"get_doc too slow: {stats['median_ms']:.1f}ms"

    async def test_get_example_latency(self, bench_retriever, bench_seeded_store):
        """get_example direct lookup should be <100ms."""
        query = IndexQuery(
            query_text="Pipeline",
            filters={"content_type": "code"},
            limit=1,
        )
        results = await bench_seeded_store.keyword_search(query)
        assert results, "Need at least one code record in seeded store"
        example_id = results[0].chunk.chunk_id

        inp = GetExampleInput(example_id=example_id)
        stats = await _measure_async(lambda: bench_retriever.get_example(inp))
        _report("get_example", stats)
        assert stats["median_ms"] < 100, f"get_example too slow: {stats['median_ms']:.1f}ms"

    async def test_get_code_snippet_latency(self, bench_retriever):
        """get_code_snippet full path should be <500ms."""
        inp = GetCodeSnippetInput(intent="create a pipeline with TTS")
        stats = await _measure_async(lambda: bench_retriever.get_code_snippet(inp))
        _report("get_code_snippet", stats)
        assert stats["median_ms"] < 500, f"get_code_snippet too slow: {stats['median_ms']:.1f}ms"


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestLatencySummary:
    """Print a summary table of all 5 tool latencies."""

    async def test_print_latency_summary(self, bench_retriever, bench_seeded_store):
        """Summary table — run with pytest -s to see output."""
        # Lookup real IDs for direct-lookup tools
        doc_results = await bench_seeded_store.keyword_search(
            IndexQuery(query_text="getting started", filters={"content_type": "doc"}, limit=1)
        )
        code_results = await bench_seeded_store.keyword_search(
            IndexQuery(query_text="Pipeline", filters={"content_type": "code"}, limit=1)
        )
        doc_id = doc_results[0].chunk.chunk_id if doc_results else "doc-missing"
        example_id = code_results[0].chunk.chunk_id if code_results else "code-missing"

        tools = [
            ("search_docs", lambda: bench_retriever.search_docs(
                SearchDocsInput(query="configure TTS", limit=5))),
            ("search_examples", lambda: bench_retriever.search_examples(
                SearchExamplesInput(query="voice bot ElevenLabs", limit=5))),
            ("get_doc", lambda: bench_retriever.get_doc(
                GetDocInput(doc_id=doc_id))),
            ("get_example", lambda: bench_retriever.get_example(
                GetExampleInput(example_id=example_id))),
            ("get_code_snippet", lambda: bench_retriever.get_code_snippet(
                GetCodeSnippetInput(intent="create pipeline with TTS"))),
        ]

        print("\n" + "=" * 72)
        print("  LATENCY SUMMARY (100 records, median of 4 runs)")
        print("=" * 72)

        for name, fn in tools:
            stats = await _measure_async(fn)
            _report(name, stats)

        print("=" * 72)
